#pragma once
#include <sdkconfig.h>

// SCCB 控制器由核心指定
// MPU 使用另一 I2C
#if defined(CONFIG_SCCB_HARDWARE_I2C_PORT1) && CONFIG_SCCB_HARDWARE_I2C_PORT1
constexpr int CAMERA_I2C_PORT = 1;
#else
constexpr int CAMERA_I2C_PORT = 0;
#endif
constexpr int MPU_I2C_PORT = 1 - CAMERA_I2C_PORT;

// ESP32-WROVER + OV2640 引脚
// 保留：6~11 Flash，16/17 PSRAM
constexpr int MPU_SDA_PIN = 13;
constexpr int MPU_SCL_PIN = 14;
constexpr int MQ_PIN = 32;       // ADC1_CH4，输入 ≤3.3V
constexpr int GPS_RX = 33;       // 接 GPS NMEA 输出
constexpr int GPS_TX = -1;       // 不启用 UART TX
constexpr int LED_PIN = 2;       // 板载 LED；禁用设 -1
constexpr int BUZZER_PIN = -1;   // 默认禁用；可选 GPIO15 驱动

constexpr int CAM_PWDN = -1;
constexpr int CAM_RESET = -1;
constexpr int CAM_XCLK = 21;
constexpr int CAM_SIOD = 26;
constexpr int CAM_SIOC = 27;
constexpr int CAM_D0 = 4;
constexpr int CAM_D1 = 5;
constexpr int CAM_D2 = 18;
constexpr int CAM_D3 = 19;
constexpr int CAM_D4 = 36;
constexpr int CAM_D5 = 39;
constexpr int CAM_D6 = 34;
constexpr int CAM_D7 = 35;
constexpr int CAM_VSYNC = 25;
constexpr int CAM_HREF = 23;
constexpr int CAM_PCLK = 22;
