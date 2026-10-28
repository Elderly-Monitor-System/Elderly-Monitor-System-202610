"""随机森林：按人员划分、峰值弱标注、冲击后判定"""
import argparse
import hashlib
import json
import re
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import scipy
from scipy.signal import butter, sosfilt, sosfilt_zi
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from fall_ml import FEATURE_VERSION, RATE_HZ, STRIDE, WINDOW, Forest, features
from download_sisfall import SHA256, URL

FILE_RE = re.compile(r"^([DF]\d{2})_(S[AE]\d{2})_(R\d{2})\.txt$")
SEED = 20261021


def split_subjects(subjects):
    # 按人员及年龄组划分，均含跌倒
    # SE06：唯一老年跌倒受试者，留作测试
    validation = {"SA03", "SA09", "SA15", "SA21", "SE03", "SE09"}
    test = {"SA04", "SA10", "SA16", "SA22", "SE06", "SE12", "SE15"}
    return {s: "test" if s in test else "validation" if s in validation else "train"
            for s in sorted(subjects)}


def read_record(archive, name):
    text = archive.read(name).decode("ascii").replace(";", ",").rstrip().rstrip(",")
    values = np.fromstring(text, dtype=np.float64, sep=",")
    if values.size % 9 or values.size < WINDOW * 4 * 9:
        raise ValueError(f"bad record: {name}")
    raw = values.reshape(-1, 9)[:, :6].copy()
    raw[:, :3] *= 32 / 8192       # ADXL345 ±16g / 13 bit
    raw[:, 3:] *= 4000 / 65536   # ITG3200 ±2000 deg/s / 16 bit
    if not np.isfinite(raw).all():
        raise ValueError(f"nonfinite record: {name}")
    raw[:, :3] = np.clip(raw[:, :3], -8, 8)
    raw[:, 3:] = np.clip(raw[:, 3:], -500, 500)
    # 因果低通近似 MPU6050 DLPF

    sos = butter(4, 20, fs=200, output="sos")
    zi = sosfilt_zi(sos)[:, :, None] * raw[0][None, None, :]
    filtered, _ = sosfilt(sos, raw, axis=0, zi=zi)
    return filtered[3::4]


def prepare(path):
    cache = path.with_name("sisfall_features_v1.npz")
    if cache.exists():
        with np.load(cache, allow_pickle=False) as data:
            if str(data["dataset_sha256"]) != SHA256 or str(data["feature_version"]) != FEATURE_VERSION:
                raise ValueError("incompatible cache")
            return {k: data[k] for k in data.files}
    with path.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != SHA256:
            raise ValueError("dataset hash mismatch")
    X, y, subjects, files, end_times, peaks, falls = [], [], [], [], [], [], []
    records = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(n for n in archive.namelist()
                       if not n.startswith("__MACOSX/") and FILE_RE.match(Path(n).name))
        for index, name in enumerate(names):
            activity, subject, _ = FILE_RE.match(Path(name).name).groups()
            rows = read_record(archive, name)
            fall = activity.startswith("F")
            peak = int(np.argmax(np.linalg.norm(rows[:, :3], axis=1))) if fall else -1
            records.append({"file": Path(name).name, "subject": subject,
                            "fall": fall, "seconds": len(rows) / RATE_HZ,
                            "weak_peak_s": peak / RATE_HZ if fall else None})
            for end in range(WINDOW, len(rows) + 1, STRIDE):
                # 仅标注冲击峰附近窗口为正例
                label = int(peak + 25 <= end <= peak + 75) if fall else 0
                if fall and not label:
                    label = -1  # 不确定窗口不参与拟合
                X.append(features(rows[end-WINDOW:end].tolist()))
                y.append(label); subjects.append(subject); files.append(Path(name).name)
                end_times.append(end / RATE_HZ); peaks.append(peak / RATE_HZ); falls.append(fall)
            if (index + 1) % 250 == 0:
                print(f"features: {index+1}/{len(names)} records, {len(X)} windows", flush=True)
    result = dict(X=np.asarray(X, dtype=np.float32), y=np.asarray(y, dtype=np.int8),
                  subjects=np.asarray(subjects), files=np.asarray(files),
                  end_times=np.asarray(end_times), peaks=np.asarray(peaks),
                  falls=np.asarray(falls), records=np.asarray(json.dumps(records)),
                  dataset_sha256=np.asarray(SHA256), feature_version=np.asarray(FEATURE_VERSION))
    np.savez_compressed(cache, **result)
    return result


def events(scores, times, threshold):
    """事件策略：连续两次命中，冷却 8s"""
    hits = 0; last_event = -1e9; out = []
    for score, t in zip(scores, times):
        hits = hits + 1 if score >= threshold else 0
        if hits >= 2 and t - last_event >= 8:
            out.append(float(t)); last_event = t; hits = 0
    return out


def evaluate(data, scores, mask, threshold):
    rows = np.flatnonzero(mask)
    records = {r["file"]: r for r in json.loads(str(data["records"]))}
    tp = fn = fp_trials = event_count = fall_trials = adl_trials = 0
    adl_seconds = 0.; delays = []
    for file in sorted(set(data["files"][rows])):
        ids = rows[data["files"][rows] == file]
        rec = records[file]
        times = data["end_times"][ids]
        detected = events(scores[ids], times, threshold)
        if rec["fall"]:
            fall_trials += 1
            # 事件时间匹配使用启发式峰值
            matched = [t for t in detected if rec["weak_peak_s"] <= t <= rec["weak_peak_s"] + 2.5]
            if matched:
                tp += 1; delays.append(min(matched) - rec["weak_peak_s"])
            else:
                fn += 1
        else:
            adl_trials += 1; adl_seconds += max(0., rec["seconds"] - WINDOW / RATE_HZ)
            fp_trials += bool(detected); event_count += len(detected)
    valid = mask & (data["y"] >= 0)
    predicted = scores[valid] >= threshold
    precision, recall, f1, _ = precision_recall_fscore_support(data["y"][valid], predicted,
                                                             average="binary", zero_division=0)
    return {"fall_trials": fall_trials, "weak_peak_matched_falls": tp, "missed_fall_trials": fn,
            "weak_peak_event_recall": tp / max(fall_trials, 1), "adl_trials": adl_trials,
            "adl_trials_with_false_alert": fp_trials, "false_alerts": event_count,
            "adl_evaluated_hours": adl_seconds / 3600,
            "false_alerts_per_adl_hour": event_count / max(adl_seconds / 3600, 1e-9),
            "median_delay_from_heuristic_peak_s": float(np.median(delays)) if delays else None,
            "weak_window_precision": float(precision), "weak_window_recall": float(recall),
            "weak_window_f1": float(f1),
            "weak_window_confusion_tn_fp_fn_tp": confusion_matrix(data["y"][valid], predicted, labels=[0,1]).ravel().tolist()}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--data", type=Path, default=Path(".build/ml-data/SisFall_dataset.zip"))
    parser.add_argument("--output", type=Path, default=Path("backend/models/sisfall_rf_v1.json"))
    parser.add_argument("--report", type=Path, default=Path("ml/reports/sisfall_rf_v1.json"))
    args = parser.parse_args()
    for path in (args.output, args.report):
        if path.exists():
            raise ValueError(f"output already exists; choose a new version: {path}")
    data = prepare(args.data)
    split = split_subjects(set(data["subjects"]))
    masks = {part: np.asarray([split[s] == part for s in data["subjects"]])
             for part in ("train", "validation", "test")}
    fit = masks["train"] & (data["y"] >= 0)
    forest = RandomForestClassifier(n_estimators=48, max_depth=8, min_samples_leaf=8,
                                    class_weight="balanced_subsample", random_state=SEED, n_jobs=2)
    started = time.perf_counter()
    forest.fit(data["X"][fit], data["y"][fit])
    fit_seconds = time.perf_counter() - started
    scores = forest.predict_proba(data["X"])[:, 1]
    candidates = []
    # 仅用验证人员选择阈值
    for threshold in (.4, .5, .6, .7, .8, .85, .9):
        metrics = evaluate(data, scores, masks["validation"], threshold)
        candidates.append({"threshold": threshold, **metrics})
    feasible = [c for c in candidates if c["weak_peak_event_recall"] >= .90]
    chosen = min(feasible, key=lambda c: (c["false_alerts_per_adl_hour"], -c["weak_peak_event_recall"])) if feasible else max(candidates, key=lambda c: c["weak_window_f1"])
    model = {"format": "imu-rf-json-v1", "model_id": "sisfall-rf-v1-20261021",
             "feature_version": FEATURE_VERSION, "sample_rate_hz": RATE_HZ, "window_samples": WINDOW,
             "stride_samples": STRIDE, "units": "g,dps", "threshold": chosen["threshold"],
             "mode": "shadow", "dataset_sha256": SHA256, "trees": []}
    for estimator in forest.estimators_:
        tree = estimator.tree_
        value = tree.value[:, 0, :]
        p = value[:, 1] / value.sum(axis=1)
        model["trees"].append({"left": tree.children_left.tolist(), "right": tree.children_right.tolist(),
                               "feature": tree.feature.tolist(), "threshold": tree.threshold.tolist(), "p": p.tolist()})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(model, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    exported = Forest(args.output)
    # 校验导出模型分数一致
    held = np.flatnonzero(masks["test"])
    actual = np.asarray([exported.score_features(data["X"][i]) for i in held])
    error = float(np.max(np.abs(actual - scores[held])))
    if error > 1e-10:
        raise AssertionError(f"export differs: {error}")
    report = {"date_beijing": "2026-10-21", "model_id": model["model_id"],
              "dataset": "SisFall public mirror (not original server)", "source_url": URL,
              "dataset_sha256": SHA256, "seed": SEED,
              "versions": {"numpy": np.__version__, "scipy": scipy.__version__, "sklearn": sklearn.__version__},
              "subject_splits": {p: sorted(s for s, part in split.items() if part == p) for p in masks},
              "record_count": len(json.loads(str(data["records"]))),
              "window_counts": {p: int(np.sum(m)) for p, m in masks.items()},
              "fitted_windows": int(np.sum(fit)), "feature_count": data["X"].shape[1],
              "fit_seconds": fit_seconds, "forest": {"trees": 48, "max_depth": 8, "min_samples_leaf": 8},
              "threshold": chosen["threshold"], "validation_candidates": candidates,
              "test": evaluate(data, scores, masks["test"], chosen["threshold"]),
              "export_max_score_error_on_all_test_windows": error,
              "model_bytes": args.output.stat().st_size,
              "model_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
              "limitations": ["Fall event timing uses the largest acceleration peak, NOT frame/video annotations.",
                              "Ambiguous windows from fall trials excluded from fitting; all evaluated in event replay.",
                              "Mostly simulated young-adult falls; SE06 only older fall performer, held out in test.",
                              "Waist placement; chest placement and real MPU6050 domain not validated.",
                              "Public sensor filtering approximates MPU6050 hardware DLPF, not identical.",
                              "ADL false alerts/hour measured on short scripted trials, not free-living use.",
                              "Scores uncalibrated; shadow mode does not trigger device alarms/photos."]}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)+"\n", encoding="utf-8")
    print(json.dumps({"model": str(args.output), "report": str(args.report), "threshold": model["threshold"],
                      "test": report["test"], "model_bytes": report["model_bytes"]}, indent=2), flush=True)


if __name__ == "__main__":
    main()
