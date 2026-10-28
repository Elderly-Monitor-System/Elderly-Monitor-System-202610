#pragma once
#include <math.h>
#include <stdint.h>

// 固件/测试共用判定器
// 参数尚未经真实佩戴数据评估
struct WearableFallConfig {
  float impactG = 2.5f;
  float lowG = 0.5f;
  uint32_t lowHoldMs = 60;
  uint32_t lowMemoryMs = 1000;
  float lowAssistedImpactG = 1.8f;
  float stillBandG = 0.20f;
  float stillGyroDps = 20.0f;
  float stillStepG = 0.12f;
  uint32_t stillMs = 1200;
  uint32_t settleDelayMs = 200;
  uint32_t fallWindowMs = 4000;
  float poseChangeDeg = 40.0f;
  float strongPoseDeg = 60.0f;
  float rotationEvidenceDeg = 25.0f;
  uint32_t prePoseAgeMs = 200;
  uint32_t maxSampleGapMs = 100;
  uint32_t fallCooldownMs = 8000;
  float uprightResetDeg = 30.0f;
  uint32_t uprightResetMs = 1000;
  float gravityTauMs = 180.0f;
  float tiltDeg = 45.0f;
  uint32_t tiltHoldMs = 1500;
  uint32_t tiltAlarmMs = 5000;
};

class WearableFallDetector {
 public:
  enum State { ARMED, TILT_WAIT, TILT_ALARM, FALL_IMPACT, FALL_STILL };
  enum Event { NONE, TILT, FALL };
  struct Vec {
    float x, y, z;
    Vec(float a = 0, float b = 0, float c = 0) : x(a), y(b), z(c) {}
  };
  struct Sample {
    uint32_t ms;
    Vec accelerationG;
    Vec gyroDps;
    Sample(uint32_t t, Vec a, Vec w = Vec()) : ms(t), accelerationG(a), gyroDps(w) {}
  };

  explicit WearableFallDetector(const WearableFallConfig& config = WearableFallConfig()) : cfg_(config) {}

  void begin(Vec upright, uint32_t now) {
    upright_ = unit(upright);
    gravity_ = upright_;
    previous_ = upright_;
    lastMs_ = now;
    state_ = ARMED;
    havePrevious_ = false;
    candidate_ = false;
    lowTiming_ = lowRecent_ = lowEvidence_ = stillTiming_ = tiltTiming_ = resetTiming_ = false;
    fallLatched_ = haveFall_ = tiltAlarm_ = false;
    historyCount_ = historyHead_ = 0;
    magnitude_ = 1; gyroSpeed_ = uprightAngle_ = poseChange_ = peak_ = rotation_ = 0;
    gapCount_ = maxGapMs_ = 0;
  }

  Event update(const Sample& sample) {
    const uint32_t now = sample.ms;
    uint32_t dt = havePrevious_ ? now - lastMs_ : 20;
    if (havePrevious_ && dt > maxGapMs_) maxGapMs_ = dt;
    lastMs_ = now;
    magnitude_ = length(sample.accelerationG);
    gyroSpeed_ = length(sample.gyroDps);
    if (!finite(sample.accelerationG) || !finite(sample.gyroDps) ||
        !isfinite(magnitude_) || !isfinite(gyroSpeed_) || magnitude_ > 30.0f || gyroSpeed_ > 2000.0f) {
      breakContinuity();
      havePrevious_ = false;
      ++gapCount_;
      return NONE;
    }
    const bool gap = havePrevious_ && dt > cfg_.maxSampleGapMs;
    float step = havePrevious_ ? length(sub(sample.accelerationG, previous_)) : 0.0f;
    previous_ = sample.accelerationG;
    havePrevious_ = true;
    if (gap) {
      breakContinuity();
      ++gapCount_;
      // 采样中断后重置连续性证据
      if (magnitude_ >= 0.7f && magnitude_ <= 1.3f) gravity_ = unit(sample.accelerationG);
      uprightAngle_ = angle(upright_, gravity_);
      remember(now);
      return NONE;
    }

    // 仅在正常重力区间更新姿态
    if (magnitude_ >= 0.7f && magnitude_ <= 1.3f) {
      float alpha = static_cast<float>(dt) / (cfg_.gravityTauMs + dt);
      gravity_ = unit(Vec(gravity_.x + alpha * (sample.accelerationG.x / magnitude_ - gravity_.x),
                          gravity_.y + alpha * (sample.accelerationG.y / magnitude_ - gravity_.y),
                          gravity_.z + alpha * (sample.accelerationG.z / magnitude_ - gravity_.z)));
    }
    uprightAngle_ = angle(upright_, gravity_);

    if (magnitude_ < cfg_.lowG) {
      if (!lowTiming_) { lowTiming_ = true; lowStart_ = now; }
      if (now - lowStart_ >= cfg_.lowHoldMs) { lowRecent_ = true; lastLow_ = now; }
    } else lowTiming_ = false;
    if (lowRecent_ && now - lastLow_ > cfg_.lowMemoryMs) lowRecent_ = false;

    const bool impact = magnitude_ >= cfg_.impactG ||
                        (lowRecent_ && magnitude_ >= cfg_.lowAssistedImpactG);
    // 新冲击优先于倾倒状态
    if (!candidate_ && impact && (!haveFall_ || now - lastFall_ >= cfg_.fallCooldownMs)) {
      candidate_ = true;
      candidateStart_ = now;
      preGravity_ = beforeImpact(now);
      lowEvidence_ = lowRecent_;
      peak_ = magnitude_;
      rotation_ = 0;
      poseChange_ = 0;
      stillTiming_ = tiltTiming_ = tiltAlarm_ = false;
      state_ = FALL_IMPACT;
      remember(now);
      return NONE;
    }

    Event event = NONE;
    if (candidate_) {
      if (magnitude_ > peak_) peak_ = magnitude_;
      rotation_ += gyroSpeed_ * (static_cast<float>(dt) / 1000.0f);
      poseChange_ = angle(preGravity_, gravity_);
      if (now - candidateStart_ > cfg_.fallWindowMs) {
        candidate_ = stillTiming_ = false;
        state_ = ARMED;
      } else {
        const bool still = now - candidateStart_ >= cfg_.settleDelayMs &&
            fabsf(magnitude_ - 1.0f) <= cfg_.stillBandG &&
            gyroSpeed_ <= cfg_.stillGyroDps && step <= cfg_.stillStepG;
        if (!still) {
          stillTiming_ = false;
          state_ = FALL_IMPACT;
        } else {
          state_ = FALL_STILL;
          if (!stillTiming_) { stillTiming_ = true; stillStart_ = now; }
          const bool evidence = lowEvidence_ || rotation_ >= cfg_.rotationEvidenceDeg ||
                                poseChange_ >= cfg_.strongPoseDeg;
          if (now - stillStart_ >= cfg_.stillMs && poseChange_ >= cfg_.poseChangeDeg && evidence) {
            candidate_ = stillTiming_ = false;
            fallLatched_ = haveFall_ = true;
            resetTiming_ = false;
            lastFall_ = now;
            state_ = ARMED;
            event = FALL;
          }
        }
      }
    } else {
      event = updateTilt(now);
    }
    remember(now);
    return event;
  }

  State state() const { return state_; }
  float magnitude() const { return magnitude_; }
  float uprightAngle() const { return uprightAngle_; }
  float gyroSpeed() const { return gyroSpeed_; }
  float poseChange() const { return poseChange_; }
  float impactPeak() const { return peak_; }
  float rotation() const { return rotation_; }
  bool lowEvidence() const { return lowEvidence_; }
  uint32_t gaps() const { return gapCount_; }
  uint32_t maxGapMs() const { return maxGapMs_; }

 private:
  static bool finite(Vec v) { return isfinite(v.x) && isfinite(v.y) && isfinite(v.z); }
  static float length(Vec v) { return sqrtf(v.x * v.x + v.y * v.y + v.z * v.z); }
  static Vec sub(Vec a, Vec b) { return Vec(a.x - b.x, a.y - b.y, a.z - b.z); }
  static Vec unit(Vec v) {
    float n = length(v);
    return finite(v) && n > 0.001f ? Vec(v.x / n, v.y / n, v.z / n) : Vec(0, 0, 1);
  }
  static float angle(Vec a, Vec b) {
    float n = length(a) * length(b);
    if (n < 0.001f) return 0;
    float c = (a.x * b.x + a.y * b.y + a.z * b.z) / n;
    c = c < -1 ? -1 : (c > 1 ? 1 : c);
    return acosf(c) * 57.2957795131f;
  }
  void remember(uint32_t now) {
    history_[historyHead_] = gravity_;
    historyMs_[historyHead_] = now;
    historyHead_ = (historyHead_ + 1) % HISTORY;
    if (historyCount_ < HISTORY) ++historyCount_;
  }
  Vec beforeImpact(uint32_t now) const {
    for (uint8_t n = 0; n < historyCount_; ++n) {
      uint8_t i = (historyHead_ + HISTORY - 1 - n) % HISTORY;
      uint32_t age = now - historyMs_[i];
      if (age >= cfg_.prePoseAgeMs && age <= 800) return history_[i];
    }
    return gravity_;
  }
  void breakContinuity() {
    candidate_ = stillTiming_ = lowTiming_ = lowRecent_ = tiltTiming_ = resetTiming_ = false;
    tiltAlarm_ = false;
    historyCount_ = historyHead_ = 0;
    state_ = ARMED;
  }
  Event updateTilt(uint32_t now) {
    if (fallLatched_) {
      // 跌倒锁存，竖直稳定后解除
      if (uprightAngle_ < cfg_.uprightResetDeg) {
        if (!resetTiming_) { resetTiming_ = true; resetStart_ = now; }
        else if (now - resetStart_ >= cfg_.uprightResetMs) fallLatched_ = false;
      } else resetTiming_ = false;
      state_ = ARMED;
      return NONE;
    }
    if (tiltAlarm_ && now - tiltAlarmStart_ < cfg_.tiltAlarmMs) { state_ = TILT_ALARM; return NONE; }
    tiltAlarm_ = false;
    if (uprightAngle_ > cfg_.tiltDeg) {
      if (!tiltTiming_) { tiltTiming_ = true; tiltStart_ = now; }
      state_ = TILT_WAIT;
      if (now - tiltStart_ >= cfg_.tiltHoldMs) {
        tiltTiming_ = false;
        tiltAlarm_ = true;
        tiltAlarmStart_ = now;
        state_ = TILT_ALARM;
        return TILT;
      }
    } else { tiltTiming_ = false; state_ = ARMED; }
    return NONE;
  }

  static const uint8_t HISTORY = 32;
  WearableFallConfig cfg_;
  Vec upright_, gravity_, previous_, preGravity_;
  Vec history_[HISTORY];
  uint32_t historyMs_[HISTORY];
  uint8_t historyCount_ = 0, historyHead_ = 0;
  State state_ = ARMED;
  bool havePrevious_ = false, candidate_ = false, lowTiming_ = false, lowRecent_ = false;
  bool lowEvidence_ = false, stillTiming_ = false, tiltTiming_ = false, tiltAlarm_ = false;
  bool resetTiming_ = false, fallLatched_ = false, haveFall_ = false;
  uint32_t lastMs_ = 0, lowStart_ = 0, lastLow_ = 0, candidateStart_ = 0, stillStart_ = 0;
  uint32_t tiltStart_ = 0, tiltAlarmStart_ = 0, resetStart_ = 0, lastFall_ = 0;
  uint32_t gapCount_ = 0, maxGapMs_ = 0;
  float magnitude_ = 1, gyroSpeed_ = 0, uprightAngle_ = 0, poseChange_ = 0, peak_ = 0, rotation_ = 0;
};
