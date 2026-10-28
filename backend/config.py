# 优先级：环境变量 > .env > 默认值
import os


def _load_dotenv():
    """加载 .env，显式环境变量优先"""
    path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# MySQL
DB_HOST     = os.environ.get("FALL_DETECTOR_DB_HOST", "127.0.0.1")
DB_PORT     = int(os.environ.get("FALL_DETECTOR_DB_PORT", "3306"))
DB_USER     = os.environ.get("FALL_DETECTOR_DB_USER", "root")
DB_PASSWORD = os.environ.get("FALL_DETECTOR_DB_PASSWORD", "")
DB_NAME     = os.environ.get("FALL_DETECTOR_DB_NAME", "fall_detector")

# Flask 服务
SERVER_HOST = os.environ.get("FALL_DETECTOR_HOST", "0.0.0.0")   # 0.0.0.0 局域网可访问
SERVER_PORT = int(os.environ.get("FALL_DETECTOR_PORT", "5000"))

# 上报限流 / 采样
SAMPLE_RATE_HZ      = 1.0    # 采样入库限流：最多 1 条/秒
# 关键状态或 alarm 变化时强制入库
SAMPLE_FORCE_STATES = {"TILT_ALARM", "FALL_STILL"}

# 照片库：SQLite 保存原始 JPEG
PHOTO_DB_PATH = os.environ.get(
    "FALL_DETECTOR_PHOTO_DB", os.path.join(os.path.dirname(__file__), "data", "photos.sqlite3")
)
PHOTO_MAX_BYTES = 512 * 1024

# 影子模型，不控制设备
FALL_MODEL_PATH = os.environ.get(
    "FALL_DETECTOR_MODEL", os.path.join(os.path.dirname(__file__), "models", "sisfall_rf_v1.json")
)
IMU_DB_PATH = os.environ.get(
    "FALL_DETECTOR_IMU_DB", os.path.join(os.path.dirname(__file__), "data", "imu.sqlite3")
)
# 原始六轴记录，默认关闭
IMU_RECORD_RAW = os.environ.get("FALL_DETECTOR_IMU_RECORD_RAW", "0") == "1"
