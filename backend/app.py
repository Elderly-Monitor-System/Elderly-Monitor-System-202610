# Flask 服务入口
import math
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

from flask import Flask, g, jsonify, render_template, request

import config
import db as db_mod
import ingest
import queries
import photos
import imu

app = Flask(__name__)
app.config.from_object(config)
app.register_blueprint(photos.bp)
app.register_blueprint(imu.bp)

# 当前设备状态缓存
latest = {
    "online": False, "device_id": None, "boot_id": None,
    "up": 0, "m": None, "ang": None,
    "air": None, "abad": 0,                    # 空气指数 / 超标标志
    "state": "OFFLINE", "alarm": 0, "seq": 0,
    "last_seen": None,
}
latest_lock = threading.Lock()


# 请求结束时归还连接
@app.before_request
def _open_db():
    if request.blueprint in ("photos", "imu") or request.endpoint == "static":
        return  # 照片使用独立 SQLite
    g.db = db_mod.get_pool().connection()


@app.teardown_request
def _close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="bad json"), 400
    ok = ingest.handle(g.db, payload, latest, latest_lock)
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, error="invalid"), 400)


@app.route("/api/state")
def api_state():
    with latest_lock:
        snap = dict(latest)
    snap["events"] = queries.recent_events(g.db, limit=20)
    snap["ec"] = queries.event_count(g.db)          # 总事件数（前端增量检测）
    snap["server_time"] = int(time.time())
    return jsonify(snap)


@app.route("/api/events")
def api_events():
    page = max(1, request.args.get("page", 1, type=int))
    per = min(100, max(1, request.args.get("per_page", 50, type=int)))
    etype = request.args.get("etype")
    if etype not in ("TILT", "FALL", "AIR"):
        etype = None
    return jsonify(queries.page_events(g.db, page, per, etype=etype))


@app.route("/api/stats")
def api_stats():
    return jsonify(queries.stats_summary(g.db))


@app.route("/api/samples")
def api_samples():
    now = int(time.time())
    since = request.args.get("since", type=int) or (now - 3600)
    until = request.args.get("until", type=int) or now
    max_points = min(1000, max(10, request.args.get("max_points", 300, type=int)))
    # 曲线按 device_id 筛选
    device_id = _normalize_dev_arg(request.args.get("device_id"))   # None：不过滤设备
    return jsonify(queries.samples_range(g.db, device_id=device_id,
                                         since=since, until=until, max_points=max_points))


@app.route("/")
def index():
    return render_template("index.html")


# 定位接口与地图

# GPS：北京时间；轨迹按 50m 抽稀

_BJ_TZ = timezone(timedelta(hours=8))
_DEV_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _bj_now_naive():
    """北京时间，无时区"""
    return datetime.now(_BJ_TZ).replace(tzinfo=None)


def _normalize_dev_arg(v):
    if not isinstance(v, str):
        return None
    s = v.strip()[:32]
    return s if _DEV_RE.match(s) else None


def _resolve_device(device_id_arg):
    """设备参数缺省时选最近设备"""
    s = _normalize_dev_arg(device_id_arg)
    if s:
        return s
    with latest_lock:
        s = latest.get("device_id")
    if s:
        return s
    with g.db.cursor() as cur:
        cur.execute("SELECT device_id FROM locations ORDER BY received_at DESC LIMIT 1")
        row = cur.fetchone()
    return row["device_id"] if row else None


def _haversine_m(lon1, lat1, lon2, lat2):
    """WGS84 距离（m）"""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _simplify_track(locs, threshold_m=queries.GPS_TRACK_THRESHOLD_M):
    """按 50m 抽稀轨迹，保留首尾点"""
    if len(locs) <= 2:
        return locs
    out = [locs[0]]
    last = locs[0]
    for p in locs[1:]:
        if _haversine_m(last["longitude"], last["latitude"],
                        p["longitude"], p["latitude"]) >= threshold_m:
            out.append(p)
            last = p
    if out[-1]["id"] != locs[-1]["id"]:
        out.append(locs[-1])
    return out


@app.route("/map")
def map_page():
    return render_template("map.html")


@app.route("/api/location")
def api_latest_location():
    """设备最新定位"""
    device_id = _resolve_device(request.args.get("device_id"))
    if not device_id:
        return jsonify(found=False, message="尚无 GPS 数据")
    row = queries.latest_location(g.db, device_id)
    if row is None:
        return jsonify(found=False, device_id=device_id, message="该设备暂无GPS数据")
    row["found"] = True
    return jsonify(row)


def _api_locations(rows, device_id, **extra):
    rows = _simplify_track(rows)
    return jsonify({"device_id": device_id, "count": len(rows),
                    "locations": rows, **extra})


@app.route("/api/locations/today")
def api_locations_today():
    """今日轨迹（北京时间）"""
    device_id = _resolve_device(request.args.get("device_id"))
    if not device_id:
        return jsonify(found=False, message="尚无 GPS 数据")
    now = _bj_now_naive()
    start = datetime.combine(now.date(), datetime.min.time())
    rows = queries.locations_range(g.db, device_id, start, now)
    return _api_locations(rows, device_id, date=str(now.date()))


@app.route("/api/locations/date")
def api_locations_by_date():
    """全天轨迹；日期 YYYY-MM-DD（北京时间）"""
    device_id = _resolve_device(request.args.get("device_id"))
    date_str = request.args.get("date_str", "")
    try:
        query_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify(found=False, error="日期格式错误，应为 YYYY-MM-DD"), 400
    if not device_id:
        return jsonify(found=False, message="尚无 GPS 数据")
    start = datetime.combine(query_date, datetime.min.time())
    end = datetime.combine(query_date, datetime.max.time())
    rows = queries.locations_range(g.db, device_id, start, end)
    return _api_locations(rows, device_id, date=str(query_date))


@app.route("/api/locations/range")
def api_locations_range():
    """时间段轨迹；since/until 为北京时间"""
    device_id = _resolve_device(request.args.get("device_id"))
    def _parse_dt(v, key):
        if not v:
            return None
        try:
            return datetime.strptime(v.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                return datetime.strptime(v.strip(), "%Y-%m-%d")
            except ValueError:
                return None
    start = _parse_dt(request.args.get("since"), "since")
    end = _parse_dt(request.args.get("until"), "until")
    if start is None or end is None:
        return jsonify(found=False, error="时间格式错误，应为 YYYY-MM-DD[ HH:MM:SS]"), 400
    if not device_id:
        return jsonify(found=False, message="尚无 GPS 数据")
    if start >= end:
        return jsonify(found=False, error="开始时间必须小于结束时间"), 400
    rows = queries.locations_range(g.db, device_id, start, end)
    return _api_locations(rows, device_id, start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
                          end_time=end.strftime("%Y-%m-%d %H:%M:%S"))


@app.route("/api/locations/nearest")
def api_locations_nearest():
    """查询窗口内的最近定位"""
    device_id = _resolve_device(request.args.get("device_id"))
    date_str = request.args.get("date_str", "")
    time_str = request.args.get("time_str", "")
    try:
        target = datetime.strptime(f"{date_str} {time_str}".strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return jsonify(found=False, error="时间格式错误，应为 date_str=YYYY-MM-DD&time_str=HH:MM:SS"), 400
    if not device_id:
        return jsonify(found=False, message="尚无 GPS 数据")
    row = queries.nearest_location(g.db, device_id, target)
    if row is None:
        return jsonify(found=False, device_id=device_id,
                       query_time=target.strftime("%Y-%m-%d %H:%M:%S"),
                       message=f"该时间前后{queries.GPS_NEAREST_MINUTES}分钟暂无GPS数据")
    row["found"] = True
    row["query_time"] = target.strftime("%Y-%m-%d %H:%M:%S")
    return jsonify(row)


# 地图接口别名

@app.route("/location")
def alias_location():
    return api_latest_location()


@app.route("/locations/today")
def alias_locations_today():
    return api_locations_today()


@app.route("/locations/history/date")
def alias_locations_history_date():
    return api_locations_by_date()


@app.route("/locations/history/range")
def alias_locations_history_range():
    return api_locations_range()


@app.route("/locations/nearest")
def alias_locations_nearest():
    return api_locations_nearest()


if __name__ == "__main__":
    db_mod.init()          # 幂等建库建表
    print(f"已连接 MySQL {config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")
    print(f"看板: http://127.0.0.1:{config.SERVER_PORT}/")
    print(f"地图: http://127.0.0.1:{config.SERVER_PORT}/map")
    app.run(host=config.SERVER_HOST, port=config.SERVER_PORT, threaded=True)
