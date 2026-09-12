"""雷达解析器单元测试。

用手册记载的帧格式合成帧来验证，不需要硬件。

LD2454 V1.00 的示例帧、目标个数判定和串口分包均在此验证；真机验收另行进行。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supermouse.devices.radar.parsers import (
    AutoParser,
    LD2410Parser,
    LD2450Parser,
    LD2454Parser,
    TextParser,
    make_parser,
)


# ---------------------------------------------------------------- 构帧辅助


def ld2410_frame(state: int = 0x01, moving_cm: int = 200, moving_e: int = 70,
                 static_cm: int = 0, static_e: int = 0, detect_cm: int = 200) -> bytes:
    """按手册构造 LD2410 基本模式数据帧。

    F4F3F2F1 | len(2,LE) | 02 AA state mov(2) movE sta(2) staE det(2) 55 chk | F8F7F6F5
    """
    data = bytes([0x02, 0xAA, state])
    data += moving_cm.to_bytes(2, "little") + bytes([moving_e])
    data += static_cm.to_bytes(2, "little") + bytes([static_e])
    data += detect_cm.to_bytes(2, "little")
    data += bytes([0x55, 0x00])
    return (b"\xf4\xf3\xf2\xf1" + len(data).to_bytes(2, "little")
            + data + b"\xf8\xf7\xf6\xf5")


def ld2450_target(x: int, y: int, speed: int) -> bytes:
    """LD2450 单目标 8 字节。符号约定：最高位 1 = 正，0 = 负。"""
    def enc(v: int) -> bytes:
        raw = (abs(v) & 0x7FFF) | (0x8000 if v >= 0 else 0)
        return raw.to_bytes(2, "little")
    return enc(x) + enc(y) + enc(speed) + (0).to_bytes(2, "little")


def ld2450_frame(targets: list[tuple[int, int, int]]) -> bytes:
    body = b""
    for i in range(3):
        body += ld2450_target(*targets[i]) if i < len(targets) else bytes(8)
    return b"\xaa\xff\x03\x00" + body + b"\x55\xcc"


# ---------------------------------------------------------------- LD2410


class TestLD2410Parser:
    def test_single_frame(self):
        p = LD2410Parser()
        out = p.feed(ld2410_frame(moving_cm=250, moving_e=80, detect_cm=250))
        assert len(out) == 1
        f = out[0]
        assert f["state"] == "moving"
        assert f["moving_cm"] == 250
        assert f["moving_energy"] == 80
        assert f["detect_cm"] == 250
        assert f["model"] == "ld2410"

    def test_all_states(self):
        for code, name in ((0x00, "none"), (0x01, "moving"),
                           (0x02, "static"), (0x03, "both")):
            out = LD2410Parser().feed(ld2410_frame(state=code))
            assert out and out[0]["state"] == name

    def test_static_target(self):
        out = LD2410Parser().feed(
            ld2410_frame(state=0x02, moving_cm=0, moving_e=0,
                         static_cm=180, static_e=90, detect_cm=180))
        assert out[0]["static_cm"] == 180
        assert out[0]["static_energy"] == 90

    def test_byte_by_byte(self):
        """串口会把帧切成任意长度，逐字节喂也必须解析出来。"""
        p = LD2410Parser()
        stream = ld2410_frame(moving_cm=100) + ld2410_frame(moving_cm=300)
        got = []
        for b in stream:
            got += p.feed(bytes([b]))
        assert [f["moving_cm"] for f in got] == [100, 300]

    def test_garbage_prefix_skipped(self):
        p = LD2410Parser()
        out = p.feed(b"\x11\x22\x33" + ld2410_frame(moving_cm=150))
        assert len(out) == 1 and out[0]["moving_cm"] == 150

    def test_bad_tail_rejected(self):
        p = LD2410Parser()
        bad = bytearray(ld2410_frame())
        bad[-1] ^= 0xFF                       # 破坏帧尾
        out = p.feed(bytes(bad) + ld2410_frame(moving_cm=222))
        assert len(out) == 1 and out[0]["moving_cm"] == 222
        assert p.bad >= 1

    def test_absurd_length_rejected(self):
        """长度字段不合理时应跳过这个假帧头，继续找真帧。"""
        p = LD2410Parser()
        fake = b"\xf4\xf3\xf2\xf1" + (9999).to_bytes(2, "little")
        out = p.feed(fake + ld2410_frame(moving_cm=77))
        assert len(out) == 1 and out[0]["moving_cm"] == 77

    def test_partial_frame_waits(self):
        p = LD2410Parser()
        frame = ld2410_frame()
        assert p.feed(frame[:8]) == []        # 不完整，先不出帧
        assert len(p.feed(frame[8:])) == 1

    def test_buffer_bounded_on_garbage(self):
        """长时间收到无帧头的垃圾时缓冲不能无限膨胀。"""
        p = LD2410Parser()
        for _ in range(200):
            p.feed(b"\x11" * 100)
        assert len(p.buf) <= 3

    def test_ack_frame_ignored(self):
        """命令应答帧（FDFCFBFA）不是数据帧，不应产出事件。"""
        p = LD2410Parser()
        ack = b"\xfd\xfc\xfb\xfa\x04\x00\x01\x02\x03\x04\x04\x03\x02\x01"
        assert p.feed(ack) == []


# ---------------------------------------------------------------- LD2450


class TestLD2450Parser:
    def test_single_target(self):
        out = LD2450Parser().feed(ld2450_frame([(100, 2000, -30)]))
        assert len(out) == 1
        t = out[0]["targets"]
        assert len(t) == 1
        assert t[0]["x_mm"] == 100
        assert t[0]["y_mm"] == 2000
        assert t[0]["speed_cms"] == -30
        assert 2000 <= t[0]["dist_mm"] <= 2010     # sqrt(100²+2000²) ≈ 2002

    def test_negative_coordinates(self):
        """符号位约定特殊：最高位 1 = 正，0 = 负。"""
        out = LD2450Parser().feed(ld2450_frame([(-500, 1500, 25)]))
        t = out[0]["targets"][0]
        assert t["x_mm"] == -500
        assert t["y_mm"] == 1500
        assert t["speed_cms"] == 25

    def test_three_targets(self):
        out = LD2450Parser().feed(
            ld2450_frame([(0, 1000, 10), (500, 2000, -5), (-800, 3000, 0)]))
        assert len(out[0]["targets"]) == 3

    def test_empty_slots_skipped(self):
        """全零槽位表示该目标不存在。"""
        out = LD2450Parser().feed(ld2450_frame([(0, 1200, 15)]))
        assert len(out[0]["targets"]) == 1

    def test_no_target_state_none(self):
        out = LD2450Parser().feed(ld2450_frame([]))
        assert out[0]["state"] == "none"
        assert out[0]["targets"] == []

    def test_single_target_fields_filled(self):
        """要同时填单目标字段，presence.py 两种型号才能共用。"""
        out = LD2450Parser().feed(ld2450_frame([(0, 2000, -40)]))
        f = out[0]
        assert f["state"] == "moving"
        assert f["moving_cm"] == 200          # 2000 mm → 200 cm
        assert f["detect_cm"] == 200

    def test_static_when_speed_zero(self):
        out = LD2450Parser().feed(ld2450_frame([(0, 1500, 0)]))
        assert out[0]["state"] == "static"
        assert out[0]["static_cm"] == 150

    def test_byte_by_byte(self):
        p = LD2450Parser()
        stream = ld2450_frame([(0, 1000, 5)]) + ld2450_frame([(0, 2000, 5)])
        got = []
        for b in stream:
            got += p.feed(bytes([b]))
        assert [f["detect_cm"] for f in got] == [100, 200]

    def test_bad_tail_rejected(self):
        p = LD2450Parser()
        bad = bytearray(ld2450_frame([(0, 1000, 5)]))
        bad[-1] ^= 0xFF
        out = p.feed(bytes(bad) + ld2450_frame([(0, 2500, 5)]))
        assert len(out) == 1 and out[0]["detect_cm"] == 250

    def test_garbage_prefix(self):
        out = LD2450Parser().feed(b"\x00\x01\x02" + ld2450_frame([(0, 900, 5)]))
        assert len(out) == 1 and out[0]["detect_cm"] == 90


# ---------------------------------------------------------------- 文本协议


class TestTextParser:
    def test_moving_with_distance(self):
        out = TextParser().feed(b"mov, dis=123\n")
        assert len(out) == 1
        assert out[0]["state"] == "moving"
        assert out[0]["detect_cm"] == 123

    def test_static_occupancy(self):
        out = TextParser().feed(b"occ, dis=250\n")
        assert out[0]["state"] == "static"
        assert out[0]["static_cm"] == 250

    def test_multiple_lines(self):
        out = TextParser().feed(b"mov, dis=100\nmov, dis=200\n")
        assert [f["detect_cm"] for f in out] == [100, 200]

    def test_partial_line_waits(self):
        p = TextParser()
        assert p.feed(b"mov, dis=1") == []
        assert len(p.feed(b"50\n")) == 1

    def test_unrecognized_line_ignored(self):
        assert TextParser().feed(b"hello world\n") == []

    def test_buffer_bounded_without_newline(self):
        p = TextParser()
        p.feed(b"x" * 2000)
        assert len(p.buf) <= 512


# ---------------------------------------------------------------- 自动识别


class TestAutoParser:
    def test_locks_onto_ld2410(self):
        p = AutoParser()
        out = p.feed(ld2410_frame(moving_cm=200))
        assert len(out) == 1
        assert p.locked is not None and p.locked.name == "ld2410"

    def test_locks_onto_ld2450(self):
        p = AutoParser()
        out = p.feed(ld2450_frame([(0, 2000, -10)]))
        assert len(out) == 1
        assert p.locked.name == "ld2450"

    def test_locks_onto_text(self):
        p = AutoParser()
        out = p.feed(b"mov, dis=88\n")
        assert len(out) == 1
        assert p.locked.name == "text"

    def test_stays_unlocked_on_noise(self):
        p = AutoParser()
        assert p.feed(b"\x00\x11\x22\x33" * 20) == []
        assert p.locked is None

    def test_continues_after_lock(self):
        p = AutoParser()
        p.feed(ld2450_frame([(0, 1000, 5)]))
        out = p.feed(ld2450_frame([(0, 3000, 5)]))
        assert len(out) == 1 and out[0]["detect_cm"] == 300

    def test_split_across_chunks(self):
        """帧被切开时也要能锁定——串口读取本来就是任意切分的。"""
        p = AutoParser()
        frame = ld2410_frame(moving_cm=180)
        assert p.feed(frame[:6]) == []
        out = p.feed(frame[6:])
        assert len(out) == 1 and out[0]["moving_cm"] == 180


class TestMakeParser:
    def test_known_models(self):
        assert make_parser("ld2410").name == "ld2410"
        assert make_parser("ld2450").name == "ld2450"
        assert make_parser("ld2454").name == "ld2454"
        assert make_parser("text").name == "text"

    def test_auto_and_unknown_fall_back(self):
        assert isinstance(make_parser("auto"), AutoParser)
        assert isinstance(make_parser(""), AutoParser)
        # LD2404 目前没有专用解析器 → 应回退到自动识别而不是崩掉
        assert isinstance(make_parser("ld2404"), AutoParser)


# ---------------------------------------------------------------- 与入侵判定联动


class TestRadarToIntruder:
    """雷达帧 → sense.intruder 的端到端链路（不需要硬件）。"""

    def _setup(self):
        from supermouse.modes.strong_sense.presence import IntruderDetector

        class Bus:
            def __init__(s): s.events = []
            def publish(s, type, **kw): s.events.append({"type": type, **kw})
            def subscribe(s, pat, fn): return lambda: None

        class Cfg:
            def __init__(s):
                s._d = {"sense.enabled": True, "sense.zone_m": [0.8, 4.0],
                        "sense.min_frames": 3, "sense.approach_speed_mps": 0.3,
                        "sense.cooldown_s": 20, "sense.clear_s": 10,
                        "sense.energy_gate": 30, "sense.intruder_target_count": 1,
                        "devices.radar.facing": "backward"}
            def get(s, k, dv=None): return s._d.get(k, dv)
            def section(s, k):
                v = s._d.get(k, {})
                return v if isinstance(v, dict) else {}

        bus = Bus()
        return IntruderDetector(Cfg(), bus), bus

    def test_ld2410_frames_trigger_intruder(self):
        det, bus = self._setup()
        p = LD2410Parser()
        for i in range(4):
            for f in p.feed(ld2410_frame(moving_cm=200, moving_e=70)):
                f["t"] = 1.0 + i * 0.1
                det.on_frame(f)
        assert any(e["type"] == "sense.intruder" for e in bus.events)

    def test_ld2450_frames_trigger_intruder(self):
        det, bus = self._setup()
        p = LD2450Parser()
        for i in range(4):
            for f in p.feed(ld2450_frame([(100, 2000, -30)])):
                f["t"] = 1.0 + i * 0.1
                det.on_frame(f)
        assert any(e["type"] == "sense.intruder" for e in bus.events)

    def test_empty_frames_do_not_trigger(self):
        det, bus = self._setup()
        p = LD2450Parser()
        for i in range(10):
            for f in p.feed(ld2450_frame([])):
                f["t"] = 1.0 + i * 0.1
                det.on_frame(f)
        assert not any(e["type"] == "sense.intruder" for e in bus.events)




# ---------------------------------------------------------------- 立创 S3 / MicroPython 文本桥

BRIDGE_EXAMPLE = (
    "#000001 T1: x=-782mm y=1713mm v=-16cm/s 距离=1.88m 角=-24.5° 分辨率=320mm"
    " | T2: 无 | T3: 无\r\n"
).encode("utf-8")


def bridge_line(targets, number=1, raw_hex=False):
    """直接执行仓库中板上 report()，但不导入/运行 main.py 的硬件初始化。"""
    import ast
    import math
    source = Path(__file__).resolve().parents[1] / "firmware/ld2454_micropython/main.py"
    tree = ast.parse(source.read_text())
    report = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "report")
    lines = []
    namespace = {"RAW_HEX": raw_hex, "log": lines.append}
    exec(compile(ast.Module(body=[report], type_ignores=[]), str(source), "exec"), namespace)
    slots = []
    for t in targets:
        if t is None:
            slots.append(None)
        else:
            x, y, v = t
            slots.append((x, y, v, 320, math.hypot(x, y) / 1000, math.degrees(math.atan2(x, y))))
    slots += [None] * (3-len(slots))
    namespace["report"](number, b"\xaa\xff\x03\x00" + bytes(24) + b"\x55\xcc", slots)
    return ("\n".join(lines) + "\n").encode("utf-8")


class TestBridgeTextParser:
    def test_actual_chinese_format(self):
        from supermouse.devices.radar.parsers import BridgeTextParser
        p = BridgeTextParser()
        f, = p.feed(BRIDGE_EXAMPLE)
        assert f["model"] == "bridge"
        assert f["targets"] == [{"x_mm": -782, "y_mm": 1713, "speed_cms": -16, "dist_mm": 1883}]
        assert f["moving_cm"] == 188 and f["moving_energy"] == 100
        assert f["static_cm"] == f["static_energy"] == 0
        assert p.frames == 1 and p.bad == 0

    @pytest.mark.parametrize("targets", [[], [(0, 1500, 0)], [(100, -2000, 20)],
                                        [(0, 500, 0), (400, 2000, -40)],
                                        [(0, 500, 0), (0, 2000, -40), (-400, 3000, 10)]])
    def test_same_payload_as_binary(self, targets):
        from supermouse.devices.radar.parsers import BridgeTextParser
        text, = BridgeTextParser().feed(bridge_line(targets))
        binary, = LD2454Parser().feed(ld2450_frame(targets))
        assert text.pop("model") == "bridge"
        binary.pop("model")
        assert text == binary

    def test_missing_slot_does_not_shift_other_people(self):
        from supermouse.devices.radar.parsers import BridgeTextParser
        f, = BridgeTextParser().feed(bridge_line([None, (10, 2000, 30), None]))
        assert len(f["targets"]) == 1 and f["targets"][0]["x_mm"] == 10

    @pytest.mark.parametrize("size", [1, 7, 37, 4096])
    def test_arbitrary_chunks_and_utf8(self, size):
        from supermouse.devices.radar.parsers import BridgeTextParser
        stream = ("MicroPython v1.29.0\r\n[配置] 多目标追踪: 成功\n".encode()
                  + BRIDGE_EXAMPLE + bridge_line([], raw_hex=True))
        p = BridgeTextParser()
        frames = [f for i in range(0, len(stream), size) for f in p.feed(stream[i:i+size])]
        assert [len(f["targets"]) for f in frames] == [1, 0]
        assert p.bad == 0

    def test_every_possible_split(self):
        from supermouse.devices.radar.parsers import BridgeTextParser
        for i in range(len(BRIDGE_EXAMPLE)):
            p = BridgeTextParser()
            assert p.feed(BRIDGE_EXAMPLE[:i]) == []
            assert len(p.feed(BRIDGE_EXAMPLE[i:])) == 1

    def test_nearest_static_person_not_used_as_moving_distance(self):
        from supermouse.devices.radar.parsers import BridgeTextParser
        f, = BridgeTextParser().feed(bridge_line([(0, 500, 0), (0, 2000, -40)]))
        assert f["detect_cm"] == 50 and f["moving_cm"] == 200

    @pytest.mark.parametrize("bad", [
        b"#123 corrupted\n", b"#1 T1: ??? | T2: ??? | T3: ???\n",
        "#1 T1: 无 | T2: 无\n".encode(),
        "#1 T1: 无 | T1: 无 | T3: 无\n".encode(),
        "#1 T1: 无 | T2: 无 | T4: 无\n".encode(),
        BRIDGE_EXAMPLE.replace(b"v=-16cm/s", b"v=garbagecm/s"),
        BRIDGE_EXAMPLE.replace(b"x=-782mm", b"x=-99999mm"),
        BRIDGE_EXAMPLE.replace("分辨率=320mm".encode(), b""),
        BRIDGE_EXAMPLE.replace("无".encode(), b"\xff"),
    ])
    def test_corrupt_lines_do_not_claim_no_people(self, bad):
        from supermouse.devices.radar.parsers import BridgeTextParser
        p = BridgeTextParser()
        assert p.feed(bad) == []
        assert p.bad == 1
        assert len(p.feed(BRIDGE_EXAMPLE)) == 1

    def test_bounded_buffer_drops_entire_overlong_line(self):
        from supermouse.devices.radar.parsers import BridgeTextParser
        p = BridgeTextParser()
        p.feed(b"x" * 100000)
        assert len(p.buf) <= p.MAX_LINE
        assert p.feed(BRIDGE_EXAMPLE) == []  # 同一损坏行的后半段
        assert len(p.feed(BRIDGE_EXAMPLE)) == 1

    def test_auto_skips_diagnostics_raw_and_locks_bridge(self):
        p = AutoParser()
        assert p.feed(b"#123 unknown\nRAW: AA FF 03 00 55 CC\n") == []
        assert p.locked is None
        assert len(p.feed(BRIDGE_EXAMPLE)) == 1
        assert p.locked.name == "bridge"
        assert make_parser("bridge").name == "bridge"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
