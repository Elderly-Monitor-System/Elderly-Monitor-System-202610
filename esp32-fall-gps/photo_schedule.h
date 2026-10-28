#pragma once
#include <stdint.h>
#include <string.h>

// 定时拍照：5 分钟，报警不重置
class PhotoSchedule {
 public:
  static constexpr uint32_t INTERVAL_MS = 5UL * 60UL * 1000UL;
  void begin(uint32_t now) { anchor_ = now; }
  bool due(uint32_t now) {
    uint32_t elapsed = now - anchor_;  // 无符号差值兼容 millis() 回绕
    if (elapsed < INTERVAL_MS) return false;
    anchor_ += (elapsed / INTERVAL_MS) * INTERVAL_MS;
    return true;                     // 延迟时仅补拍一张
  }
  static bool isPhotoAlarm(const char* type) {
    return strcmp(type, "FALL") == 0 || strcmp(type, "AIR") == 0;
  }
 private:
  uint32_t anchor_ = 0;
};
