#include "imu_stream.h"
#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

namespace {
struct Sample { uint64_t up; float axis[6]; };
QueueHandle_t samples = nullptr;
String url;
char device[33], boot[17];
portMUX_TYPE metricsMux = portMUX_INITIALIZER_UNLOCKED;
uint32_t dropped = 0;

uint64_t upMs() { return static_cast<uint64_t>(esp_timer_get_time()) / 1000; }

void uploadTask(void*) {
  Sample batch[25];
  char body[4096];
  uint32_t seq = 0, sent = 0;
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      Sample ignored;
      while (xQueueReceive(samples, &ignored, 0) == pdTRUE) {}
      vTaskDelay(pdMS_TO_TICKS(200));
      continue;
    }
    if (xQueueReceive(samples, &batch[0], pdMS_TO_TICKS(500)) != pdTRUE) continue;
    uint64_t now = upMs();
    if (now - batch[0].up > 1000) continue;  // 过期批次丢弃
    size_t count = 1;
    uint64_t start = now;
    while (count < 25 && upMs() - start < 500) {
      if (xQueueReceive(samples, &batch[count], pdMS_TO_TICKS(25)) == pdTRUE) ++count;
    }
    now = upMs();
    if (now - batch[0].up > 1500) continue;
    int off = snprintf(body, sizeof(body),
      "{\"version\":1,\"units\":\"g,dps\",\"device_id\":\"%s\",\"boot_id\":\"%s\","
      "\"batch_seq\":%u,\"sent_up_ms\":%llu,\"samples\":[",
      device, boot, ++seq, (unsigned long long)now);
    bool fits = off > 0 && static_cast<size_t>(off) < sizeof(body);
    for (size_t i = 0; i < count && fits; ++i) {
      int n = snprintf(body + off, sizeof(body) - off,
        "%s[%llu,%.5f,%.5f,%.5f,%.4f,%.4f,%.4f]", i ? "," : "",
        (unsigned long long)batch[i].up, batch[i].axis[0], batch[i].axis[1], batch[i].axis[2],
        batch[i].axis[3], batch[i].axis[4], batch[i].axis[5]);
      fits = n > 0 && static_cast<size_t>(n) < sizeof(body) - off;
      if (fits) off += n;
    }
    if (!fits || static_cast<size_t>(off) + 3 > sizeof(body)) continue;
    body[off++] = ']'; body[off++] = '}'; body[off] = '\0';
    HTTPClient http;
    http.setConnectTimeout(1000);
    http.setTimeout(1000);
    int code = -1;
    if (http.begin(url)) {
      http.addHeader("Content-Type", "application/json");
      code = http.POST(reinterpret_cast<uint8_t*>(body), off);
      // 响应不控制报警和拍照
    }
    http.end();
    if (code == 200) ++sent;
    if (seq % 20 == 0 || (code != 200 && seq % 5 == 0)) {
      portENTER_CRITICAL(&metricsMux);
      uint32_t drops = dropped;
      portEXIT_CRITICAL(&metricsMux);
      Serial.printf("[IMU上传] batch=%u code=%d ok=%u queueDrop=%u (后端模型仅影子判定)\n",
                    seq, code, sent, drops);
    }
    // 六轴不重传；缺批次重置窗口

  }
}
}

bool imuStreamSetup(const char* ingestUrl, const char* deviceId, const char* bootId) {
  if (samples) return true;
  url = ingestUrl;
  if (!url.endsWith("/api/ingest")) {
    Serial.println("[IMU上传] 未启用：后端地址应以 /api/ingest 结尾。");
    return false;
  }
  url.remove(url.length() - String("/api/ingest").length());
  url += "/api/imu";
  strlcpy(device, deviceId, sizeof(device)); strlcpy(boot, bootId, sizeof(boot));
  samples = xQueueCreate(100, sizeof(Sample));  // 队列上限：2s
  if (!samples) {
    Serial.println("[IMU上传] 队列分配失败；原检测/报警功能继续。");
    return false;
  }
  if (xTaskCreatePinnedToCore(uploadTask, "imu-upload", 12288, nullptr, 1, nullptr, 0) != pdPASS) {
    vQueueDelete(samples); samples = nullptr;
    Serial.println("[IMU上传] 网络任务创建失败；原检测/报警功能继续。");
    return false;
  }
  Serial.println("[IMU上传] 已启用：50Hz六轴，每25点一批，模型在电脑运行。");
  return true;
}

void imuStreamSample(float ax, float ay, float az, float gx, float gy, float gz) {
  if (!samples) return;
  Sample s = {upMs(), {ax, ay, az, gx, gy, gz}};
  for (float v : s.axis) if (!isfinite(v)) return;
  if (xQueueSend(samples, &s, 0) != pdTRUE) {
    portENTER_CRITICAL(&metricsMux);
    ++dropped;
    portEXIT_CRITICAL(&metricsMux);
  }
}
