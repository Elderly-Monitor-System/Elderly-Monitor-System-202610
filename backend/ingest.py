# 状态、事件和定位接收
import re
import time
from datetime import datetime, timedelta, timezone

import queries

VALID_STATES = {"ARMED", "TILT_WAIT", "TILT_ALARM", "FALL_IMPACT", "FALL_STILL"}
_SAFE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# GPS UTC 转北京时间
_BJ_TZ = timezone(timedelta(hours=8))


def _norm_device_id(v):
    if not isinstance(v, str):
        return None
    s = v.strip()[:32]
    return s if _SAFE_RE.match(s) else None


def _norm_boot_id(v):
    if not isinstance(v, str):
        return None
    s = v.strip()[:16]
    return s if _SAFE_RE.match(s) else None


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _to_float_range(v, lo, hi):
    """v → float 且落在 [lo, hi]，否则 None"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if lo <= f <= hi:
        return f
    return None


def _parse_gps_time_to_beijing(v):
    """ISO 转北京时间；缺省 UTC，非法为 None"""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_BJ_TZ).replace(tzinfo=None, microsecond=0)


def handle(conn, payload, latest, lock):
    """校验入库；返回是否接受"""
    if not isinstance(payload, dict):
        return False
    t = payload.get("t")
    if t not in ("state", "event", "location"):
        return False

    device_id = _norm_device_id(payload.get("device_id"))
    boot_id = _norm_boot_id(payload.get("boot_id"))
    if not device_id or not boot_id:
        return False

    # 登记设备并刷新在线时间
    queries.upsert_device(conn, device_id, mac=payload.get("mac"))

    if t == "event":
        etype = payload.get("etype")
        if etype not in ("TILT", "FALL", "AIR"):
            return False
        seq = _to_int(payload.get("seq"))
        up_ms = _to_int(payload.get("up"))
        queries.insert_event(conn, device_id, boot_id, seq, etype, up_ms)
    elif t == "location":
        # 定位输入：WGS84、UTC
        lat = _to_float_range(payload.get("lat"), -90.0, 90.0)
        lng = _to_float_range(payload.get("lng"), -180.0, 180.0)
        if lat is None or lng is None:
            return False
        bj_time = _parse_gps_time_to_beijing(payload.get("gps_time"))
        if bj_time is None:
            return False
        sats = _to_int(payload.get("sats"), -1)
        hdop = _to_float_range(payload.get("hdop"), 0.0, 99.0)
        queries.insert_location(
            conn, device_id, boot_id,
            seq=_to_int(payload.get("seq")),
            up_ms=_to_int(payload.get("up")),
            lat=lat, lng=lng, gps_time=bj_time,
            sats=sats if sats >= 0 else None,
            hdop=hdop,
        )
    else:
        m = float(payload.get("m", 0))
        ang = float(payload.get("ang", 0))
        air = min(1000.0, max(0.0, float(payload.get("air", 0))))  # 0~100 空气污染指数
        abad = 1 if payload.get("abad") else 0                     # 持续超标标志
        alarm = 1 if payload.get("alarm") else 0
        up = _to_int(payload.get("up"))
        seq = _to_int(payload.get("seq"))
        state = payload.get("state")
        if state not in VALID_STATES:
            state = "UNKNOWN"
        queries.insert_sample_ratelimited(
            conn, device_id, boot_id, up, m, ang, air, state, alarm
        )

    # 刷新状态缓存与在线时间
    with lock:
        latest["online"] = True
        latest["device_id"] = device_id
        latest["boot_id"] = boot_id
        latest["last_seen"] = time.time()
        if t == "state":
            latest.update({
                "up": up, "m": m, "ang": ang,
                "state": state, "alarm": alarm, "seq": seq,
                "air": air, "abad": abad,
            })
    return True
