#include "../wearable_fall_detector.h"
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>

using Detector = WearableFallDetector;
using Vec = Detector::Vec;
static int checks = 0;
static void expect(bool condition, const char* name) {
  ++checks;
  if (!condition) { std::fprintf(stderr, "FAIL: %s\n", name); std::exit(1); }
}

struct Replay {
  Detector detector;
  uint32_t now;
  int falls = 0, tilts = 0;
  bool alternateMount;
  explicit Replay(uint32_t start = 0, bool alternate = false) : now(start), alternateMount(alternate) {
    detector.begin(transform(Vec(0, 0, 1)), now);
  }
  Vec transform(Vec v) const { return alternateMount ? Vec(v.z, -v.y, v.x) : v; }
  void step(Vec acceleration, Vec gyro = Vec(), uint32_t delta = 20) {
    now += delta;
    Detector::Event e = detector.update(Detector::Sample(now, transform(acceleration), transform(gyro)));
    falls += e == Detector::FALL;
    tilts += e == Detector::TILT;
  }
  void hold(int ms, float degrees = 0, float magnitude = 1, float gyroSpeed = 0) {
    float angle = degrees * 0.01745329252f;
    for (int i = 0; i < ms / 20; ++i)
      step(Vec(std::sin(angle) * magnitude, 0, std::cos(angle) * magnitude), Vec(0, gyroSpeed, 0));
  }
  void rotate(float from, float to, int ms) {
    int count = ms / 20;
    for (int i = 1; i <= count; ++i) {
      float a = (from + (to - from) * i / count) * 0.01745329252f;
      step(Vec(std::sin(a), 0, std::cos(a)), Vec(0, std::fabs(to - from) * 1000.0f / ms, 0));
    }
  }
  void impact(float magnitude = 3.2f, float angle = 0) { hold(20, angle, magnitude); }
  void fall(bool low = true, float peak = 3.2f, float from = 0, float to = 90) {
    if (low) hold(100, from, 0.2f);
    impact(peak, from);
    rotate(from, to, 400);
    hold(1800, to);
  }
};

int main() {
  {
    Replay r; r.hold(30000);
    expect(r.falls == 0 && r.tilts == 0, "standing does not alarm");
  }
  {
    Replay r;
    for (int i = 0; i < 1500; ++i) {
      float a = 0.15f * std::sin(i * 0.2f), m = 1 + 0.18f * std::sin(i * 0.35f);
      r.step(Vec(std::sin(a) * m, 0, std::cos(a) * m), Vec(0, 15 * std::sin(i * 0.2f), 0));
    }
    expect(r.falls == 0 && r.tilts == 0, "walking-like oscillations do not alarm");
  }
  {
    Replay r; r.hold(1000); r.fall();
    expect(r.falls == 1, "low-g, impact, rotation, lying still confirms a fall");
    expect(r.detector.impactPeak() >= 3.1f, "fall captures impact peak");
    expect(r.detector.lowEvidence(), "qualified low-g retained as evidence");
    r.hold(15000, 90);
    expect(r.falls == 1 && r.tilts == 0, "lying after confirmed fall does not flood FALL or TILT");
  }
  {
    Replay r; r.hold(1000); r.fall(false);
    expect(r.falls == 1, "strong impact fall does not require ideal free fall");
  }
  {
    Replay r; r.hold(1000); r.fall(true, 1.9f);
    expect(r.falls == 1, "qualified low-g enables lower impact path");
  }
  {
    Replay r; r.hold(1000); r.fall(false, 1.9f);
    expect(r.falls == 0, "moderate impact without low-g does not use low-g path");
  }
  {
    Replay r; r.hold(1000); r.hold(20, 0, 0.2f); r.fall(false, 1.9f);
    expect(r.falls == 0, "one low-g sample is not qualified free fall");
  }
  {
    Replay r; r.hold(1000); r.hold(100, 0, 0.2f); r.hold(1400); r.fall(false, 1.9f);
    expect(r.falls == 0, "expired low-g evidence cannot help later moderate impact");
  }
  {
    Replay r; r.hold(1000);
    for (int i = 0; i < 4; ++i) { r.impact(); r.hold(4500); }
    expect(r.falls == 0, "bumps with unchanged posture are not falls");
  }
  {
    Replay r; r.hold(1000); r.hold(100, 0, 0.2f); r.impact(); r.hold(4500);
    expect(r.falls == 0, "jump and landing upright is not a fall");
  }
  {
    Replay r; r.hold(1000); r.rotate(0, 90, 2000); r.hold(4000, 90);
    expect(r.falls == 0, "slow voluntary lying down without impact is not FALL");
    expect(r.tilts >= 1, "original TILT warning is retained");
  }
  {
    Replay r; r.hold(1000); r.rotate(0, 90, 2000); r.hold(2000, 90);
    r.impact(3.2f, 90); r.hold(4500, 90);
    expect(r.falls == 0, "impact while already lying must change pre-impact posture");
  }
  {
    Replay r; r.hold(1000); r.impact(); r.rotate(0, 90, 400);
    r.hold(800, 90); r.hold(100, 90, 1.5f); r.hold(800, 90);
    expect(r.falls == 0, "interrupted stillness is not cumulative stillness");
    r.hold(600, 90);
    expect(r.falls == 1, "new continuous stillness can confirm within window");
  }
  {
    Replay r; r.hold(1000); r.impact(); r.rotate(0, 90, 400); r.hold(4400, 90, 1, 40);
    expect(r.falls == 0, "rotating at 1g is not stillness");
    r.hold(2000, 90);
    expect(r.falls == 0, "timed-out candidate cannot revive without new impact");
  }
  {
    Replay r; r.hold(1000); r.impact(); r.rotate(0, 90, 400); r.hold(800, 90);
    r.step(Vec(1, 0, 0), Vec(), 1500); r.hold(3000, 90);
    expect(r.falls == 0, "sampling gap cannot masquerade as stillness");
    expect(r.detector.gaps() == 1 && r.detector.maxGapMs() >= 1500, "sample gap is observable");
  }
  {
    Replay r; r.hold(1000); r.impact(); r.rotate(0, 90, 400); r.hold(800, 90);
    r.step(Vec(std::numeric_limits<float>::quiet_NaN(), 0, 0)); r.hold(3000, 90);
    expect(r.falls == 0 && r.detector.gaps() == 1, "invalid acceleration breaks confirmation");
  }
  {
    Replay r; r.hold(1000); r.impact(); r.rotate(0, 90, 400); r.hold(800, 90);
    r.step(Vec(1, 0, 0), Vec(0, std::numeric_limits<float>::infinity(), 0)); r.hold(3000, 90);
    expect(r.falls == 0, "invalid gyro breaks confirmation");
  }
  {
    Replay r; r.hold(1000); r.rotate(0, 60, 600); r.hold(300, 60);
    expect(r.detector.state() == Detector::TILT_WAIT, "fixture enters tilt wait");
    r.impact(3.2f, 60); r.rotate(60, 150, 400); r.hold(1800, 150);
    expect(r.falls == 1, "impact during TILT_WAIT is not swallowed");
  }
  {
    Replay r; r.hold(1000); r.rotate(0, 90, 1200); r.hold(1800, 90);
    expect(r.detector.state() == Detector::TILT_ALARM, "fixture enters TILT_ALARM");
    r.impact(3.2f, 90); r.rotate(90, 180, 400); r.hold(1800, 180);
    expect(r.falls == 1, "new impact during TILT_ALARM can become FALL");
  }
  {
    Replay r; r.hold(1000); r.fall();
    r.fall(false, 3.2f, 90, 180);
    expect(r.falls == 1, "cooldown suppresses repeated fall sequence");
    r.hold(8000, 180); r.fall(false, 3.2f, 180, 90);
    expect(r.falls == 2, "new fall can be detected after cooldown even before upright recovery");
  }
  {
    Replay r; r.hold(1000); r.fall(); r.rotate(90, 0, 1200); r.hold(1400);
    r.rotate(0, 90, 1200); r.hold(2200, 90);
    expect(r.tilts >= 1, "upright recovery restores TILT functionality");
  }
  {
    Replay r(0xFFFFF800u); r.hold(1000); r.fall();
    expect(r.falls == 1, "millis wrap does not break fall confirmation");
  }
  {
    Replay r(0, true); r.hold(1000); r.fall();
    expect(r.falls == 1, "algorithm is invariant to fixed mounting axis permutation");
  }
  {
    Replay r; r.hold(1000); r.impact(); r.rotate(0, 25, 400); r.hold(3000, 25);
    expect(r.falls == 0, "large rotation signal cannot replace required posture change");
  }
  {
    Replay r; r.hold(1000); r.impact(); r.hold(3000, 45);
    expect(r.falls == 0, "moderate posture change alone lacks supporting movement evidence");
  }
  {
    Replay r; r.hold(1000); r.fall();
    r.detector.begin(Vec(0, 0, 1), r.now);
    expect(!r.detector.lowEvidence(), "explicit restart clears previous low-g evidence");
    r.rotate(0, 90, 1200); r.hold(2200, 90);
    expect(r.tilts >= 1, "explicit restart clears previous fall latch");
  }
  std::printf("PASS: %d behavioral assertions across wearable synthetic motion replays.\n", checks);
  return 0;
}
