// 设备监测与上传；接线见 ../接线说明.txt

#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <TinyGPSPlus.h>
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <esp_random.h>
#include "board_pins.h"
#include "camera_service.h"
#include "wearable_fall_detector.h"
#include "imu_stream.h"
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/queue.h>

TwoWire sensorWire(MPU_I2C_PORT);  // MPU 独立 I2C 控制器

Adafruit_MPU6050 mpu;
TinyGPSPlus gps;

// 检测参数
const float   IMPACT_G        = 2.5f;   // 冲击阈值（g）
const float   STILL_G         = 0.20f;  // 静止模值容差（g）
const float   TILT_DEG        = 45.0f;  // 倾倒阈值（度）
const uint32_t STILL_MS       = 1200;   // 静止确认时间（ms）
const uint32_t HOLD_MS        = 1500;   // 倾倒确认时间（ms）
const uint32_t ALARM_MS       = 5000;   // 报警时长（ms）
const uint32_t ARM_DELAY_MS   = 3000;   // 开机校准延时（ms）

// MQ135 空气判定参数
const uint32_t MQ_WARM_MS     = 90000;  // 基线预热（ms），期间 air=0
const uint32_t AIR_TICK_MS    = 200;    // 空气采样周期（ms）
const float    MQ_RATIO_MAX   = 1.8f;   // 指数满量程比值：AO/基线
const float    AIR_BAD_IDX    = 60.0f;  // 空气超标阈值
const uint32_t AIR_HOLD_MS    = 6000;   // 超标确认时间（ms）
const float    AIR_GOOD_IDX   = 40.0f;  // 空气恢复阈值
const uint32_t AIR_RELEASE_MS = 4000;   // 恢复确认时间（ms）

// GPS 参数
const uint32_t GPS_UPLOAD_MS     = 5000;  // 定位上报周期（ms）
const uint32_t GPS_FIX_MAX_AGE_MS = 10000; // 定位有效期（ms）
const uint32_t GPS_UART_TIMEOUT_MS = 3000; // 串口断流阈值（ms）

// 联网配置（烧录前填写）
#define STA_SSID        "YOUR_WIFI_SSID"      // 2.4GHz Wi-Fi 名称
#define STA_PASS        "YOUR_WIFI_PASSWORD"
#define BACKEND_URL     "http://192.168.1.100:5000/api/ingest"  // 电脑后端 /api/ingest 地址
#define AP_SSID_PREFIX  "FallDetector"           // AP 前缀，附加 MAC
#define AP_PASS         "CHANGE_ME_123"               // AP 密码，至少 8 位
#define SEND_MS         500                      // 状态上报周期（ms）
#define HTTP_TIMEOUT_MS 2000                     // POST 超时（ms）
#define PEND_BUF_SIZE   20                       // 离线事件缓冲容量
#define JSON_BUF_SIZE   640                      // JSON 缓冲字节数

// 加速度换算：m/s² → g
const float G = SENSORS_GRAVITY_STANDARD;

// 引脚集中在 board_pins.h
HardwareSerial GPSSerial(2);

// MPU 独占采样，主循环读取快照
sensors_event_t accel, gyro, temp;

float refX = 0, refY = 0, refZ = 1;   // 校准竖直单位向量

enum State { ARMED, TILT_WAIT, TILT_ALARM, FALL_IMPACT, FALL_STILL };
State state = ARMED;
bool   alarmOn  = false;
unsigned long alarmStart = 0;
unsigned long lastDbg   = 0;
unsigned long lastSend  = 0;

// 上报指标
float gM = 0, gAngle = 0;

// MQ135 空气质量状态
float airIdx = 0;          // 污染指数 0~100，越高越差
bool  airBad = false;      // 持续超标标志（abad）
long  airMv = -1;          // AO（mV）；-1 未采样
float airBaseMv = -1.0f;   // 基线（mV）；负数表示预热
bool  airCalDone = false;  // 基线就绪标志
long  airMinMv = 0;        // 预热最小值
unsigned long lastAirTick = 0;
unsigned long airCondStart = 0;   // 超标/恢复计时起点
bool  airWarned = false;   // 预热异常只警告一次

// 事件幂等键：设备 + 会话 + 序号
char deviceId[24];      // MAC 派生设备标识
char bootIdStr[16];     // 本次启动标识
uint32_t eventSeq = 0;  // 事件序号/幂等键

// 离线事件缓冲（环形）
struct PendingEvent {
  char     etype[8];    // "TILT" / "FALL" / "AIR"
  uint32_t up_ms;       // 事件时运行毫秒
  uint32_t seq;
};
PendingEvent pendBuf[PEND_BUF_SIZE];
uint8_t  pendHead  = 0;
uint8_t  pendCount = 0;

// POST 诊断状态
bool   lastPostOk = false;
int    lastPostCode = -1;
bool   posting = false;      // HTTP 防重入标志

char jsonBuf[JSON_BUF_SIZE];

// Web 服务：仅 AP 调试页
WebServer server(80);

// 跌倒检测与事件队列
const uint32_t IMU_SAMPLE_MS = 20;  // 采样周期：20ms（50Hz）
const bool ENABLE_IMU_STREAM = true;  // 六轴上传开关；后端影子判定
struct MotionSnapshot {
  float magnitude = 1, angle = 0, gyroDps = 0, poseChange = 0, peak = 0, rotation = 0;
  uint32_t sampleMs = 0, gaps = 0, maxGapMs = 0;
  uint8_t state = ARMED;
  bool valid = false, lowEvidence = false;
};
MotionSnapshot motionSnapshot;
portMUX_TYPE motionMux = portMUX_INITIALIZER_UNLOCKED;
portMUX_TYPE eventMux = portMUX_INITIALIZER_UNLOCKED;
QueueHandle_t detectionEvents = nullptr;
bool imuTaskRunning = false;
uint32_t detectionQueueDrops = 0;

WearableFallConfig makeFallConfig() {
  WearableFallConfig c;
  c.impactG = IMPACT_G;
  c.stillBandG = STILL_G;
  c.stillMs = STILL_MS;
  c.tiltDeg = TILT_DEG;
  c.tiltHoldMs = HOLD_MS;
  c.tiltAlarmMs = ALARM_MS;
  return c;
}
WearableFallDetector fallDetector(makeFallConfig());

uint32_t currentEventSeq() {
  portENTER_CRITICAL(&eventMux);
  uint32_t seq = eventSeq;
  portEXIT_CRITICAL(&eventMux);
  return seq;
}
bool alarmActive() {
  portENTER_CRITICAL(&eventMux);
  bool active = alarmOn;
  portEXIT_CRITICAL(&eventMux);
  return active;
}
MotionSnapshot readMotionSnapshot() {
  portENTER_CRITICAL(&motionMux);
  MotionSnapshot snapshot = motionSnapshot;
  portEXIT_CRITICAL(&motionMux);
  return snapshot;
}

// GPS 状态
struct GpsFix {
  bool valid = false;          // 定位有效标志
  double lat = 0.0;            // WGS84 纬度
  double lng = 0.0;            // WGS84 经度
  String utc = "";             // "YYYY-MM-DDTHH:MM:SSZ"
  uint64_t timeKey = 0;        // 定位时间去重键
  unsigned long at = 0;        // 定位接收时间（ms）
  int sats = -1;               // 卫星数（未知=-1）
  float hdop = -1.0f;          // HDOP（未知=-1）
};
GpsFix gpsFix;
uint64_t gpsUploadedKey = 0;   // 最近上传定位键
unsigned long lastGpsByteTime = 0;  // 最近串口接收时间（ms）
unsigned long lastGpsCheck = 0;     // 上次定位检查时间（ms）
String lastGpsStatus = "尚未定位";


float mag(float x, float y, float z)        { return sqrt(x*x + y*y + z*z); }
float clampF(float v, float lo, float hi)   { return v < lo ? lo : (v > hi ? hi : v); }

// 竖直夹角（°）；无效返回 -1
float tiltAngleDeg(float x, float y, float z) {
  float m = mag(x, y, z);
  if (m < 0.5f) return -1.0f;
  float cosA = (x*refX + y*refY + z*refZ) / m;
  return acos(clampF(cosA, -1.0f, 1.0f)) * RAD_TO_DEG;
}

const char* stateName() {
  switch (state) {
    case ARMED:       return "ARMED";
    case TILT_WAIT:   return "TILT_WAIT";
    case TILT_ALARM:  return "TILT_ALARM";
    case FALL_IMPACT: return "FALL_IMPACT";
    case FALL_STILL:  return "FALL_STILL";
  }
  return "?";
}

// 事件类型 ASCII 码 → 中文标签（串口用）
const char* alarmLabel(const char* code) {
  if (strcmp(code, "TILT") == 0) return "倾倒 TILT";
  if (strcmp(code, "FALL") == 0) return "跌落 FALL";
  return "空气污染 AIR";
}

const char* staStatusText() {
  switch (WiFi.status()) {
    case WL_CONNECTED:       return "已连接";
    case WL_NO_SSID_AVAIL:   return "找不到WiFi(检查2.4GHz/SSID)";
    case WL_CONNECT_FAILED:  return "连接失败(密码?)";
    case WL_DISCONNECTED:    return "已断开";
    default:                 return "连接中...";
  }
}


bool httpPostJson();
void sendEvent(const char* etype, uint32_t up_ms, uint32_t seq);

// 采样任务入队，主循环发送
void reportDetectionEvent(const char* type, uint32_t up_ms) {
  portENTER_CRITICAL(&eventMux);
  uint32_t seq = ++eventSeq;
  portEXIT_CRITICAL(&eventMux);
  cameraOnAlarm(type, seq, up_ms);  // 仅 FALL/AIR 请求拍照
  PendingEvent e = {};
  strlcpy(e.etype, type, sizeof(e.etype));
  e.seq = seq;
  e.up_ms = up_ms;
  if (!detectionEvents || xQueueSend(detectionEvents, &e, 0) != pdTRUE) {
    portENTER_CRITICAL(&eventMux);
    ++detectionQueueDrops;
    portEXIT_CRITICAL(&eventMux);
    Serial.printf("[事件] 待发送队列满，丢弃 %s seq=%u；本地报警不受影响\n", type, seq);
  }
}

void startAlarm(const char* type, uint32_t now) {
  portENTER_CRITICAL(&eventMux);
  alarmOn = true;
  alarmStart = now;
  portEXIT_CRITICAL(&eventMux);
  reportDetectionEvent(type, now);
  Serial.printf("[报警] %s 发生于 up=%lu ms\n", alarmLabel(type), (unsigned long)now);
}

void updateAlarmOutputs(uint32_t now) {
  portENTER_CRITICAL(&eventMux);
  if (alarmOn && now - alarmStart >= ALARM_MS) alarmOn = false;
  bool active = alarmOn;
  portEXIT_CRITICAL(&eventMux);
  if (LED_PIN >= 0) digitalWrite(LED_PIN, active && (now / 200) % 2 ? HIGH : LOW);
  if (BUZZER_PIN >= 0) digitalWrite(BUZZER_PIN, active ? HIGH : LOW);
}

void sampleMotion() {
  bool ok = mpu.getEvent(&accel, &gyro, &temp);
  uint32_t now = millis();
  if (ok && ENABLE_IMU_STREAM) {
    imuStreamSample(accel.acceleration.x / G, accel.acceleration.y / G, accel.acceleration.z / G,
                    gyro.gyro.x * RAD_TO_DEG, gyro.gyro.y * RAD_TO_DEG, gyro.gyro.z * RAD_TO_DEG);
  }
  WearableFallDetector::Sample sample(now,
      ok ? WearableFallDetector::Vec(accel.acceleration.x / G, accel.acceleration.y / G, accel.acceleration.z / G)
         : WearableFallDetector::Vec(NAN, NAN, NAN),
      WearableFallDetector::Vec(gyro.gyro.x * RAD_TO_DEG, gyro.gyro.y * RAD_TO_DEG, gyro.gyro.z * RAD_TO_DEG));
  WearableFallDetector::Event event = fallDetector.update(sample);
  MotionSnapshot next = readMotionSnapshot();
  next.valid = ok && isfinite(fallDetector.magnitude()) && isfinite(fallDetector.gyroSpeed());
  if (next.valid) {
    next.magnitude = fallDetector.magnitude();
    next.angle = fallDetector.uprightAngle();
    next.gyroDps = fallDetector.gyroSpeed();
    next.sampleMs = now;
  }
  next.state = static_cast<uint8_t>(fallDetector.state());
  next.poseChange = fallDetector.poseChange();
  next.peak = fallDetector.impactPeak();
  next.rotation = fallDetector.rotation();
  next.lowEvidence = fallDetector.lowEvidence();
  next.gaps = fallDetector.gaps();
  next.maxGapMs = fallDetector.maxGapMs();
  portENTER_CRITICAL(&motionMux);
  motionSnapshot = next;
  portEXIT_CRITICAL(&motionMux);
  if (event == WearableFallDetector::FALL) startAlarm("FALL", now);
  else if (event == WearableFallDetector::TILT) startAlarm("TILT", now);
  updateAlarmOutputs(now);
}

void motionTask(void*) {
  TickType_t wake = xTaskGetTickCount();
  const TickType_t period = pdMS_TO_TICKS(IMU_SAMPLE_MS);
  for (;;) {
    sampleMotion();
    // 延迟后跳过补采样
    if (xTaskGetTickCount() - wake >= period) wake = xTaskGetTickCount();
    vTaskDelayUntil(&wake, period);
  }
}

// 空气采样与判定
// 比值指数，持续触发、回差解除
void airSample(unsigned long now) {
  if (now - lastAirTick < AIR_TICK_MS) return;
  lastAirTick = now;

  // 均值采样与 EWMA 平滑
  long sum = 0;
  const int N = 4;
  for (int i = 0; i < N; i++) sum += analogReadMilliVolts(MQ_PIN);
  long raw = sum / N;
  airMv = (airMv < 0) ? raw : (airMv * 9 / 10 + raw * 1 / 10);

  // 预热期忽略前 8s，取最小值为基线
  const uint32_t IGNORE_MS = 8000;
  if (!airCalDone) {
    if (now >= IGNORE_MS) {
      if (airMinMv == 0 || airMv < airMinMv) airMinMv = airMv;
      if (now >= MQ_WARM_MS && airMinMv > 50) {        // 基线最小值：50mV
        airBaseMv = (float)airMinMv;
        airCalDone = true;
        Serial.printf("[空气] 基线校准完成 baseline=%ldmV（请在干净环境开机预热 %lu 秒）\n",
                      (long)airBaseMv, (unsigned long)(MQ_WARM_MS / 1000));
      }
      if (now >= MQ_WARM_MS && !airCalDone && !airWarned) {
        airWarned = true;
        Serial.println("[空气] 预热结束但读数过低：确认 MQ135 已接、AO 电压 ≤3.3V，继续采集基线");
      }
    }
    airIdx = 0; airBad = false; airCondStart = 0;
    return;
  }

  // 基线仅向低污染方向微调
  float ratio = (float)airMv / airBaseMv;
  airIdx = (ratio <= 1.0f) ? 0.0f
           : clampF((ratio - 1.0f) / (MQ_RATIO_MAX - 1.0f), 0.0f, 1.0f) * 100.0f;
  if (airMv < airBaseMv)
    airBaseMv += ((float)airMv - airBaseMv) * 0.01f;   // 长期漂移/中途通风补偿

  // 空气超标与回差恢复状态机
  if (airBad) {
    if (airIdx <= AIR_GOOD_IDX) {
      if (airCondStart == 0) airCondStart = now;
      else if (now - airCondStart >= AIR_RELEASE_MS) { airBad = false; airCondStart = 0; }
    } else airCondStart = 0;
  } else {
    if (airIdx >= AIR_BAD_IDX) {
      if (airCondStart == 0) airCondStart = now;
      else if (now - airCondStart >= AIR_HOLD_MS) {
        airBad = true; airCondStart = 0;
        reportDetectionEvent("AIR", now);  // 共用事件序号与发送队列
        Serial.println("[空气] 污染超标，已排队 AIR 事件及拍照");
      }
    } else airCondStart = 0;
  }
}

// GPS 解析与上传
// NMEA 非阻塞读取
uint64_t gpsTimeKey() {
  return
    (uint64_t)gps.date.year()   * 10000000000ULL +
    (uint64_t)gps.date.month()  *   100000000ULL +
    (uint64_t)gps.date.day()    *     1000000ULL +
    (uint64_t)gps.time.hour()   *       10000ULL +
    (uint64_t)gps.time.minute() *         100ULL +
    (uint64_t)gps.time.second();
}

String gpsUtcIso() {
  char b[25];
  snprintf(b, sizeof(b), "%04d-%02d-%02dT%02d:%02d:%02dZ",
           (int)gps.date.year(), (int)gps.date.month(), (int)gps.date.day(),
           (int)gps.time.hour(), (int)gps.time.minute(), (int)gps.time.second());
  return String(b);
}

void readGPS() {
  while (GPSSerial.available()) {
    char c = GPSSerial.read();
    lastGpsByteTime = millis();
    if (gps.encode(c)) {
      // 位置、日期、时间均有效才更新
      if (gps.location.isUpdated() && gps.location.isValid() &&
          gps.date.isValid() && gps.time.isValid()) {
        gpsFix.valid = true;
        gpsFix.lat = gps.location.lat();
        gpsFix.lng = gps.location.lng();
        gpsFix.utc = gpsUtcIso();
        gpsFix.timeKey = gpsTimeKey();
        gpsFix.at = millis();
        gpsFix.sats = gps.satellites.isValid() ? gps.satellites.value() : -1;
        gpsFix.hdop = gps.hdop.isValid() ? gps.hdop.hdop() : -1.0f;
        Serial.println();
        Serial.print("== 新 GPS Fix  卫星: ");
        Serial.println(gpsFix.sats);
        Serial.printf("   WGS84: %.6f, %.6f   UTC: %s\n",
                      gpsFix.lat, gpsFix.lng, gpsFix.utc.c_str());
      }
    }
  }
}

bool gpsUartAlive() {
  if (lastGpsByteTime == 0) return false;
  return (millis() - lastGpsByteTime) < GPS_UART_TIMEOUT_MS;
}

// 仅上传 10s 内的新定位
bool canSendGps() {
  if (!gpsFix.valid || gpsFix.utc.length() == 0) return false;
  if (gpsUploadedKey != 0 && gpsFix.timeKey <= gpsUploadedKey) return false;
  if (millis() - gpsFix.at > GPS_FIX_MAX_AGE_MS) return false;
  return true;
}

void buildLocationJson() {
  snprintf(jsonBuf, JSON_BUF_SIZE,
    "{\"t\":\"location\",\"device_id\":\"%s\",\"boot_id\":\"%s\","
    "\"seq\":%u,\"lat\":%.6f,\"lng\":%.6f,\"gps_time\":\"%s\","
    "\"up\":%lu,\"sats\":%d,\"hdop\":%.1f}",
    deviceId, bootIdStr, currentEventSeq(),
    gpsFix.lat, gpsFix.lng, gpsFix.utc.c_str(),
    (unsigned long)millis(), gpsFix.sats, gpsFix.hdop);
}

// 上传成功后更新定位键
void sendGpsLocation() {
  if (WiFi.status() != WL_CONNECTED) { lastGpsStatus = "WiFi未连接"; return; }
  if (!canSendGps()) { lastGpsStatus = "暂无新定位"; return; }
  buildLocationJson();
  if (httpPostJson()) {
    gpsUploadedKey = gpsFix.timeKey;
    lastGpsStatus = "上传成功";
    Serial.println("[GPS] 定位上传成功");
  } else {
    lastGpsStatus = "上传失败";
    Serial.printf("[GPS] 上传失败 code=%d\n", lastPostCode);
  }
}

// HTTP 上报
// 构建 JSON → jsonBuf
void buildStateJson() {
  MotionSnapshot fresh = readMotionSnapshot();
  gM = fresh.magnitude;
  gAngle = fresh.angle;
  state = static_cast<State>(fresh.state);
  snprintf(jsonBuf, JSON_BUF_SIZE,
    "{\"t\":\"state\",\"device_id\":\"%s\",\"boot_id\":\"%s\","
    "\"up\":%lu,\"m\":%.2f,\"ang\":%.1f,"
    "\"state\":\"%s\",\"alarm\":%d,\"seq\":%u,"
    "\"air\":%.0f,\"abad\":%d}",
    deviceId, bootIdStr,
    (unsigned long)millis(), gM, gAngle,
    stateName(), alarmActive() ? 1 : 0, currentEventSeq(),
    airIdx, airBad ? 1 : 0);
}

void buildEventJson(const char* etype, uint32_t up_ms, uint32_t seq) {
  snprintf(jsonBuf, JSON_BUF_SIZE,
    "{\"t\":\"event\",\"device_id\":\"%s\",\"boot_id\":\"%s\","
    "\"seq\":%u,\"etype\":\"%s\",\"up\":%lu}",
    deviceId, bootIdStr, seq, etype, (unsigned long)up_ms);
}

// HTTP 200 为成功


bool httpPostJson() {
  if (posting) return false;
  posting = true;
  bool ok = false;
  HTTPClient http;
  http.begin(BACKEND_URL);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST((uint8_t*)jsonBuf, strlen(jsonBuf));
  lastPostCode = code;
  if (code == 200) { ok = true; lastPostOk = true; }
  else { lastPostOk = false; }
  http.end();
  posting = false;
  return ok;
}

// 事件环形缓存
void bufferEvent(const char* etype, uint32_t up_ms, uint32_t seq) {
  PendingEvent& e = pendBuf[pendHead];
  pendHead = (pendHead + 1) % PEND_BUF_SIZE;
  if (pendCount < PEND_BUF_SIZE) pendCount++;
  strncpy(e.etype, etype, sizeof(e.etype) - 1);
  e.etype[sizeof(e.etype) - 1] = 0;
  e.up_ms = up_ms;
  e.seq = seq;  // 使用事件创建时的序号
}

// 离线事件按序补发，失败暂停
// 出队不移动写指针 pendHead
void flushPendingEvents() {
  while (pendCount > 0) {
    uint8_t oldest = (pendHead + PEND_BUF_SIZE - pendCount) % PEND_BUF_SIZE;
    PendingEvent& e = pendBuf[oldest];
    buildEventJson(e.etype, e.up_ms, e.seq);
    if (!httpPostJson()) break;            // 失败：停止补发，下次再试
    pendCount--;
    Serial.printf("[补发] %s seq=%u\n", e.etype, e.seq);
  }
}

// 周期状态上报；离线不缓存
void sendState() {
  if (WiFi.status() != WL_CONNECTED) return;
  buildStateJson();
  if (httpPostJson()) flushPendingEvents();
}

// 事件发送失败入缓冲，重试不拍照
void sendEvent(const char* etype, uint32_t up_ms, uint32_t seq) {
  if (WiFi.status() != WL_CONNECTED) { bufferEvent(etype, up_ms, seq); return; }
  buildEventJson(etype, up_ms, seq);
  if (!httpPostJson()) bufferEvent(etype, up_ms, seq);
  else flushPendingEvents();
}

// WiFi：AP+STA 双模式
void wifiSetup() {
  WiFi.mode(WIFI_AP_STA);        // 双模式：STA 上报 + AP 调试页
  WiFi.setSleep(false);          // 关闭 Wi-Fi 休眠

  String ap = String(AP_SSID_PREFIX) + "-" +
              String((uint32_t)(ESP.getEfuseMac() & 0xFFFFFF), HEX);
  WiFi.softAP(ap.c_str(), AP_PASS);
  WiFi.setAutoReconnect(true);
  WiFi.begin(STA_SSID, STA_PASS);   // 异步连接
  delay(100);
  Serial.printf("AP  SSID=%s IP=%s\n", ap.c_str(),
                WiFi.softAPIP().toString().c_str());
  Serial.printf("STA SSID=%s 状态=%s\n", STA_SSID, staStatusText());
}

// STA 每 10s 检查并重连
void wifiCheck() {
  static unsigned long lastCheck = 0;
  unsigned long now = millis();
  if (now - lastCheck < 10000) return;
  lastCheck = now;
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] STA 断开，尝试重连...");
    WiFi.disconnect();
    WiFi.begin(STA_SSID, STA_PASS);
  }
}

// AP 调试页（STA 排障 + GPS 诊断）
void handleRoot() {
  char b[4096];
  int off = snprintf(b, sizeof(b),
    "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<style>body{font-family:monospace;background:#0f172a;color:#e2e8f0;padding:16px;max-width:560px;margin:0 auto}"
    "h2{color:#fff}.ok{color:#22c55e}.err{color:#ef4444}.warn{color:#fbbf24}td{padding:3px 0}"
    "table{width:100%;border-collapse:collapse}th{color:#94a3b8;text-align:left;padding-top:10px}</style></head><body>"
    "<h2>ESP32-WROVER 综合监测 · 调试页</h2><table>");
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>AP IP</td><td>%s</td></tr>",
                  WiFi.softAPIP().toString().c_str());
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>STA 状态</td><td>%s</td></tr>",
                  staStatusText());
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>STA IP</td><td>%s</td></tr>",
                  WiFi.localIP().toString().c_str());
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>后端地址</td><td>%s</td></tr>", BACKEND_URL);
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>最近 POST</td><td class='%s'>%s (%d)</td></tr>",
                  lastPostOk ? "ok" : "err", lastPostOk ? "成功" : "失败", lastPostCode);
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>事件计数</td><td>%u</td></tr>", currentEventSeq());
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>离线缓冲</td><td>%u/%u</td></tr>",
                  pendCount, PEND_BUF_SIZE);
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>运行时长</td><td>%lus</td></tr>",
                  (unsigned long)(millis() / 1000));
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>free heap</td><td>%u</td></tr>",
                  ESP.getFreeHeap());


  off += snprintf(b + off, sizeof(b) - off, "<th colspan='2'>GPS (L80-R)</th>");
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>UART 状态</td><td class='%s'>%s</td></tr>",
                  gpsUartAlive() ? "ok" : "err",
                  (lastGpsByteTime == 0) ? "从未收到数据" : (gpsUartAlive() ? "正常" : "断流/无数据"));
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>累计字符</td><td>%lu</td></tr>",
                  (unsigned long)gps.charsProcessed());
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>卫星数</td><td>%s</td></tr>",
                  (gpsFix.sats >= 0) ? String(gpsFix.sats).c_str() : "--");
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>定位</td><td class='%s'>%s</td></tr>",
                  gpsFix.valid ? "ok" : "warn", gpsFix.valid ? "已定位" : "等待定位(楼内正常)");
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>经纬度</td><td>%s</td></tr>",
                  gpsFix.valid ? String(String(gpsFix.lat, 6) + ", " + String(gpsFix.lng, 6)).c_str()
                               : "--");
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>GPS UTC</td><td>%s</td></tr>",
                  gpsFix.utc.length() ? gpsFix.utc.c_str() : "--");
  off += snprintf(b + off, sizeof(b) - off, "<tr><td>最近上报</td><td>%s</td></tr>",
                  lastGpsStatus.c_str());
  snprintf(b + off, sizeof(b) - off, "</table><p style='color:#64748b'>GPS NMEA→GPIO33；MPU 13/14；MQ135 AO→32</p><p><a style='color:#80c7c8' href='/camera'>摄像头：手动拍照与上传诊断</a></p></body></html>");
  server.send(200, "text/html; charset=utf-8", String(b));
}

void handleNotFound() {
  server.send(404, "text/plain", "404 Not Found");
}

// MPU 地址扫描
// 返回 0x68/0x69；未发现为 0
uint8_t scanI2CForMpu() {
  uint8_t found = 0;
  Serial.println("\n-- I2C 总线扫描 --");
  Serial.printf("I2C%d SDA=13 SCL=14（检查杜邦线/供电/共地）\n", MPU_I2C_PORT);
  int n = 0;
  for (uint8_t a = 1; a < 127; a++) {
    sensorWire.beginTransmission(a);
    if (sensorWire.endTransmission() == 0) {
      Serial.printf("  I2C 地址 0x%02X 有设备\n", a);
      if (a == 0x68 || a == 0x69) found = a;   // MPU6050/MPU6500 常见地址
      n++;
    }
  }
  if (n == 0) Serial.println("  总线上没有发现任何 I2C 设备：优先查 VCC/GND/SDA/SCL 接线与模块供电");
  else if (found == 0) Serial.println("  总线有设备但不是 0x68/0x69：确认接的是 MPU6050");
  else Serial.printf("  MPU 地址 = 0x%02X\n", found);
  return found;
}


void setup() {
  Serial.begin(115200);
  if (LED_PIN >= 0) pinMode(LED_PIN, OUTPUT);
  if (BUZZER_PIN >= 0) pinMode(BUZZER_PIN, OUTPUT);
  analogSetPinAttenuation(MQ_PIN, ADC_11db);   // ADC 11dB 衰减；输入须 ≤3.3V

  // MPU 初始化：0x68/0x69
  sensorWire.begin(MPU_SDA_PIN, MPU_SCL_PIN);  // MPU SDA/SCL：13/14
  bool mpuOk = false;
  uint8_t mpuAddr = scanI2CForMpu();
  if (mpuAddr == 0x68 || mpuAddr == 0x69) mpuOk = mpu.begin(mpuAddr, &sensorWire);
  else mpuOk = mpu.begin(0x68, &sensorWire);           // 默认地址兜底
  if (!mpuOk) {
    Serial.println("\n[MPU6050] 未找到/初始化失败。请按上面扫描结果排查：");
    Serial.println("  1) 无任何设备 → VCC/GND/SDA(13)/SCL(14) 接线松动或没共地、MPU 没供电(3V3)");
    Serial.println("  2) 只缺 0x68 → 模块 AD0 若悬空=0x69，请把 AD0 接到 GND（或用 0x69 地址）");
    Serial.println("  3) 杜邦线接触不良（GPS 项目曾踩过同样坑）→ 重新插紧或换线");
    while (1) delay(10);
  }
  Serial.printf("MPU6050 初始化成功 (地址 0x%02X)\n", mpuAddr ? mpuAddr : 0x68);
  mpu.setAccelerometerRange(MPU6050_RANGE_8_G);   // 加速度量程：±8g
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);     // 低通带宽：21Hz

  // GPS UART2，只接收
  GPSSerial.setRxBufferSize(2048);  // NMEA 接收缓冲
  GPSSerial.begin(9600, SERIAL_8N1, GPS_RX, GPS_TX);
  Serial.println("GPS UART 初始化完成 (GPS实际 NMEA 输出 -> GPIO33, 9600 baud)");

  // MAC 设备标识与随机启动标识
  uint32_t macHex = (uint32_t)(ESP.getEfuseMac() & 0xFFFFFF);
  snprintf(deviceId, sizeof(deviceId), "esp32-%06x", macHex);
  snprintf(bootIdStr, sizeof(bootIdStr), "%08x", (uint32_t)esp_random());

  // 联网：AP+STA + AP 调试页
  wifiSetup();
  server.on("/", HTTP_GET, handleRoot);
  cameraSetup(server, BACKEND_URL, deviceId, bootIdStr);
  server.onNotFound(handleNotFound);
  server.begin();
  Serial.printf("设备身份: device=%s boot=%s\n", deviceId, bootIdStr);
  Serial.printf("后端地址: %s\n", BACKEND_URL);

  Serial.println("请在 3 秒内把设备摆成正常工作姿态（保持静止）...");
  delay(ARM_DELAY_MS);

  // 竖直方向校准：50 次均值
  float sx = 0, sy = 0, sz = 0;
  const int N = 50;
  for (int i = 0; i < N; i++) {
    mpu.getEvent(&accel, &gyro, &temp);
    sx += accel.acceleration.x;
    sy += accel.acceleration.y;
    sz += accel.acceleration.z;
    delay(10);
  }
  float rm = mag(sx, sy, sz);
  if (rm > 0.1f) {
    refX = sx / rm; refY = sy / rm; refZ = sz / rm;
  }
  detectionEvents = xQueueCreate(PEND_BUF_SIZE, sizeof(PendingEvent));
  if (!detectionEvents) {
    Serial.println("[致命] 无法分配事件队列，请检查内存后重新启动；未进入监测。");
    while (true) delay(1000);
  }
  fallDetector.begin(WearableFallDetector::Vec(refX, refY, refZ), millis());
  if (ENABLE_IMU_STREAM) imuStreamSetup(BACKEND_URL, deviceId, bootIdStr);
  imuTaskRunning = xTaskCreatePinnedToCore(motionTask, "imu-detection", 4096, nullptr, 2, nullptr, 1) == pdPASS;
  if (!imuTaskRunning) Serial.println("[IMU] 独立采样任务创建失败，降级为主循环采样；网络可能影响采样，检查内存。");
  else Serial.println("[IMU] 人体佩戴检测：50Hz独立采样，冲击/姿态变化/连续静止联合判定。");
  cameraStartSchedule(millis());
  Serial.println("校准完成，开始监测。每 5 分钟拍照；仅 FALL/AIR 触发报警拍照。");
}


void loop() {
  if (!imuTaskRunning) {
    static uint32_t lastFallbackSample = 0;
    if (millis() - lastFallbackSample >= IMU_SAMPLE_MS) {
      lastFallbackSample = millis();
      sampleMotion();
    }
  }
  MotionSnapshot snapshot = readMotionSnapshot();
  gM = snapshot.magnitude;
  gAngle = snapshot.angle;
  state = static_cast<State>(snapshot.state);
  readGPS();
  PendingEvent queued;
  // 每轮最多处理一条事件
  if (xQueueReceive(detectionEvents, &queued, 0) == pdTRUE) {
    sendEvent(queued.etype, queued.up_ms, queued.seq);
  }
  unsigned long now = millis();

  cameraTick(now);

  // 空气采样与事件判定，周期 200ms
  airSample(now);

  // GPS 调度：每 5s 检查新定位
  if (now - lastGpsCheck >= GPS_UPLOAD_MS) {
    lastGpsCheck = now;
    sendGpsLocation();
  }

  // 网络重连与周期上报
  wifiCheck();
  server.handleClient();
  if (now - lastSend >= SEND_MS) {
    lastSend = now;
    sendState();
  }

  // 诊断输出：500ms
  if (now - lastDbg >= 500) {
    lastDbg = now;
    snapshot = readMotionSnapshot();
    Serial.printf("模值=%.2fg  倾角=%.1f°  状态=%d  STA=%s  GPS卫星=%d\n",
                  snapshot.magnitude, snapshot.angle, (int)snapshot.state,
                  WiFi.status() == WL_CONNECTED ? "连" : "断",
                  gpsFix.sats >= 0 ? gpsFix.sats : 0);
    Serial.printf("[判定] IMU=%s age=%lums gyro=%.1fdeg/s pose=%.1fdeg peak=%.2fg rotation=%.1fdeg low=%d gap=%u maxGap=%ums\n",
                  snapshot.valid ? "OK" : "INVALID", (unsigned long)(millis() - snapshot.sampleMs),
                  snapshot.gyroDps, snapshot.poseChange, snapshot.peak, snapshot.rotation,
                  snapshot.lowEvidence ? 1 : 0, snapshot.gaps, snapshot.maxGapMs);
  }
}
