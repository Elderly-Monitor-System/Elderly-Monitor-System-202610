#pragma once
#include <WebServer.h>
#include <stdint.h>

// 摄像头服务，故障不停止监测
void cameraSetup(WebServer& server, const char* ingestUrl,
                 const char* deviceId, const char* bootId);
void cameraStartSchedule(uint32_t now);
void cameraTick(uint32_t now);
void cameraOnAlarm(const char* type, uint32_t eventSeq, uint32_t triggerMs);
