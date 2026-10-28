#pragma once

// 六轴有界队列与独立上传任务
bool imuStreamSetup(const char* ingestUrl, const char* deviceId, const char* bootId);
void imuStreamSample(float ax, float ay, float az, float gx, float gy, float gz);
