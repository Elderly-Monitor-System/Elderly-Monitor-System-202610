#include "camera_service.h"
#include "board_pins.h"
#include "photo_schedule.h"
#include <HTTPClient.h>
#include <WiFi.h>
#include <esp_camera.h>
#include <esp_heap_caps.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

namespace {
constexpr size_t MAX_JPEG = 512 * 1024;
constexpr int REQUEST_CAPACITY = 8;
constexpr int UPLOAD_CAPACITY = 4;
constexpr uint32_t UPLOAD_TIMEOUT_MS = 4000;

struct CaptureRequest {
  char reason[8];
  uint32_t eventSeq;
  uint32_t triggerMs;
};
struct Photo {
  uint8_t* data;
  size_t len;
  uint32_t seq, captureMs;
  uint16_t width, height;
  CaptureRequest request;
};
struct CameraStatus {
  bool ready = false;
  uint32_t captured = 0, uploaded = 0, failed = 0, dropped = 0;
  uint32_t latestSeq = 0;
  int lastCode = 0;
};

QueueHandle_t requests = nullptr, uploads = nullptr;
SemaphoreHandle_t cacheLock = nullptr;
portMUX_TYPE statusMux = portMUX_INITIALIZER_UNLOCKED;
CameraStatus status;
uint8_t* latestJpeg = nullptr;
size_t latestLen = 0;
String uploadUrl;
char device[33], boot[17];
WebServer* web = nullptr;
PhotoSchedule schedule;

CameraStatus snapshot() {
  portENTER_CRITICAL(&statusMux);
  CameraStatus copy = status;
  portEXIT_CRITICAL(&statusMux);
  return copy;
}
void countFailure(bool dropped = false) {
  portENTER_CRITICAL(&statusMux);
  if (dropped) status.dropped++; else status.failed++;
  portEXIT_CRITICAL(&statusMux);
}
bool enqueue(const char* reason, uint32_t seq, uint32_t now) {
  if (!requests || !snapshot().ready) {
    countFailure();
    Serial.printf("[Camera] not ready; missed %s seq=%u\n", reason, seq);
    return false;
  }
  CaptureRequest req = {};
  strlcpy(req.reason, reason, sizeof(req.reason));
  req.eventSeq = seq;
  req.triggerMs = now;
  // 报警优先，非阻塞入队
  BaseType_t ok = PhotoSchedule::isPhotoAlarm(reason)
      ? xQueueSendToFront(requests, &req, 0) : xQueueSend(requests, &req, 0);
  if (ok != pdTRUE) {
    countFailure(true);
    Serial.printf("[Camera] capture queue full; missed %s seq=%u\n", reason, seq);
  }
  return ok == pdTRUE;
}

void uploadTask(void*) {
  Photo photo;
  for (;;) {
    if (xQueueReceive(uploads, &photo, portMAX_DELAY) != pdTRUE) continue;
    // 重传原图，不重复拍照
    // 内存队列：4 张待传、1 张重试；掉电丢失
    for (;;) {
      int code = -1;
      if (WiFi.status() == WL_CONNECTED) {
        HTTPClient http;
        http.setConnectTimeout(UPLOAD_TIMEOUT_MS);
        http.setTimeout(UPLOAD_TIMEOUT_MS);
        if (http.begin(uploadUrl)) {
          http.addHeader("Content-Type", "image/jpeg");
          http.addHeader("X-Device-Id", device);
          http.addHeader("X-Boot-Id", boot);
          http.addHeader("X-Photo-Seq", String(photo.seq));
          http.addHeader("X-Photo-Reason", photo.request.reason);
          http.addHeader("X-Event-Seq", String(photo.request.eventSeq));
          http.addHeader("X-Trigger-Up-Ms", String(photo.request.triggerMs));
          http.addHeader("X-Capture-Up-Ms", String(photo.captureMs));
          code = http.POST(photo.data, photo.len);
        }
        http.end();
      }
      portENTER_CRITICAL(&statusMux);
      status.lastCode = code;
      if (code == 200 || code == 201) status.uploaded++;
      portEXIT_CRITICAL(&statusMux);
      if (code == 200 || code == 201) break;
      if (code >= 400 && code < 500 && code != 408 && code != 429) {
        Serial.printf("[Camera] upload rejected HTTP %d photo=%u\n", code, photo.seq);
        countFailure();
        break;  // 丢弃永久非法报文
      }
      vTaskDelay(pdMS_TO_TICKS(5000));
    }
    free(photo.data);
  }
}

void captureTask(void*) {
  // SCCB 与 MPU 分用 I2C
  camera_config_t cfg = {};
  cfg.ledc_channel = LEDC_CHANNEL_0;
  cfg.ledc_timer = LEDC_TIMER_0;
  cfg.pin_d0 = CAM_D0; cfg.pin_d1 = CAM_D1;
  cfg.pin_d2 = CAM_D2; cfg.pin_d3 = CAM_D3;
  cfg.pin_d4 = CAM_D4; cfg.pin_d5 = CAM_D5;
  cfg.pin_d6 = CAM_D6; cfg.pin_d7 = CAM_D7;
  cfg.pin_xclk = CAM_XCLK; cfg.pin_pclk = CAM_PCLK;
  cfg.pin_vsync = CAM_VSYNC; cfg.pin_href = CAM_HREF;
  cfg.pin_sccb_sda = CAM_SIOD; cfg.pin_sccb_scl = CAM_SIOC;
  cfg.pin_pwdn = CAM_PWDN; cfg.pin_reset = CAM_RESET;
  cfg.xclk_freq_hz = 20000000;
  cfg.pixel_format = PIXFORMAT_JPEG;
  cfg.frame_size = FRAMESIZE_SVGA;  // JPEG 800×600
  cfg.jpeg_quality = 12;
  cfg.fb_count = 1;
  cfg.fb_location = CAMERA_FB_IN_PSRAM;
  cfg.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  if (!psramFound()) {
    Serial.println("[Camera] PSRAM unavailable; enable PSRAM. Sensors continue.");
    countFailure();
    vTaskDelete(nullptr);
    return;
  }
  esp_err_t err = esp_camera_init(&cfg);
  if (err != ESP_OK) {
    Serial.printf("[Camera] init failed 0x%x; sensors continue\n", err);
    countFailure();
    vTaskDelete(nullptr);
    return;
  }
  portENTER_CRITICAL(&statusMux);
  status.ready = true;
  portEXIT_CRITICAL(&statusMux);
  uint32_t photoSeq = 0;
  CaptureRequest req;
  for (;;) {
    if (xQueueReceive(requests, &req, portMAX_DELAY) != pdTRUE) continue;
    // 丢弃积压帧后取新帧
    camera_fb_t* fb = esp_camera_fb_get();
    if (fb) esp_camera_fb_return(fb);
    fb = esp_camera_fb_get();
    if (!fb) { countFailure(); continue; }
    Photo photo = {};
    photo.seq = ++photoSeq;
    photo.captureMs = millis();
    photo.width = fb->width; photo.height = fb->height;
    photo.request = req;
    if (fb->format != PIXFORMAT_JPEG || fb->len > MAX_JPEG) {
      esp_camera_fb_return(fb); countFailure(); continue;
    }
    photo.len = fb->len;
    photo.data = static_cast<uint8_t*>(heap_caps_malloc(photo.len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (photo.data) memcpy(photo.data, fb->buf, photo.len);
    esp_camera_fb_return(fb);
    if (!photo.data) { countFailure(); continue; }
    if (xSemaphoreTake(cacheLock, pdMS_TO_TICKS(100)) == pdTRUE) {
      uint8_t* copy = static_cast<uint8_t*>(heap_caps_malloc(photo.len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
      if (copy) {
        memcpy(copy, photo.data, photo.len);
        free(latestJpeg); latestJpeg = copy; latestLen = photo.len;
        portENTER_CRITICAL(&statusMux);
        status.latestSeq = photo.seq;
        portEXIT_CRITICAL(&statusMux);
      }
      xSemaphoreGive(cacheLock);
    }
    portENTER_CRITICAL(&statusMux);
    status.captured++;
    portEXIT_CRITICAL(&statusMux);
    Serial.printf("[Camera] captured %s photo=%u event=%u %u bytes\n",
                  req.reason, photo.seq, req.eventSeq, static_cast<unsigned>(photo.len));
    if (xQueueSend(uploads, &photo, 0) != pdTRUE) {
      free(photo.data);
      countFailure(true);
      Serial.println("[Camera] offline photo buffer full; photo only in latest preview");
    }
  }
}

const char CAMERA_PAGE[] PROGMEM = R"HTML(<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>设备摄像头</title><style>body{font:16px/1.7 system-ui;background:#101b2c;color:#e0e9f5;max-width:800px;margin:auto;padding:24px}a{color:#80c7c8}button{font:inherit;padding:10px 18px;cursor:pointer}img{display:block;max-width:100%;margin-top:20px}p{overflow-wrap:anywhere}</style><h1>设备摄像头</h1><p>每五分钟定时拍照；跌倒与空气超标各拍一张。倾倒不拍照。</p><button id="take">拍一张</button> <a href="/">设备诊断</a><p id="status" role="status">读取状态…</p><img id="photo" alt="最近一次拍摄的画面" hidden><script>
let shown=0, baseline=0, waiting=false, timer=0;
async function refresh(){try{const r=await fetch('/camera/status',{signal:AbortSignal.timeout(5000)});if(!r.ok)throw Error();const s=await r.json();document.getElementById('status').textContent=(s.ready?'摄像头就绪':'摄像头未就绪，请检查串口、PSRAM和接线')+' · 已拍 '+s.captured+' · 已上传 '+s.uploaded+' · 失败 '+s.failed+' · 队满丢弃 '+s.dropped;if(s.latest_seq&&s.latest_seq!==shown){shown=s.latest_seq;const img=document.getElementById('photo');img.src='/camera.jpg?v='+shown;img.hidden=false}if(waiting&&(s.latest_seq>baseline||Date.now()-timer>15000)){waiting=false;document.getElementById('take').disabled=false;if(s.latest_seq<=baseline)document.getElementById('status').textContent+=' · 拍照未完成，请稍后重试'}}catch(e){document.getElementById('status').textContent='无法连接设备，请检查 WiFi';waiting=false;document.getElementById('take').disabled=false}}
document.getElementById('take').onclick=async()=>{const b=document.getElementById('take');b.disabled=true;baseline=shown;try{const r=await fetch('/camera/capture',{method:'POST',signal:AbortSignal.timeout(5000)});if(!r.ok)throw Error();waiting=true;timer=Date.now()}catch(e){document.getElementById('status').textContent='拍照请求失败，摄像头可能未就绪或队列已满';b.disabled=false}};refresh();setInterval(refresh,1500);
</script></html>)HTML";
}

void cameraSetup(WebServer& server, const char* ingestUrl, const char* deviceId, const char* bootId) {
  web = &server;
  strlcpy(device, deviceId, sizeof(device)); strlcpy(boot, bootId, sizeof(boot));
  uploadUrl = String(ingestUrl);
  int suffix = uploadUrl.lastIndexOf("/api/ingest");
  if (suffix < 0) { Serial.println("[Camera] invalid backend URL"); return; }
  uploadUrl = uploadUrl.substring(0, suffix) + "/api/photos";
  requests = xQueueCreate(REQUEST_CAPACITY, sizeof(CaptureRequest));
  uploads = xQueueCreate(UPLOAD_CAPACITY, sizeof(Photo));
  cacheLock = xSemaphoreCreateMutex();
  server.on("/camera", HTTP_GET, [] { web->send_P(200, "text/html; charset=utf-8", CAMERA_PAGE); });
  server.on("/camera/status", HTTP_GET, [] {
    CameraStatus s = snapshot();
    char json[320];
    snprintf(json, sizeof(json), "{\"ready\":%s,\"captured\":%u,\"uploaded\":%u,\"failed\":%u,\"dropped\":%u,\"latest_seq\":%u,\"last_http\":%d,\"interval_ms\":%u}",
             s.ready ? "true" : "false", s.captured, s.uploaded, s.failed, s.dropped,
             s.latestSeq, s.lastCode, PhotoSchedule::INTERVAL_MS);
    web->sendHeader("Cache-Control", "no-store");
    web->send(200, "application/json", json);
  });
  server.on("/camera/capture", HTTP_POST, [] {
    bool ok = enqueue("MANUAL", 0, millis());
    web->send(ok ? 202 : 503, "application/json", ok ? "{\"queued\":true}" : "{\"queued\":false}");
  });
  auto image = [] {
    if (!cacheLock || xSemaphoreTake(cacheLock, pdMS_TO_TICKS(30)) != pdTRUE) {
      web->send(503, "text/plain", "Camera busy"); return;
    }
    if (!latestJpeg) { xSemaphoreGive(cacheLock); web->send(404, "text/plain", "No photo yet"); return; }
    size_t len = latestLen;
    uint8_t* copy = static_cast<uint8_t*>(heap_caps_malloc(len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (copy) memcpy(copy, latestJpeg, len);
    xSemaphoreGive(cacheLock);
    if (!copy) { web->send(503, "text/plain", "No memory"); return; }
    web->sendHeader("Cache-Control", "no-store");
    web->send_P(200, "image/jpeg", reinterpret_cast<const char*>(copy), len);
    free(copy);
  };
  server.on("/camera.jpg", HTTP_GET, image);
  server.on("/capture", HTTP_GET, image);  // 兼容接口：读取最近照片
  if (!requests || !uploads || !cacheLock) { countFailure(); return; }
  if (xTaskCreatePinnedToCore(uploadTask, "photo-upload", 6144, nullptr, 1, nullptr, 0) != pdPASS) {
    countFailure(); return;
  }
  if (xTaskCreatePinnedToCore(captureTask, "photo-capture", 6144, nullptr, 1, nullptr, 0) != pdPASS) countFailure();
}

void cameraStartSchedule(uint32_t now) { schedule.begin(now); }
void cameraTick(uint32_t now) {
  if (schedule.due(now)) enqueue("TIMER", 0, now);
}
void cameraOnAlarm(const char* type, uint32_t eventSeq, uint32_t triggerMs) {
  if (PhotoSchedule::isPhotoAlarm(type)) enqueue(type, eventSeq, triggerMs);
}
