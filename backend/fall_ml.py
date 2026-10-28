"""六轴随机森林：50Hz、150 点、g/deg/s；分数未经校准"""
import json
import math
from pathlib import Path

RATE_HZ = 50
WINDOW = 150
STRIDE = 25
FEATURE_VERSION = "imu-invariant-v1"


def _stats(values):
    n = len(values)
    mean = sum(values) / n
    ordered = sorted(values)
    return [mean, math.sqrt(sum((x - mean) ** 2 for x in values) / n),
            ordered[0], ordered[-1], ordered[int((n - 1) * .1)],
            ordered[int((n - 1) * .5)], ordered[int((n - 1) * .9)],
            math.sqrt(sum(x * x for x in values) / n)]


def features(samples):
    """共用特征，无未来采样或拟合归一化"""
    if len(samples) != WINDOW:
        raise ValueError(f"expected {WINDOW} samples")
    rows = []
    for row in samples:
        if len(row) != 6 or any(not math.isfinite(float(v)) for v in row):
            raise ValueError("invalid six-axis sample")
        # 限幅：±8g、±500°/s
        rows.append([max(-8., min(8., float(x))) for x in row[:3]] +
                    [max(-500., min(500., float(x))) for x in row[3:]])
    a = [math.sqrt(sum(v * v for v in r[:3])) for r in rows]
    w = [math.sqrt(sum(v * v for v in r[3:])) for r in rows]
    jerk = [math.sqrt(sum((rows[i][j] - rows[i-1][j]) ** 2 for j in range(3))) * RATE_HZ
            for i in range(1, WINDOW)]
    out = _stats(a) + _stats(w) + _stats(jerk)
    # 旋转不变特征与分段时序统计
    for start, stop in ((0, 50), (50, 100), (100, 150)):
        for signal in (a, w):
            out += _stats(signal[start:stop])[:2]
    first = [sum(r[j] for r in rows[:25]) / 25 for j in range(3)]
    last = [sum(r[j] for r in rows[-25:]) / 25 for j in range(3)]
    norms = math.sqrt(sum(x*x for x in first) * sum(x*x for x in last))
    cosine = sum(x*y for x,y in zip(first,last)) / max(norms, 1e-9)
    out += [max(-1., min(1., cosine)), sum(x < .5 for x in a) / WINDOW,
            sum(x > 2. for x in a) / WINDOW,
            sum(x < 20. for x in w[-50:]) / 50]
    return out


class Forest:
    def __init__(self, path):
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if (data.get("format") != "imu-rf-json-v1" or
                data.get("feature_version") != FEATURE_VERSION or
                data.get("sample_rate_hz") != RATE_HZ or data.get("window_samples") != WINDOW or
                data.get("stride_samples") != STRIDE or data.get("units") != "g,dps"):
            raise ValueError("incompatible model format or preprocessing")
        self.trees = data["trees"]
        self.threshold = float(data["threshold"])
        self.model_id = data["model_id"]
        if not 0 < self.threshold < 1 or not 1 <= len(self.trees) <= 256:
            raise ValueError("invalid model bounds")
        for tree in self.trees:
            n = len(tree["feature"])
            if not 1 <= n <= 65535 or any(len(tree[k]) != n for k in ("left", "right", "threshold", "p")):
                raise ValueError("invalid tree")
            for i, f in enumerate(tree["feature"]):
                if not math.isfinite(tree["p"][i]) or not 0 <= tree["p"][i] <= 1:
                    raise ValueError("invalid leaf score")
                if f >= 0 and (f >= 40 or not math.isfinite(tree["threshold"][i]) or
                               not i < tree["left"][i] < n or not i < tree["right"][i] < n):
                    raise ValueError("invalid tree topology")

    def score_features(self, values):
        if len(values) != 40 or any(not math.isfinite(float(x)) for x in values):
            raise ValueError("expected 40 finite features")
        # float32 特征，与 sklearn 一致

        import struct
        values = [struct.unpack('f', struct.pack('f', x))[0] for x in values]
        total = 0.
        for tree in self.trees:
            i = 0
            while tree["feature"][i] >= 0:
                i = tree["left"][i] if values[tree["feature"][i]] <= tree["threshold"][i] else tree["right"][i]
            total += tree["p"][i]
        return total / len(self.trees)

    def predict(self, samples):
        return self.score_features(features(samples))
