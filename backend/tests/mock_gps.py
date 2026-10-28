#!/usr/bin/env python3
# GPS 上报与查询测试
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return f"ERR:{e.__class__.__name__}"


def get(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:5000")
    ap.add_argument("--device", default="esp32-gps-mock")
    ap.add_argument("--points", type=int, default=6)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    ingest_url = args.url.rstrip("/") + "/api/ingest"
    boot = "mockgps0001"
    fails = 0
    now = datetime.now(timezone.utc)
    lng = 116.3900   # 北京天安门一带，逐点移动
    lat = 39.9000

    print(f"上报 {args.points} 条 t=location ...")
    for k in range(args.points):
        gt = (now - timedelta(seconds=args.points - 1 - k)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "t": "location", "device_id": args.device, "boot_id": boot,
            "seq": k + 1, "up": (k + 1) * 1000,
            "lat": round(lat + k * 0.001, 6),
            "lng": round(lng + k * 0.001, 6),
            "gps_time": gt, "sats": 8, "hdop": 1.2,
        }
        code = post(ingest_url, payload)
        if code != 200:
            fails += 1
        if args.verbose:
            print(f"  +{payload['lng']},{payload['lat']}  gps_time={gt} → {code}")
        time.sleep(0.15)

    # 验证最新点
    latest = get(args.url + "/api/location?device_id=" + args.device)
    ok1 = latest.get("found") is True and latest.get("device_id") == args.device
    print(f"最新点 found={ok1}  {latest.get('longitude')},{latest.get('latitude')}  gps_time={latest.get('gps_time')}")

    # 验证当天轨迹
    day = latest["gps_time"][:10]
    track = get(args.url + f"/api/locations/date?device_id={args.device}&date_str={day}")
    ok2 = track.get("count", 0) >= args.points
    print(f"当天轨迹({day}) count={track.get('count')}  ≥{args.points}: {ok2}")

    # 最近点时间差应为 0
    tstr = latest["gps_time"][:19]
    nearest = get(args.url + f"/api/locations/nearest?device_id={args.device}&date_str={day}&time_str={tstr[11:]}")
    ok3 = nearest.get("found") is True and nearest.get("difference_seconds") == 0
    print(f"最近点查询 {tstr} → found={nearest.get('found')} 相差={nearest.get('difference_seconds')}s: {ok3}")

    # 拒绝非法坐标和时间
    bad1 = post(ingest_url, {"t": "location", "device_id": args.device, "boot_id": boot,
                             "lat": 999, "lng": 0, "gps_time": gt})
    bad2 = post(ingest_url, {"t": "location", "device_id": args.device, "boot_id": boot,
                             "lat": 39.9, "lng": 116.3, "gps_time": "not-a-time"})
    ok4 = bad1 == 400 and bad2 == 400
    print(f"负例 非法经纬度→{bad1} 非法时间→{bad2}: {ok4}")

    ok = fails == 0 and ok1 and ok2 and ok3 and ok4
    print("\nGPS 链路验证:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
