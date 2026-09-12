"""解析器与信号处理的单元测试。

不需要硬件，用合成数据验证：
  - ThinkGear 解析器能处理任意切分的字节流、假同步头、坏校验
  - 眨眼检测器能从合成信号里找到眨眼
  - 眼势状态机能区分单眨/双眨/三眨/重眨
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supermouse.devices.bci.blink_detector import BlinkDetector
from supermouse.devices.bci.thinkgear import ThinkGearParser
from supermouse.modes.sharp_eyes.gestures import GestureRecognizer

FS = 250


# ---------------------------------------------------------------- 协议


def tg_packet(payload: bytes) -> bytes:
    return b"\xaa\xaa" + bytes([len(payload)]) + payload + bytes([(~sum(payload)) & 0xFF])


def raw_payload(value: int) -> bytes:
    return b"\x80\x02" + int(value).to_bytes(2, "big", signed=True)


class TestThinkGearParser:
    def test_real_frame_from_device(self):
        """用实测抓到的真实帧验证（2026-09-12 /dev/tty.usbmodem2017_2_251）。"""
        data = bytes.fromhex("aa aa 04 80 02 ff c3 bb".replace(" ", ""))
        rows = list(ThinkGearParser().feed(data))
        assert rows == [("raw", -61)]

    def test_multiple_codes_in_one_packet(self):
        payload = b"\x02\x00" + b"\x04\x40" + b"\x05\x32"
        rows = list(ThinkGearParser().feed(tg_packet(payload)))
        assert rows == [("poor_signal", 0), ("attention", 64), ("meditation", 50)]

    def test_byte_by_byte_split(self):
        """BLE/串口会把帧切成任意长度，逐字节喂也必须能解析出来。"""
        p = ThinkGearParser()
        stream = tg_packet(raw_payload(100)) + tg_packet(raw_payload(-100))
        got = []
        for b in stream:
            got.extend(p.feed(bytes([b])))
        assert got == [("raw", 100), ("raw", -100)]

    def test_bad_checksum_skipped(self):
        good = tg_packet(raw_payload(42))
        bad = bytearray(tg_packet(raw_payload(99)))
        bad[-1] ^= 0xFF
        p = ThinkGearParser()
        rows = list(p.feed(bytes(bad) + good))
        assert rows == [("raw", 42)]
        assert p.bad_checksum == 1

    def test_fake_sync_in_payload(self):
        """payload 里出现 aa aa 时不能被误认为同步头。"""
        p = ThinkGearParser()
        # 长度字节是 0xFF (255 > 169) → 必须跳过重找
        junk = b"\xaa\xaa\xff\x00\x00"
        rows = list(p.feed(junk + tg_packet(raw_payload(7))))
        assert rows == [("raw", 7)]

    def test_garbage_prefix(self):
        p = ThinkGearParser()
        rows = list(p.feed(b"\x01\x02\x03" + tg_packet(raw_payload(5))))
        assert rows == [("raw", 5)]

    def test_eeg_power_band_names(self):
        payload = b"\x83\x18" + bytes(range(24))
        rows = list(ThinkGearParser().feed(tg_packet(payload)))
        assert rows[0][0] == "eeg_power"
        assert set(rows[0][1]) == {"delta", "theta", "low_alpha", "high_alpha",
                                  "low_beta", "high_beta", "low_gamma", "mid_gamma"}

    def test_buffer_does_not_grow_unbounded(self):
        """长时间收到无同步头的垃圾数据时，缓冲不能无限膨胀。"""
        p = ThinkGearParser()
        for _ in range(200):
            list(p.feed(b"\x11" * 100))
        assert len(p.buf) <= 2


# ---------------------------------------------------------------- 眨眼检测


def synth_eeg(seconds: float, blinks: list[tuple[float, float, float]],
              fs: int = FS, seed: int = 0) -> np.ndarray:
    """合成脑电：底噪 + 若干眨眼尖峰。

    blinks: [(起始秒, 幅度, 时长秒), ...]
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * fs)
    t = np.arange(n) / fs
    # 底噪：宽带随机 + 10 Hz alpha
    sig = rng.normal(0, 8, n) + 6 * np.sin(2 * np.pi * 10 * t)
    for start, amp, dur in blinks:
        i0 = int(start * fs)
        i1 = min(int((start + dur) * fs), n)
        if i1 <= i0:
            continue
        # 眨眼是单个正向慢波（半个正弦周期）
        sig[i0:i1] += amp * np.sin(np.linspace(0, np.pi, i1 - i0))
    return sig


class TestBlinkDetector:
    def test_detects_single_blink(self):
        det = BlinkDetector(fs=FS)
        sig = synth_eeg(6.0, [(3.0, 220, 0.22)])
        found = []
        for i in range(0, len(sig), 32):
            found += det.feed(sig[i:i + 32])
        assert len(found) == 1, f"应检测到 1 次眨眼，实际 {len(found)}"
        assert 120 < found[0].duration_ms < 500

    def test_detects_double_blink(self):
        det = BlinkDetector(fs=FS)
        sig = synth_eeg(6.0, [(3.0, 200, 0.2), (3.25, 200, 0.2)])
        found = []
        for i in range(0, len(sig), 32):
            found += det.feed(sig[i:i + 32])
        assert len(found) == 2, f"应检测到 2 次眨眼，实际 {len(found)}"

    def test_hard_blink_has_higher_strength(self):
        """用力眨眼的 strength 必须显著高于普通眨眼——这是重眨识别的基础。"""
        det = BlinkDetector(fs=FS, abs_threshold=40.0, intent_peak=150.0)
        soft = det.feed(synth_eeg(4.0, [(2.0, 90, 0.2)]))
        det2 = BlinkDetector(fs=FS, abs_threshold=40.0, intent_peak=150.0)
        hard = det2.feed(synth_eeg(4.0, [(2.0, 300, 0.3)]))
        assert soft and hard
        assert hard[0].strength > soft[0].strength + 0.2

    def test_no_false_positive_on_clean_noise(self):
        """纯底噪不应该产生眨眼。"""
        det = BlinkDetector(fs=FS)
        sig = synth_eeg(20.0, [])
        found = []
        for i in range(0, len(sig), 32):
            found += det.feed(sig[i:i + 32])
        assert len(found) == 0, f"底噪误报了 {len(found)} 次"

    def test_refractory_blocks_immediate_retrigger(self):
        det = BlinkDetector(fs=FS, refractory_ms=300)
        # 两次眨眼间隔仅 100 ms，第二次应被不应期挡掉
        sig = synth_eeg(5.0, [(2.0, 220, 0.15), (2.25, 220, 0.15)])
        found = []
        for i in range(0, len(sig), 32):
            found += det.feed(sig[i:i + 32])
        assert len(found) == 1

    def test_absolute_threshold_overrides_adaptive(self):
        det = BlinkDetector(fs=FS, abs_threshold=1e9)
        found = []
        sig = synth_eeg(5.0, [(2.0, 400, 0.2)])
        for i in range(0, len(sig), 32):
            found += det.feed(sig[i:i + 32])
        assert found == []


# ---------------------------------------------------------------- 眼势


class FakeBus:
    def __init__(self):
        self.events: list[dict] = []

    def publish(self, type: str, **payload):
        self.events.append({"type": type, **payload})

    def gestures(self) -> list[str]:
        return [e["gesture"] for e in self.events if e["type"] == "eye.gesture"]


class FakeCfg:
    def __init__(self, **over):
        self._d = {
            "eyes.enabled": True,
            "eyes.refractory_ms": 500,
            "eyes.quality_gate": 50,
            "eyes.calibration": {"hard_threshold": 0.55, "double_gap_ms": [120, 450],
                                 "long_ms": 400},
            **over,
        }

    def get(self, k, default=None):
        return self._d.get(k, default)

    def section(self, k):
        v = self._d.get(k, {})
        return v if isinstance(v, dict) else {}


class TestGestureRecognizer:
    def _feed(self, rec, times: list[float], strength: float = 0.3,
              duration: float = 150):
        """在没有事件循环的环境下，每次 on_blink 会立即判定，
        所以这里手动模拟"攒够一组再判定"的时序。"""
        for t in times:
            rec.buf.append((t, strength))
        rec._decide()

    def test_single_normal_blink_ignored(self):
        """自然眨眼每分钟 15-20 次，单次普通眨眼绝不能触发动作。"""
        bus = FakeBus()
        rec = GestureRecognizer(FakeCfg(), bus)
        self._feed(rec, [1.0], strength=0.2)
        assert bus.gestures() == []

    def test_double_blink(self):
        bus = FakeBus()
        rec = GestureRecognizer(FakeCfg(), bus)
        self._feed(rec, [1.0, 1.25])
        assert bus.gestures() == ["double_blink"]

    def test_double_blink_too_fast_rejected(self):
        bus = FakeBus()
        rec = GestureRecognizer(FakeCfg(), bus)
        self._feed(rec, [1.0, 1.05])      # 50 ms < 120 ms 下限
        assert bus.gestures() == []

    def test_triple_blink(self):
        bus = FakeBus()
        rec = GestureRecognizer(FakeCfg(), bus)
        self._feed(rec, [1.0, 1.2, 1.4])
        assert bus.gestures() == ["triple_blink"]

    def test_hard_blink(self):
        bus = FakeBus()
        rec = GestureRecognizer(FakeCfg(), bus)
        self._feed(rec, [1.0], strength=0.9)
        assert bus.gestures() == ["hard_blink"]

    def test_long_blink_immediate(self):
        bus = FakeBus()
        rec = GestureRecognizer(FakeCfg(), bus)
        rec.on_blink({"t": 1.0, "strength": 0.4, "duration_ms": 500})
        assert bus.gestures() == ["long_blink"]

    def test_refractory_blocks_second_gesture(self):
        bus = FakeBus()
        rec = GestureRecognizer(FakeCfg(), bus)
        self._feed(rec, [1.0, 1.25])
        self._feed(rec, [1.3, 1.55])       # 距上次仅 300 ms < 500 ms 不应期
        assert bus.gestures() == ["double_blink"]

    def test_poor_signal_gates_everything(self):
        bus = FakeBus()
        rec = GestureRecognizer(FakeCfg(), bus)
        rec.on_quality({"poor_signal": 200})
        rec.on_blink({"t": 1.0, "strength": 0.9, "duration_ms": 150})
        rec.on_blink({"t": 1.25, "strength": 0.9, "duration_ms": 150})
        assert bus.gestures() == []

    def test_mirror_always_published(self):
        """即使信号差或功能关闭，老鼠也要跟着眨眼——这是"它在读我的眼睛"的证明。"""
        bus = FakeBus()
        rec = GestureRecognizer(FakeCfg(**{"eyes.enabled": False}), bus)
        rec.on_blink({"t": 1.0, "strength": 0.3, "duration_ms": 150})
        anims = [e.get("anim") for e in bus.events if e["type"] == "mouse.state"]
        assert "blink_mirror" in anims
        assert bus.gestures() == []


# ---------------------------------------------------------------- 入侵判定


class TestIntruderDetector:
    def _det(self, **over):
        from supermouse.modes.strong_sense.presence import IntruderDetector
        cfg = FakeCfg(**{
            "sense.enabled": True, "sense.zone_m": [0.8, 4.0], "sense.min_frames": 3,
            "sense.approach_speed_mps": 0.3, "sense.cooldown_s": 20, "sense.clear_s": 10,
            "sense.intruder_target_count": 1, "sense.energy_gate": 30, "devices.radar.facing": "backward", **over,
        })
        bus = FakeBus()
        return IntruderDetector(cfg, bus), bus

    def _frame(self, t, cm, energy=70, state="moving"):
        return {"t": t, "state": state, "moving_cm": cm, "moving_energy": energy,
                "static_cm": 0, "static_energy": 0}

    def test_triggers_after_min_frames(self):
        det, bus = self._det()
        for i in range(3):
            det.on_frame(self._frame(1.0 + i * 0.1, 200))
        assert any(e["type"] == "sense.intruder" for e in bus.events)

    def test_no_trigger_on_single_frame(self):
        det, bus = self._det()
        det.on_frame(self._frame(1.0, 200))
        assert not any(e["type"] == "sense.intruder" for e in bus.events)

    def test_low_energy_ignored(self):
        det, bus = self._det()
        for i in range(6):
            det.on_frame(self._frame(1.0 + i * 0.1, 200, energy=10))
        assert not any(e["type"] == "sense.intruder" for e in bus.events)

    def test_out_of_zone_ignored(self):
        det, bus = self._det()
        for i in range(6):
            det.on_frame(self._frame(1.0 + i * 0.1, 600))   # 6 m > 4 m 上限
        assert not any(e["type"] == "sense.intruder" for e in bus.events)

    def test_clear_after_absence(self):
        det, bus = self._det()
        for i in range(3):
            det.on_frame(self._frame(1.0 + i * 0.1, 200))
        for i in range(105):
            det.on_frame(self._frame(1.3 + i * 0.1, 0, energy=0, state="none"))
        assert any(e["type"] == "sense.clear" for e in bus.events)

    def test_cooldown_prevents_retrigger(self):
        det, bus = self._det()
        for i in range(3):
            det.on_frame(self._frame(1.0 + i * 0.1, 200))
        for i in range(3):
            det.on_frame(self._frame(2.0 + i * 0.1, 200))
        assert sum(1 for e in bus.events if e["type"] == "sense.intruder") == 1

    def test_forward_facing_ignores_user(self):
        """雷达朝向用户时，近处的用户自己不算入侵。"""
        det, bus = self._det(**{"devices.radar.facing": "forward",
                                "sense.user_dist_m": 1.2})
        for i in range(6):
            det.on_frame(self._frame(1.0 + i * 0.1, 70))   # 0.7 m = 用户自己
        assert not any(e["type"] == "sense.intruder" for e in bus.events)

    def test_ld2450_multi_target(self):
        det, bus = self._det()
        for i in range(3):
            det.on_frame({"t": 1.0 + i * 0.1, "state": "moving",
                          "targets": [{"x_mm": 100, "y_mm": 2000, "speed_cms": -30,
                                       "dist_mm": 2002}]})
        assert any(e["type"] == "sense.intruder" for e in bus.events)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
