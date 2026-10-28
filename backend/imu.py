"""六轴影子推理；仅单进程、多线程"""
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from fall_ml import Forest, STRIDE, WINDOW

bp = Blueprint("imu", __name__)
_creation_lock = threading.Lock()
_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def validate(payload):
    if not isinstance(payload, dict) or payload.get("version") != 1 or payload.get("units") != "g,dps":
        raise ValueError("expected version=1 and units=g,dps")
    for key, limit in (("device_id", 32), ("boot_id", 16)):
        value = payload.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= limit or not _ID.fullmatch(value):
            raise ValueError(f"invalid {key}")
    for key in ("batch_seq", "sent_up_ms"):
        v = payload.get(key)
        if type(v) is not int or not 0 <= v <= 2**53-1:
            raise ValueError(f"invalid {key}")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not 1 <= len(samples) <= 50:
        raise ValueError("expected 1..50 samples")
    previous = None
    for row in samples:
        if not isinstance(row, list) or len(row) != 7:
            raise ValueError("each row must be [up_ms,ax,ay,az,gx,gy,gz]")
        t = row[0]
        if type(t) is not int or t < 0 or t > payload["sent_up_ms"] or (previous is not None and t <= previous):
            raise ValueError("sample timestamps must increase and precede sent_up_ms")
        previous = t
        for j, v in enumerate(row[1:]):
            bound = 8.01 if j < 3 else 500.01
            if type(v) not in (int, float) or not math.isfinite(v) or abs(v) > bound:
                raise ValueError("invalid axes or units; expected MPU6050 +/-8g, +/-500dps")
    if payload["sent_up_ms"] - samples[0][0] > 2000:
        raise ValueError("batch is stale (more than 2 seconds)")
    # 仅保留协议字段
    return {k: payload[k] for k in ("version", "units", "device_id", "boot_id", "batch_seq", "sent_up_ms", "samples")}


class ImuService:
    def __init__(self, db_path, model_path, record_raw=False):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.record_raw = record_raw
        self.lock = threading.Lock()
        self.streams = {}
        self.model = None
        self.model_error = None
        try:
            self.model = Forest(model_path)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            self.model_error = f"model unavailable: {type(exc).__name__}"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS imu_batches (
                id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, boot_id TEXT NOT NULL,
                batch_seq INTEGER NOT NULL, digest TEXT NOT NULL, received REAL NOT NULL,
                samples_json TEXT, result_json TEXT NOT NULL,
                UNIQUE(device_id,boot_id,batch_seq))""")
            db.execute("CREATE INDEX IF NOT EXISTS imu_device_id ON imu_batches(device_id,id)")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def accept(self, payload):
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True, allow_nan=False)
        digest = hashlib.sha256(body.encode()).hexdigest()
        key = (payload["device_id"], payload["boot_id"])
        with self.lock, self.connect() as db:
            existing = db.execute("SELECT digest,result_json FROM imu_batches WHERE device_id=? AND boot_id=? AND batch_seq=?",
                                  (*key, payload["batch_seq"])).fetchone()
            if existing:
                if existing["digest"] != digest:
                    return {"ok": False, "error": "batch_seq conflict"}, 409
                return {**json.loads(existing["result_json"]), "duplicate": True}, 200
            last = db.execute("SELECT MAX(batch_seq) AS seq FROM imu_batches WHERE device_id=? AND boot_id=?", key).fetchone()["seq"]
            if last is not None and payload["batch_seq"] <= last:
                return {"ok": False, "error": "out-of-order batch"}, 409
            old = self.streams.get(key)
            state = dict(old) if old else {"rows": deque(maxlen=WINDOW), "since": 0,
                                           "hits": 0, "last_event": -100000, "gaps": 0,
                                           "last_t": None, "last_seq": None, "latest": None}
            state["rows"] = deque(state["rows"], maxlen=WINDOW)
            if state["last_seq"] is not None and payload["batch_seq"] != state["last_seq"] + 1:
                state["rows"].clear(); state["hits"] = state["since"] = 0; state["gaps"] += 1
                state["latest"] = None
            predictions = []
            for row in payload["samples"]:
                t = row[0]
                if state["last_t"] is not None:
                    dt = t - state["last_t"]
                    if dt <= 0:
                        return {"ok": False, "error": "sample timestamps overlap prior batch"}, 409
                    if not 10 <= dt <= 40:
                        state["rows"].clear(); state["hits"] = state["since"] = 0
                        state["gaps"] += 1; state["latest"] = None
                state["last_t"] = t
                state["rows"].append(row)
                state["since"] += 1
                if len(state["rows"]) < WINDOW or state["since"] < STRIDE or not self.model:
                    continue
                state["since"] = 0
                span = t - state["rows"][0][0]
                if abs(span - (WINDOW-1)*20) > 100:
                    # 非连续 50Hz 数据重置窗口
                    state["hits"] = 0; state["latest"] = None
                    continue
                score = self.model.predict([r[1:] for r in state["rows"]])
                high = score >= self.model.threshold
                state["hits"] = state["hits"] + 1 if high else 0
                event = state["hits"] >= 2 and t - state["last_event"] >= 8000
                if event:
                    state["last_event"] = t; state["hits"] = 0
                prediction = {"up_ms": t, "score": score, "threshold": self.model.threshold,
                              "above_threshold": high, "shadow_event": event}
                state["latest"] = prediction
                predictions.append(prediction)
            state["last_seq"] = payload["batch_seq"]
            result = {"ok": True, "mode": "shadow", "device_id": key[0], "boot_id": key[1],
                      "batch_seq": payload["batch_seq"], "model_id": self.model.model_id if self.model else None,
                      "model_error": self.model_error, "raw_recording": self.record_raw,
                      "window_samples": len(state["rows"]), "gap_resets": state["gaps"],
                      "status": "model_unavailable" if not self.model else "ready" if state["latest"] else "warming_up",
                      "latest": state["latest"], "predictions": predictions, "received_at": time.time()}
            db.execute("INSERT INTO imu_batches(device_id,boot_id,batch_seq,digest,received,samples_json,result_json) VALUES(?,?,?,?,?,?,?)",
                       (*key, payload["batch_seq"], digest, result["received_at"],
                        body if self.record_raw else None, json.dumps(result, allow_nan=False)))
            db.commit()
            if len(self.streams) >= 32 and key not in self.streams:
                self.streams.pop(next(iter(self.streams)))
            self.streams[key] = state
            return result, 200

    def status(self, device_id=None):
        with self.connect() as db:
            row = db.execute("SELECT result_json FROM imu_batches " +
                             ("WHERE device_id=? " if device_id else "") + "ORDER BY id DESC LIMIT 1",
                             (device_id,) if device_id else ()).fetchone()
        result = json.loads(row[0]) if row else {"ok": True, "mode": "shadow", "status": "no_data", "latest": None}
        result["model_id"] = self.model.model_id if self.model else None
        result["model_error"] = self.model_error
        result["raw_recording"] = self.record_raw
        result["online"] = bool(row and time.time() - result["received_at"] <= 5)
        # 重启清空窗口，保留历史结果
        result["window_in_memory"] = (result.get("device_id"), result.get("boot_id")) in self.streams
        result["server_time"] = time.time()
        return result

    def events(self, device_id=None, after=None, before=None, limit=20):
        """模型事件；游标含无报警批次"""
        with self.connect() as db:
            high = db.execute("SELECT COALESCE(MAX(id),0) FROM imu_batches").fetchone()[0]
            reset = after is not None and after > high
            if reset:
                after = None
            clauses = ["b.id <= ?", "EXISTS (SELECT 1 FROM json_each(b.result_json, '$.predictions') p "
                       "WHERE json_extract(p.value,'$.shadow_event') = 1)"]
            args = [high]
            if device_id:
                clauses.append("b.device_id = ?"); args.append(device_id)
            if after is not None:
                clauses.append("b.id > ?"); args.append(after)
            if before is not None:
                clauses.append("b.id < ?"); args.append(before)
            ascending = after is not None
            rows = db.execute("SELECT b.id,b.result_json FROM imu_batches b WHERE " +
                              " AND ".join(clauses) + " ORDER BY b.id " +
                              ("ASC" if ascending else "DESC") + " LIMIT ?", (*args, limit+1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        events = []
        for row in rows:
            result = json.loads(row["result_json"])
            for prediction in result.get("predictions", []):
                if not prediction.get("shadow_event"):
                    continue
                events.append({"id": f'{result["device_id"]}:{result["boot_id"]}:{prediction["up_ms"]}',
                               "batch_id": row["id"], "device_id": result["device_id"],
                               "boot_id": result["boot_id"], "model_id": result["model_id"],
                               "received_at": result["received_at"], "mode": "shadow", **prediction})
        events.sort(key=lambda e: (e["batch_id"], e["up_ms"]), reverse=not ascending)
        return {"ok": True, "events": events, "cursor": rows[-1]["id"] if ascending and more else high,
                "has_more": more, "next_before": min((row["id"] for row in rows), default=None),
                "reset": reset, "server_time": time.time()}


def _service():
    with _creation_lock:
        if "imu_service" not in current_app.extensions:
            current_app.extensions["imu_service"] = ImuService(current_app.config["IMU_DB_PATH"],
                                                                 current_app.config["FALL_MODEL_PATH"],
                                                                 current_app.config["IMU_RECORD_RAW"])
    return current_app.extensions["imu_service"]


@bp.route("/api/imu", methods=["POST"])
def upload():
    request.max_content_length = 32 * 1024
    try:
        payload = validate(request.get_json(silent=True))
        result, status = _service().accept(payload)
        return jsonify(result), status
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except sqlite3.Error:
        current_app.logger.exception("IMU storage failure")
        return jsonify(ok=False, error="IMU storage unavailable"), 503


@bp.route("/api/ml/status")
def model_status():
    device_id = request.args.get("device_id")
    if device_id is not None and (not 1 <= len(device_id) <= 32 or not _ID.fullmatch(device_id)):
        return jsonify(ok=False, error="invalid device_id"), 400
    try:
        return jsonify(_service().status(device_id))
    except sqlite3.Error:
        return jsonify(ok=False, error="IMU storage unavailable"), 503


@bp.route("/api/ml/events")
def model_events():
    device_id = request.args.get("device_id")
    if device_id is not None and (not 1 <= len(device_id) <= 32 or not _ID.fullmatch(device_id)):
        return jsonify(ok=False, error="invalid device_id"), 400
    values = {}
    for key in ("after", "before", "limit"):
        text = request.args.get(key)
        if text is not None:
            if not text.isascii() or not text.isdigit() or len(text) > 16:
                return jsonify(ok=False, error=f"invalid {key}"), 400
            values[key] = int(text)
            if values[key] > 2**53-1:
                return jsonify(ok=False, error=f"invalid {key}"), 400
    if "after" in values and "before" in values:
        return jsonify(ok=False, error="after and before are mutually exclusive"), 400
    if "limit" in values and not 1 <= values["limit"] <= 100:
        return jsonify(ok=False, error="limit must be 1..100"), 400
    try:
        response = jsonify(_service().events(device_id=device_id, **values))
        response.headers["Cache-Control"] = "no-store"
        return response
    except sqlite3.Error:
        return jsonify(ok=False, error="IMU storage unavailable"), 503
