"""网页模拟服务：隔离 SQLite、模拟 MySQL"""
import json
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import Mock


def main():
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root / "backend/.venv/Lib/site-packages"), str(root / "backend")]
    from flask import request, jsonify
    from app import app, latest
    from imu import _service
    import db
    import queries

    run = root / ".build/model-alerts-ui" / ("run-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    app.config.update(TESTING=True, IMU_DB_PATH=str(run / "imu.sqlite3"),
                      PHOTO_DB_PATH=str(run / "photos.sqlite3"))
    db.get_pool = lambda: Mock(connection=lambda: Mock())
    queries.recent_events = lambda *a, **k: []
    queries.event_count = lambda *a, **k: 0
    queries.stats_summary = lambda *a, **k: dict(today=0, total=0, last=None)
    queries.page_events = lambda *a, **k: dict(items=[], total=0, page=1, per_page=50)
    queries.samples_range = lambda *a, **k: []

    @app.before_request
    def fixture_status():
        latest.update(online=True, device_id="esp32-preview", boot_id="preview", up=100000,
                      m=1.02, ang=3.5, air=15., state="ARMED", alarm=0, seq=0, last_seen=time.time())
        if request.endpoint == "imu.model_status":
            result = _service().status(request.args.get("device_id"))
            # 模拟心跳，不代表真实设备
            result["online"] = bool(result.get("device_id"))
            return jsonify(result)

    def append_result(device, event):
        service = _service()
        with service.connect() as conn:
            seq = conn.execute("SELECT COALESCE(MAX(batch_seq),0) FROM imu_batches").fetchone()[0] + 1
            p = dict(up_ms=seq*8000, score=.954 if event else .1, threshold=.8,
                     shadow_event=event, above_threshold=event)
            result = dict(ok=True, device_id=device, boot_id="preview", batch_seq=seq,
                          model_id="ui-test-fixture", model_error=None, received_at=time.time(),
                          mode="shadow", raw_recording=False, window_samples=150, gap_resets=0,
                          status="ready", latest=p, predictions=[p])
            conn.execute("INSERT INTO imu_batches(device_id,boot_id,batch_seq,digest,received,result_json) "
                         "VALUES(?,?,?,?,?,?)", (device, "preview", seq, "fixture", result["received_at"], json.dumps(result)))
            service.streams[(device, "preview")] = {}

    @app.post("/_test/seed")
    def seed():
        payload = request.get_json()
        for _ in range(payload.get("count", 1)):
            append_result(payload.get("device", "esp32-preview"), True)
        return jsonify(ok=True)

    @app.post("/_test/normal")
    def normal():
        append_result("esp32-preview", False)
        return jsonify(ok=True)

    print("UI fixture server: isolated SQLite, mocked MySQL, no physical device; 127.0.0.1:5070", flush=True)
    app.run("127.0.0.1", 5070, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
