"""六轴接口测试：隔离 SQLite、导出模型"""
import json
import math
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import app
from fall_ml import Forest, features
from imu import ImuService, validate

MODEL = Path(__file__).resolve().parents[1] / "models/sisfall_rf_v1.json"


def packet(seq=1, start=1000, device="esp32-test", boot="boot1", count=25):
    return {"version": 1, "units": "g,dps", "device_id": device, "boot_id": boot,
            "batch_seq": seq, "sent_up_ms": start + (count-1)*20,
            "samples": [[start+i*20, 0., 0., 1., 0., 0., 0.] for i in range(count)]}


class ImuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = dict(app.config)
        self.previous_service = app.extensions.pop("imu_service", None)
        app.config.update(TESTING=True, IMU_DB_PATH=str(Path(self.tmp.name)/"imu.sqlite3"),
                          FALL_MODEL_PATH=str(MODEL), IMU_RECORD_RAW=False)
        self.guard = patch("app.db_mod.get_pool", side_effect=AssertionError("IMU must not access MySQL"))
        self.guard.start()
        self.client = app.test_client()

    def tearDown(self):
        self.guard.stop()
        app.extensions.pop("imu_service", None)
        if self.previous_service is not None:
            app.extensions["imu_service"] = self.previous_service
        app.config.clear(); app.config.update(self.old)
        self.tmp.cleanup()

    def send(self, **kwargs):
        response = self.client.post("/api/imu", json=packet(**kwargs))
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_real_model_window_warmup_and_stride(self):
        for seq in range(1, 6):
            self.assertEqual(self.send(seq=seq, start=1000+(seq-1)*500)["status"], "warming_up")
        first = self.send(seq=6, start=3500)
        self.assertEqual(first["status"], "ready")
        self.assertEqual(len(first["predictions"]), 1)
        self.assertLess(first["latest"]["score"], first["latest"]["threshold"])
        self.assertFalse(first["latest"]["shadow_event"])
        self.assertEqual(self.send(seq=7, start=4000)["latest"]["up_ms"], 4480)
        self.assertEqual(first["mode"], "shadow")

    def test_invalid_protocol_and_ranges(self):
        for change in ({"units": "m/s2,rad/s"}, {"version": 2}, {"batch_seq": True},
                       {"device_id": "a"*33}, {"boot_id": "bad/boot"}, {"samples": []}):
            with self.subTest(change=change):
                p = packet(); p.update(change)
                self.assertEqual(self.client.post("/api/imu", json=p).status_code, 400)
        for value in (math.nan, math.inf, 9., "1", True):
            p = packet(); p["samples"][0][1] = value
            self.assertEqual(self.client.post("/api/imu", json=p).status_code, 400)

    def test_bad_timestamps_and_stale(self):
        for change in ("repeat", "future", "stale"):
            p = packet()
            if change == "repeat": p["samples"][1][0] = p["samples"][0][0]
            if change == "future": p["sent_up_ms"] = 0
            if change == "stale": p["sent_up_ms"] += 5000
            self.assertEqual(self.client.post("/api/imu", json=p).status_code, 400)

    def test_duplicate_conflict_and_out_of_order(self):
        self.send()
        self.assertTrue(self.send()["duplicate"])
        p = packet(); p["samples"][0][1] = .1
        self.assertEqual(self.client.post("/api/imu", json=p).status_code, 409)
        self.send(seq=3, start=2000)
        self.assertEqual(self.client.post("/api/imu", json=packet(seq=2, start=1500)).status_code, 409)

    def test_gap_and_missing_batch_clear_window(self):
        for seq in range(1, 7): self.send(seq=seq, start=1000+(seq-1)*500)
        result = self.send(seq=8, start=4500)
        self.assertEqual(result["status"], "warming_up")
        self.assertEqual(result["window_samples"], 25)
        self.assertGreater(result["gap_resets"], 0)
        result = self.send(seq=9, start=6000)
        self.assertEqual(result["window_samples"], 25)
        self.assertIsNone(result["latest"])

    def test_timestamp_overlap_rejected_without_mutating_stream(self):
        self.send()
        self.assertEqual(self.client.post("/api/imu", json=packet(seq=2, start=1400)).status_code, 409)
        self.assertEqual(self.send(seq=2, start=1500)["window_samples"], 50)

    def test_separate_devices_and_reboots(self):
        self.send()
        self.assertEqual(self.send(seq=2, start=1500)["window_samples"], 50)
        self.assertEqual(self.send(boot="boot2")["window_samples"], 25)
        self.assertEqual(self.send(device="esp32-other")["window_samples"], 25)
        self.assertEqual(self.client.get("/api/ml/status?device_id=esp32-test").json["boot_id"], "boot2")

    def test_model_failure_does_not_block_ingestion(self):
        app.config["FALL_MODEL_PATH"] = str(Path(self.tmp.name)/"missing.json")
        result = self.send()
        self.assertEqual(result["status"], "model_unavailable")
        self.assertIsNone(result["model_id"])
        self.assertIsNone(result["latest"])

    def test_payload_limit_and_invalid_json(self):
        self.assertEqual(self.client.post("/api/imu", data="{"+" "*40000,
                                         content_type="application/json").status_code, 413)
        self.assertEqual(self.client.post("/api/imu", json=[1]).status_code, 400)

    def test_raw_recording_opt_in_and_restart(self):
        app.config["IMU_RECORD_RAW"] = True
        self.send()
        with closing(sqlite3.connect(app.config["IMU_DB_PATH"])) as db:
            saved = json.loads(db.execute("SELECT samples_json FROM imu_batches").fetchone()[0])
        self.assertEqual(len(saved["samples"]), 25)
        app.extensions.pop("imu_service")
        status = self.client.get("/api/ml/status").json
        self.assertFalse(status["window_in_memory"])
        self.assertTrue(self.send()["duplicate"])
        self.assertEqual(self.send(seq=2, start=1500)["window_samples"], 25)

    def test_no_raw_recording_by_default(self):
        self.send()
        with closing(sqlite3.connect(app.config["IMU_DB_PATH"])) as db:
            self.assertIsNone(db.execute("SELECT samples_json FROM imu_batches").fetchone()[0])

    def test_concurrent_duplicate_is_idempotent(self):
        self.send()
        def call(_):
            with app.test_client() as client:
                return client.post("/api/imu", json=packet(seq=2, start=1500)).json
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(call, range(8)))
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(sum(bool(r.get("duplicate")) for r in results), 7)

    def test_shadow_two_hits_cooldown_and_no_alarm_side_effect(self):
        self.send()
        service = app.extensions["imu_service"]
        with patch.object(service.model, "predict", return_value=1.):
            events = []
            for seq in range(2, 25):
                result = self.send(seq=seq, start=1000+(seq-1)*500)
                events += [p["up_ms"] for p in result["predictions"] if p["shadow_event"]]
        self.assertGreaterEqual(len(events), 2)
        self.assertGreaterEqual(events[1] - events[0], 8000)
        self.assertEqual(result["mode"], "shadow")

    def test_status_without_data_and_filter(self):
        self.assertEqual(self.client.get("/api/ml/status").json["status"], "no_data")
        self.assertEqual(self.client.get("/api/ml/status?device_id=../").status_code, 400)

    def test_model_events_survive_normal_batches_and_restart(self):
        self.send()
        service = app.extensions["imu_service"]
        with patch.object(service.model, "predict", return_value=1.):
            for seq in range(2, 8): self.send(seq=seq, start=1000+(seq-1)*500)
        self.send(seq=8, start=4500)  # 新分数不覆盖历史事件
        self.assertFalse(self.client.get("/api/ml/status").json["latest"]["shadow_event"])
        response = self.client.get("/api/ml/events").json
        self.assertEqual(len(response["events"]), 1)
        event = response["events"][0]
        self.assertEqual(event["id"], "esp32-test:boot1:4480")
        self.assertEqual(event["mode"], "shadow")
        self.assertEqual(self.client.get("/api/ml/events?after="+str(response["cursor"])).json["events"], [])
        app.extensions.pop("imu_service")
        self.assertEqual(self.client.get("/api/ml/events").json["events"][0]["id"], event["id"])

    def _event_fixtures(self, count=7):
        """分页模拟记录，非模型实测"""
        self.client.get("/api/ml/status")
        service = app.extensions["imu_service"]
        with service.connect() as db:
            for seq in range(1, count+1):
                device = "device-a" if seq % 2 else "device-b"
                p = {"up_ms": seq*8000, "score": .95, "threshold": .8, "shadow_event": True, "above_threshold": True}
                result = {"device_id": device, "boot_id": "fixture", "model_id": "test-fixture",
                          "received_at": 1700000000+seq, "predictions": [p]}
                db.execute("INSERT INTO imu_batches(device_id,boot_id,batch_seq,digest,received,result_json) VALUES(?,?,?,?,?,?)",
                           (device, "fixture", seq, "fixture", result["received_at"], json.dumps(result)))

    def test_events_incremental_cursor_and_device_filter(self):
        self._event_fixtures()
        first = self.client.get("/api/ml/events?after=0&limit=2").json
        self.assertEqual([e["batch_id"] for e in first["events"]], [1,2])
        self.assertTrue(first["has_more"])
        second = self.client.get("/api/ml/events?after="+str(first["cursor"])+"&limit=2").json
        self.assertEqual([e["batch_id"] for e in second["events"]], [3,4])
        filtered = self.client.get("/api/ml/events?device_id=device-a&after=0").json
        self.assertEqual(len(filtered["events"]), 4)
        self.assertTrue(all(e["device_id"] == "device-a" for e in filtered["events"]))
        self.assertEqual(filtered["cursor"], 7)

    def test_events_history_pagination_and_database_reset(self):
        self._event_fixtures()
        first = self.client.get("/api/ml/events?limit=2").json
        self.assertEqual([e["batch_id"] for e in first["events"]], [7,6])
        older = self.client.get("/api/ml/events?before="+str(first["next_before"])+"&limit=2").json
        self.assertEqual([e["batch_id"] for e in older["events"]], [5,4])
        reset = self.client.get("/api/ml/events?after=100").json
        self.assertTrue(reset["reset"])
        self.assertEqual(reset["cursor"], 7)
        self.assertEqual(len(reset["events"]), 7)

    def test_events_validation_empty_and_no_cache(self):
        response = self.client.get("/api/ml/events")
        self.assertEqual(response.json["events"], [])
        self.assertEqual(response.json["cursor"], 0)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for args in ("after=-1", "after=x", "after=1&before=2", "before=1.5", "limit=0", "limit=101",
                     "device_id=bad/", "after=9999999999999999"):
            self.assertEqual(self.client.get("/api/ml/events?"+args).status_code, 400, args)


class FeatureTests(unittest.TestCase):
    def test_physical_features_and_rotation(self):
        a = [[.2*math.sin(i/7), math.cos(i/9)*.1, 1., 2., 3., 4.] for i in range(150)]
        b = [[r[2], r[0], r[1], r[5], r[3], r[4]] for r in a]
        self.assertEqual(len(features(a)), 40)
        for x,y in zip(features(a), features(b)):
            self.assertAlmostEqual(x,y,places=10)

    def test_invalid_features_and_model(self):
        with self.assertRaises(ValueError): features([[0]*6]*149)
        with self.assertRaises(ValueError): features([[float("nan")]*6]*150)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/"invalid.json"; p.write_text('{}')
            with self.assertRaises(ValueError): Forest(p)


if __name__ == "__main__":
    unittest.main()
