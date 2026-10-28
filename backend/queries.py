# MySQL 查询与幂等写入
import threading
import time
from datetime import date, datetime, timedelta

import config

# 按设备及启动会话记录采样时间
_last_sample = {}
_ss_lock = threading.Lock()


def upsert_device(conn, device_id, mac=None):
    with conn.cursor() as cur:
        if mac:
            cur.execute(
                "INSERT INTO devices (device_id, mac, last_seen_at) "
                "VALUES (%s, %s, NOW(3)) "
                "ON DUPLICATE KEY UPDATE mac=%s, last_seen_at=NOW(3)",
                (device_id, mac, mac),
            )
        else:
            cur.execute(
                "INSERT INTO devices (device_id, last_seen_at) "
                "VALUES (%s, NOW(3)) "
                "ON DUPLICATE KEY UPDATE last_seen_at=NOW(3)",
                (device_id,),
            )


def _fmt_dt(dt):
    """datetime 转字符串"""
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def insert_event(conn, device_id, boot_id, seq, etype, up_ms):
    """幂等键：(device_id, boot_id, seq)"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (device_id, boot_id, seq, etype, up_ms) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE id=id",
            (device_id, boot_id, seq, etype, up_ms),
        )


def recent_events(conn, device_id=None, limit=20):
    """最近 limit 条，旧→新"""
    sql = "SELECT id, device_id, boot_id, seq, etype, up_ms, created_at FROM events"
    params = []
    if device_id:
        sql += " WHERE device_id=%s"
        params.append(device_id)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = list(cur.fetchall())     # 统一返回列表
    for r in rows:
        r["created_at"] = _fmt_dt(r["created_at"])
    rows.reverse()
    return rows


def event_count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM events")
        return cur.fetchone()["c"]


def stats_summary(conn):
    """报警统计：今日、累计、最近时间"""
    today_start = datetime.combine(date.today(), datetime.min.time())
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM events WHERE created_at >= %s",
            (today_start,),
        )
        today = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM events")
        total = cur.fetchone()["c"]
        cur.execute("SELECT MAX(created_at) AS last FROM events")
        last = cur.fetchone()["last"]
    return {"today": today, "total": total, "last": _fmt_dt(last)}


def page_events(conn, page, per_page, device_id=None, etype=None):
    """事件分页，旧→新，可按 etype 筛选"""
    offset = (page - 1) * per_page
    conds, params = [], []
    if device_id:
        conds.append("device_id=%s")
        params.append(device_id)
    if etype in ("TILT", "FALL", "AIR"):
        conds.append("etype=%s")
        params.append(etype)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM events {where}", tuple(params))
        total = cur.fetchone()["c"]
        sql = "SELECT id, device_id, boot_id, seq, etype, up_ms, created_at FROM events"
        sql += (" WHERE " + " AND ".join(conds)) if conds else ""
        sql += " ORDER BY id DESC LIMIT %s OFFSET %s"
        cur.execute(sql, tuple(params + [per_page, offset]))
        rows = list(cur.fetchall())     # 统一返回列表
    for r in rows:
        r["created_at"] = _fmt_dt(r["created_at"])
    rows.reverse()
    return {"total": total, "page": page, "per_page": per_page, "items": rows}


def insert_sample_ratelimited(conn, device_id, boot_id, up_ms, m, ang, air, state, alarm):
    """采样限流；关键状态或报警变化时强制入库"""
    force = state in config.SAMPLE_FORCE_STATES
    key = (device_id, boot_id)
    now = time.monotonic()
    with _ss_lock:
        last = _last_sample.get(key)
        if not force and last is not None and (now - last) < (1.0 / config.SAMPLE_RATE_HZ):
            return False
        _last_sample[key] = now
    with conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO samples "
            "(device_id, boot_id, up_ms, m, ang, air, state, alarm) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (device_id, boot_id, up_ms, m, ang, air, state, alarm),
        )
    return True


def samples_range(conn, device_id=None, since=None, until=None, max_points=300):
    """按 Unix 秒查询趋势并抽稀，保留最新点"""
    conds, params = [], []
    if device_id:
        conds.append("device_id=%s")
        params.append(device_id)
    if since is not None:
        conds.append("created_at >= FROM_UNIXTIME(%s)")
        params.append(since)
    if until is not None:
        conds.append("created_at <= FROM_UNIXTIME(%s)")
        params.append(until)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params = tuple(params)

    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM samples {where}", params)
        total = cur.fetchone()["c"]
        cur.execute(
            f"SELECT id, m, ang, air, state, alarm, "
            f"UNIX_TIMESTAMP(created_at) AS ts "
            f"FROM samples {where} ORDER BY id ASC",
            params,
        )
        rows = cur.fetchall()

    # Decimal 转 float
    rows = [
        {
            "id": r["id"],
            "m": float(r["m"]),
            "ang": float(r["ang"]),
            "air": float(r["air"]),
            "state": r["state"],
            "alarm": int(r["alarm"]),
            "ts": int(r["ts"]),
        }
        for r in rows
    ]

    step = max(1, int(total / max_points))
    picked = rows[::step] if step > 1 else rows
    if rows and (not picked or picked[-1]["id"] != rows[-1]["id"]):
        picked = picked + [rows[-1]]
    return {"total": total, "max_points": max_points, "items": picked}


# locations（L80-R GPS 定位）

# GPS 时间：北京时间，无时区

GPS_TRACK_THRESHOLD_M = 50.0     # 轨迹抽稀距离（m）
GPS_NEAREST_MINUTES   = 5        # 最近点时间窗（分钟）


def _loc_row(r):
    """MySQL 行转 JSON 字典"""
    return {
        "id": r["id"],
        "device_id": r["device_id"],
        "longitude": float(r["longitude"]),
        "latitude": float(r["latitude"]),
        "gps_time": _fmt_dt(r["gps_time"]),
        "received_at": _fmt_dt(r["received_at"]),
        "sats": r["sats"],
        "hdop": float(r["hdop"]) if r["hdop"] is not None else None,
    }


def insert_location(conn, device_id, boot_id, seq, up_ms, lat, lng,
                    gps_time, sats=None, hdop=None):
    """写入定位；gps_time 为北京时间，无时区"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO locations "
            "(device_id, boot_id, seq, up_ms, longitude, latitude, gps_time, sats, hdop) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (device_id, boot_id, seq, up_ms, lng, lat, gps_time,
             sats, hdop),
        )


def latest_location(conn, device_id):
    """最新定位；无结果为 None"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, device_id, longitude, latitude, gps_time, received_at, sats, hdop "
            "FROM locations WHERE device_id=%s "
            "ORDER BY gps_time DESC, id DESC LIMIT 1",
            (device_id,),
        )
        row = cur.fetchone()
    return _loc_row(row) if row else None


def locations_range(conn, device_id, start_dt, end_dt):
    """查询 [start,end] 定位，按北京时间升序"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, device_id, longitude, latitude, gps_time, received_at, sats, hdop "
            "FROM locations WHERE device_id=%s AND gps_time>=%s AND gps_time<=%s "
            "ORDER BY gps_time ASC, id ASC",
            (device_id, start_dt, end_dt),
        )
        rows = cur.fetchall()
    return [_loc_row(r) for r in rows]


def nearest_location(conn, device_id, target_dt, window_minutes=GPS_NEAREST_MINUTES):
    """target_dt ±window 分钟内最近定位"""
    max_diff = timedelta(minutes=window_minutes)
    start_dt = target_dt - max_diff
    end_dt = target_dt + max_diff
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, device_id, longitude, latitude, gps_time, received_at, sats, hdop "
            "FROM locations WHERE device_id=%s AND gps_time>=%s AND gps_time<=%s "
            "ORDER BY ABS(TIMESTAMPDIFF(SECOND, gps_time, %s)) ASC, id DESC "
            "LIMIT 1",
            (device_id, start_dt, end_dt, target_dt),
        )
        row = cur.fetchone()
    if row is None:
        return None
    d = _loc_row(row)
    d["difference_seconds"] = abs(int((row["gps_time"] - target_dt).total_seconds()))
    return d
