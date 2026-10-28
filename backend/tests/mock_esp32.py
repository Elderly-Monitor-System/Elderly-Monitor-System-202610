#!/usr/bin/env python3
# 设备上报与幂等测试
import argparse
import json
import random
import sys
import time
import urllib.request

# Windows 控制台使用 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def post(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status
    except Exception as e:
        return f"ERR:{e.__class__.__name__}"


def get(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:5000")
    ap.add_argument("--seconds", type=float, default=8)
    ap.add_argument("--fall-after", type=float, default=3)
    ap.add_argument("--alarm-window", type=float, default=2.5)
    ap.add_argument("--air-bad-after", type=float, default=4)
    ap.add_argument("--air-window", type=float, default=2.5)
    ap.add_argument("--device", default="esp32-mock")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    base = args.url.rstrip("/") + "/api/ingest"
    boot = "mockboot0001"
    random.seed(7)

    t0 = time.time()
    alarms = [("FALL", 1000), ("TILT", 2000)]   # (etype, seq)，保持上升序
    ei = 0
    last_seq = None
    last_etype = None
    alarm_until = 0.0
    fail_count = 0
    air_event_sent = False

    while time.time() - t0 < args.seconds:
        up = int((time.time() - t0) * 1000)
        now = time.time() - t0

        # 报警窗口内置 alarm=1
        in_alarm = now < alarm_until
        st = "TILT_ALARM" if in_alarm else "ARMED"

        # 模拟空气持续超标窗口
        in_air_bad = args.air_bad_after <= now < args.air_bad_after + args.air_window
        if in_air_bad:
            air = round(random.uniform(68, 84), 1)
            abad = 1
        else:
            air = round(random.uniform(4, 18), 1)
            abad = 0

        m = round(1.0 + random.uniform(-0.03, 0.03), 3)
        ang = round(abs(random.gauss(10, 3)), 1)
        payload = {
            "t": "state", "device_id": args.device, "boot_id": boot,
            "up": up, "m": m, "ang": ang,
            "state": st, "alarm": 1 if in_alarm else 0, "seq": last_seq or 0,
            "air": air, "abad": abad,
        }
        code = post(base, payload)
        if code != 200:
            fail_count += 1
        if args.verbose:
            print(f"[{up:>7}ms] state {st:10s} air={air:5.1f} abad={abad} → {code}")

        # 注入 FALL/TILT 事件
        if ei < len(alarms) and now >= args.fall_after:
            etype, seq = alarms[ei]
            ei += 1
            ev = {"t": "event", "device_id": args.device, "boot_id": boot,
                  "seq": seq, "etype": etype, "up": up}
            print(f"[{up:>7}ms] EVENT {etype} seq={seq} → {post(base, ev)}")
            last_seq, last_etype = seq, etype
            alarm_until = now + args.alarm_window

        # 超标起点注入 AIR 事件
        if in_air_bad and not air_event_sent:
            air_event_sent = True
            seq = 3000
            ev = {"t": "event", "device_id": args.device, "boot_id": boot,
                  "seq": seq, "etype": "AIR", "up": up}
            print(f"[{up:>7}ms] EVENT AIR seq={seq} → {post(base, ev)}")
            last_seq, last_etype = seq, "AIR"

        time.sleep(0.5)

    # 幂等验证
    before = get(args.url + "/api/events?per_page=100").get("total", 0)
    if last_seq is not None:
        dup = {"t": "event", "device_id": args.device, "boot_id": boot,
               "seq": last_seq, "etype": last_etype or "FALL", "up": 0}
        post(base, dup)
    after = get(args.url + "/api/events?per_page=100").get("total", 0)
    idem_ok = (before == after)


    events = get(args.url + "/api/events?per_page=100")
    state = get(args.url + "/api/state")
    samples = get(args.url + "/api/samples")
    print("\n=== 结果 ===")
    print(f"POST 失败次数: {fail_count}")
    print(f"事件总数: {events.get('total')}   幂等(重复上报不新增): {'PASS' if idem_ok else 'FAIL'}")
    for e in events.get("items", []):
        print(f"   #{e['id']} {e['etype']:5s} seq={e['seq']} {e['created_at']} up_ms={e['up_ms']}")
    print(f"最新状态: {state.get('state')}  m={state.get('m')}  air={state.get('air')}  "
          f"abad={state.get('abad')}  online={state.get('online')}")
    air_rows = [s.get("air") for s in samples.get("items", [])[-5:]]
    print(f"采样点数: {samples.get('total')}   最近5条 air: {air_rows}")
    ok = fail_count == 0 and idem_ok and events.get("total", 0) >= 3
    print("链路验证:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
