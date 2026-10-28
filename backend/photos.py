"""照片接收、SQLite 保存与查询"""
import hashlib
import io
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, send_file

bp = Blueprint("photos", __name__)
_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_REASONS = {"TIMER", "FALL", "AIR", "MANUAL"}
_BJ = timezone(timedelta(hours=8))
_COLUMNS = "id,device_id,boot_id,photo_seq,reason,event_seq,trigger_up_ms,capture_up_ms,width,height,received_at,sha256,length(jpeg) AS bytes"


@contextmanager
def _db():
    path = Path(current_app.config["PHOTO_DB_PATH"])
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY,
            device_id TEXT NOT NULL, boot_id TEXT NOT NULL,
            photo_seq INTEGER NOT NULL, reason TEXT NOT NULL,
            event_seq INTEGER, trigger_up_ms INTEGER NOT NULL,
            capture_up_ms INTEGER NOT NULL, width INTEGER NOT NULL,
            height INTEGER NOT NULL, received_at TEXT NOT NULL,
            sha256 TEXT NOT NULL, jpeg BLOB NOT NULL,
            UNIQUE(device_id, boot_id, photo_seq))""")
        conn.execute("CREATE INDEX IF NOT EXISTS photos_device_id ON photos(device_id,id)")
        conn.execute("CREATE INDEX IF NOT EXISTS photos_reason_id ON photos(reason,id)")
        conn.execute("CREATE INDEX IF NOT EXISTS photos_event ON photos(device_id,boot_id,event_seq)")
        with conn:
            yield conn
    finally:
        conn.close()


def _identifier(value, max_len):
    if not value or len(value) > max_len or not _ID.fullmatch(value):
        raise ValueError("设备或启动标识无效")
    return value


def _number(name, minimum=0, maximum=0xFFFFFFFF):
    value = request.headers.get(name, "")
    if not value.isascii() or not value.isdecimal():
        raise ValueError(f"{name} 必须为非负整数")
    value = int(value)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 超出范围")
    return value


def _jpeg_size(data):
    """校验 JPEG 段边界并读取 SOF 尺寸"""
    if len(data) < 20 or data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        raise ValueError("需要完整 JPEG 图片")
    pos, size = 2, None
    sof = {0xC0, 0xC1, 0xC2}
    while pos < len(data) - 2:
        if data[pos] != 0xFF:
            raise ValueError("JPEG 段格式无效")
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker in {0xD8, 0xD9, 0x00} or 0xD0 <= marker <= 0xD7:
            raise ValueError("JPEG 标记无效")
        if pos + 2 > len(data):
            break
        length = int.from_bytes(data[pos:pos + 2], "big")
        if length < 2 or pos + length > len(data) - 2:
            raise ValueError("JPEG 数据不完整")
        if marker in sof:
            if length < 8:
                raise ValueError("JPEG 尺寸无效")
            height = int.from_bytes(data[pos + 3:pos + 5], "big")
            width = int.from_bytes(data[pos + 5:pos + 7], "big")
            if not (1 <= width <= 1600 and 1 <= height <= 1200):
                raise ValueError("照片尺寸须在 1600×1200 以内")
            size = (width, height)
        if marker == 0xDA:
            if not size or pos + length >= len(data) - 2:
                break
            return size
        pos += length
    raise ValueError("JPEG 缺少有效图像段")


def _record(row):
    result = dict(row)
    result["url"] = f"/api/photos/{row['id']}/image"
    return result


@bp.route("/photos")
def page():
    return render_template("photos.html")


@bp.route("/api/photos", methods=["POST"])
def upload():
    if request.mimetype != "image/jpeg":
        return jsonify(ok=False, error="Content-Type 必须为 image/jpeg"), 415
    limit = current_app.config["PHOTO_MAX_BYTES"]
    if request.content_length is not None and request.content_length > limit:
        return jsonify(ok=False, error="照片过大"), 413
    try:
        device_id = _identifier(request.headers.get("X-Device-Id"), 32)
        boot_id = _identifier(request.headers.get("X-Boot-Id"), 16)
        reason = request.headers.get("X-Photo-Reason")
        if reason not in _REASONS:
            raise ValueError("拍照类型仅允许 TIMER/FALL/AIR/MANUAL")
        seq = _number("X-Photo-Seq", 1)
        event_seq = _number("X-Event-Seq")
        trigger = _number("X-Trigger-Up-Ms")
        captured = _number("X-Capture-Up-Ms")
        if (reason in {"FALL", "AIR"}) != (event_seq > 0):
            raise ValueError("报警照片必须提供事件序号，其他照片事件序号须为 0")
        data = request.stream.read(limit + 1)
        if len(data) > limit:
            return jsonify(ok=False, error="照片过大"), 413
        width, height = _jpeg_size(data)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    digest = hashlib.sha256(data).hexdigest()
    received = datetime.now(_BJ).isoformat(timespec="milliseconds")
    with _db() as conn:
        inserted = conn.execute("""INSERT OR IGNORE INTO photos
            (device_id,boot_id,photo_seq,reason,event_seq,trigger_up_ms,capture_up_ms,
             width,height,received_at,sha256,jpeg) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (device_id, boot_id, seq, reason, event_seq or None, trigger, captured,
             width, height, received, digest, data)).rowcount == 1
        row = conn.execute(f"SELECT {_COLUMNS} FROM photos WHERE device_id=? AND boot_id=? AND photo_seq=?",
                           (device_id, boot_id, seq)).fetchone()
        if (row["sha256"], row["reason"], row["event_seq"], row["trigger_up_ms"], row["capture_up_ms"]) != (
                digest, reason, event_seq or None, trigger, captured):
            return jsonify(ok=False, error="同一照片序号的内容或元数据不一致"), 409
        return jsonify(ok=True, duplicate=not inserted, photo=_record(row)), 201 if inserted else 200


@bp.route("/api/photos", methods=["GET"])
def listing():
    clauses, args = [], []
    try:
        for name, max_len in (("device_id", 32), ("boot_id", 16)):
            value = request.args.get(name)
            if value:
                clauses.append(f"{name}=?")
                args.append(_identifier(value, max_len))
        reason = request.args.get("reason")
        if reason:
            if reason not in _REASONS:
                raise ValueError("拍照类型无效")
            clauses.append("reason=?")
            args.append(reason)
        event_seq = request.args.get("event_seq")
        if event_seq is not None:
            if not event_seq.isascii() or not event_seq.isdecimal() or not 1 <= int(event_seq) <= 0xFFFFFFFF:
                raise ValueError("事件序号无效")
            clauses.append("event_seq=?")
            args.append(int(event_seq))
        before = request.args.get("before", type=int)
        if before is not None:
            if before < 1:
                raise ValueError("翻页游标无效")
            clauses.append("id<?")
            args.append(before)
        limit = min(48, max(1, request.args.get("limit", 12, type=int)))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _db() as conn:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM photos{where} ORDER BY id DESC LIMIT ?", args + [limit + 1]).fetchall()
    more = len(rows) > limit
    photos = [_record(row) for row in rows[:limit]]
    return jsonify(photos=photos, has_more=more, next_before=photos[-1]["id"] if more else None)


@bp.route("/api/photos/<int:photo_id>/image")
def image(photo_id):
    with _db() as conn:
        row = conn.execute("SELECT jpeg,sha256,received_at FROM photos WHERE id=?", (photo_id,)).fetchone()
    if row is None:
        return jsonify(error="照片不存在"), 404
    response = send_file(io.BytesIO(row["jpeg"]), mimetype="image/jpeg", download_name=f"photo-{photo_id}.jpg",
                         etag=row["sha256"], conditional=True, max_age=86400)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


@bp.errorhandler(sqlite3.Error)
@bp.errorhandler(OSError)
def storage_error(exc):
    current_app.logger.exception("Photo storage unavailable")
    return jsonify(ok=False, error="照片存储暂不可用，请检查保存目录权限及磁盘空间"), 503
