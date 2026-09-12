"""LD2454 第二人判定、桥接重连与安全打开串口的回归。"""
from collections import deque
from types import SimpleNamespace

import pytest

from supermouse.core.bus import EventBus
from supermouse.core.config import Config
from supermouse.devices.radar import serial_client as client
from supermouse.devices.radar.parsers import AutoParser, LD2454Parser, PARSERS
from supermouse.modes.strong_sense.presence import IntruderDetector
from test_radar import ld2450_frame
from tools import radar_probe


def setup(**overrides):
    cfg = Config({"sense": {"intruder_target_count": 2, "clear_s": .3, **overrides}})
    bus = EventBus()
    events = []
    bus.subscribe("sense.*", events.append)
    detector = IntruderDetector(cfg, bus)
    detector.install()
    return detector, bus, events


def emit(bus, t, targets=((0, 500, 0), (0, 2000, -40))):
    for f in LD2454Parser().feed(ld2450_frame(targets)):
        bus.publish("radar.frame", **f, t=t)


def test_official_ld2454_example_bytewise():
    data = bytes.fromhex("AA FF 03 00 0E 03 B1 86 10 00 40 01") + bytes(16) + bytes.fromhex("55 CC")
    p = LD2454Parser()
    frames = [f for b in data for f in p.feed(bytes([b]))]
    assert len(frames) == 1
    assert frames[0]["model"] == "ld2454"
    t = frames[0]["targets"][0]
    assert (t["x_mm"], t["y_mm"], t["speed_cms"]) == (-782, 1713, -16)


def test_out_and_diagnostic_text_never_become_people():
    assert "out" not in PARSERS
    p = AutoParser()
    assert p.feed(b"P 1 100\n# moving dis=200\n#INFO multi=1 bridging=1\n") == []
    assert p.locked is None


def test_user_alone_never_triggers_even_when_moving_and_nearby():
    _, bus, events = setup()
    for i in range(20):
        emit(bus, 1 + i * .1, ((0, 1500-i*50, -50),))
    assert events == []


def test_two_targets_count_near_user_and_preserve_event_contract():
    det, bus, events = setup()
    emit(bus, 1)
    emit(bus, 1.1)
    assert events == []
    emit(bus, 1.2)
    assert len(events) == 1 and det.active
    assert events[0]["distance_m"] == 2
    assert events[0]["approaching"] is True
    assert set(events[0]) == {"type", "t", "distance_m", "approaching"}


def test_static_targets_count_without_energy_gate():
    _, bus, events = setup(energy_gate=999)
    for i in range(3):
        emit(bus, 1+i*.1, ((0, 500, 0), (0, 2000, 0)))
    assert events[0]["approaching"] is False


@pytest.mark.parametrize("targets", [(), ((0, 500, 0),), ((0, 500, 0), (0, 5000, -40))])
def test_single_dropout_frame_keeps_evidence(targets):
    """走动目标会被雷达间歇性丢失——单帧闪断不能清空已累计的证据（实测踩过）。"""
    _, bus, events = setup()
    emit(bus, 1); emit(bus, 1.1)
    emit(bus, 1.2, targets)          # 闪断一帧
    emit(bus, 1.3)
    assert events and events[0]["type"] == "sense.intruder"


def test_sustained_absence_expires_evidence_beyond_window():
    """缺席超过 window_s 后证据过期，重新出现需要重新凑满 min_frames。"""
    _, bus, events = setup(window_s=0.5)
    emit(bus, 1); emit(bus, 1.1)          # 2 帧证据
    emit(bus, 1.2, ((0, 500, 0),))        # 只剩本人
    emit(bus, 2.0, ((0, 500, 0),))        # 缺席 0.8s > 0.5s → 证据过期
    emit(bus, 2.1, ((0, 500, 0),))
    emit(bus, 2.2)                        # 第二人重新出现，仅 1 帧
    assert events == []                    # 旧证据已过期，1 帧不够
    emit(bus, 2.3); emit(bus, 2.4)
    assert [e["type"] for e in events] == ["sense.intruder"]


def test_approaching_cannot_bypass_min_frames():
    _, bus, events = setup(min_frames=5)
    for i in range(4):
        emit(bus, 1+i*.1, ((0, 500, 0), (0, 3000-i*400, -100)))
    assert events == []
    emit(bus, 1.4)
    assert len(events) == 1


def test_count_one_representative_is_nearest_target():
    """阈值 1（雷达只覆盖过道）时代表就是最近目标——距离下限已移除。"""
    _, bus, events = setup(intruder_target_count=1)
    for i in range(3): emit(bus, 1+i*.1)
    assert events[0]["distance_m"] == 0.5


def test_close_second_person_beside_user_triggers():
    """实测回归：本人 0.5m，来人走到旁边站定 0.65m —— 不得被距离下限挡掉。"""
    _, bus, events = setup()
    for i in range(4):
        emit(bus, 1+i*.1, ((0, 500, 0), (100, 650, 0)))
    assert [e["type"] for e in events] == ["sense.intruder"]
    assert events[0]["distance_m"] == 0.66   # √(100²+650²)=657.6mm


def test_walking_person_flicker_still_triggers():
    """实测回归：走动目标被间歇性丢失（2→1→2 交替），滑窗内仍应触发。"""
    _, bus, events = setup()
    seq = [((0, 500, 0), (0, 2000, -40)), ((0, 500, 0),),
           ((0, 500, 0), (0, 1800, -40)), ((0, 500, 0),),
           ((0, 500, 0), (0, 1600, -40))]
    for i, tg in enumerate(seq):
        emit(bus, 1+i*.1, tg)
    assert any(e["type"] == "sense.intruder" for e in events)


def test_person_leaves_only_user_remains_clears_once():
    det, bus, events = setup()
    for i in range(3): emit(bus, 1+i*.1)
    for i in range(10): emit(bus, 1.3+i*.1, ((0, 500, 0),))
    assert not det.active
    assert [e["type"] for e in events] == ["sense.intruder", "sense.clear"]
    assert set(events[-1]) == {"type", "t"}


def test_presence_does_not_retrigger_after_cooldown_until_clear():
    _, bus, events = setup(cooldown_s=.3)
    for i in range(50): emit(bus, 1+i*.1)
    assert len(events) == 1


def test_cooldown_still_applies_after_clear():
    _, bus, events = setup(cooldown_s=2)
    for i in range(3): emit(bus, 1+i*.1)
    for i in range(6): emit(bus, 1.3+i*.1, ((0, 500, 0),))
    for i in range(10): emit(bus, 1.9+i*.1)
    assert len(events) == 2
    for i in range(6): emit(bus, 2.9+i*.1)
    assert [e["type"] for e in events] == ["sense.intruder", "sense.clear", "sense.intruder"]


def test_frame_gap_and_disconnect_do_not_reuse_evidence_or_clear_on_silence():
    det, bus, events = setup()
    emit(bus, 1); emit(bus, 1.1); emit(bus, 3)
    assert len(det.recent) == 1 and events == []
    bus.publish("radar.disconnected")
    emit(bus, 3.1); emit(bus, 3.2)
    assert events == []
    emit(bus, 3.3)
    bus.publish("radar.disconnected")
    emit(bus, 20, ())
    assert det.active and len(events) == 1


def test_slot_reordering_does_not_invent_approach_velocity():
    _, bus, events = setup()
    emit(bus, 1, ((0, 500, 0), (0, 3000, 0)))
    emit(bus, 1.1, ((0, 2500, 0), (0, 500, 0)))
    emit(bus, 1.2, ((0, 500, 0), (0, 2000, 0)))
    assert events[0]["approaching"] is False


def test_old_single_target_protocol_cannot_claim_two_people():
    _, bus, events = setup()
    for i in range(10):
        bus.publish("radar.frame", t=1+i*.1, state="both", moving_cm=200,
                    moving_energy=100, static_cm=50, static_energy=100)
    assert events == []


@pytest.mark.parametrize("count", [0, 4, -1])
def test_invalid_count_rejected(count):
    with pytest.raises(ValueError): setup(intruder_target_count=count)


def test_three_target_threshold():
    _, bus, events = setup(intruder_target_count=3)
    for i in range(4): emit(bus, 1+i*.1)
    assert events == []
    for i in range(3): emit(bus, 1.4+i*.1, ((0,500,0), (0,2000,0), (0,3000,0)))
    assert events[0]["distance_m"] == 3


class FakeSerial:
    def __init__(self, data=b"", on_empty=None):
        self.pending = bytearray(data)
        self.on_empty = on_empty
        self.writes = []
    def read(self, n):
        out = bytes(self.pending[:n]); del self.pending[:n]
        if not out and self.on_empty: self.on_empty()
        return out
    def write(self, data): self.writes.append(data)
    def flush(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass


def test_dtr_and_rts_set_before_open(monkeypatch):
    class Serial:
        def open(self):
            assert self.dtr is False and self.rts is False
            assert self.port == "/dev/test" and self.baudrate == 115200
    monkeypatch.setattr(client.serial, "Serial", Serial)
    client.open_serial("/dev/test", 115200)


def test_bridge_missing_marker_and_reconnect_reset_parser(monkeypatch):
    bus = EventBus(); events = []
    bus.subscribe("radar.frame", events.append)
    c = client.RadarSerialClient(Config({"devices":{"radar":{"model":"auto", "bridge":True}}}), bus)
    c.parser.feed(b"mov, dis=123\n")  # 上次误锁文本，重连必须重建
    fake = FakeSerial(ld2450_frame([(0, 500, 0), (0, 2000, -40)]), c._stop.set)
    monkeypatch.setattr(client, "open_serial", lambda *args: fake)
    c._session("/dev/test")
    assert len(events) == 1 and len(events[0]["targets"]) == 2
    assert fake.writes == [b"!info\n"]


@pytest.mark.parametrize("chunk_size", [1, 7, 30, 2048])
def test_bridge_diagnostics_never_drop_frames(chunk_size):
    bus = EventBus(); events = []
    bus.subscribe("radar.frame", events.append)
    c = client.RadarSerialClient(Config({"devices":{"radar":{"model":"ld2454", "connection":"arduino"}}}), bus)
    frame = ld2450_frame([(0, 2000, -40)])
    data = b"#SCAN start\n#BRIDGE rx=17 baud=256000 multi=1\n"+frame+b"#INFO bridging=1 multi=1\n"+frame
    for i in range(0, len(data), chunk_size):
        c._handle(c._consume_diag(data[i:i+chunk_size]))
    assert len(events) == 2 and c._in_bridge


def test_probe_recognizes_running_bridge_and_preserves_first_frame():
    ok, info = radar_probe.read_until_bridge(FakeSerial(b"#INFO rx=17 baud=256000 multi=1 bridging=1\n"))
    assert ok and info["multi"] == "1"
    frame = ld2450_frame([(0, 500, 0), (0, 2000, 0)])
    ok, info = radar_probe.read_until_bridge(FakeSerial(frame))
    assert ok and info["prefetched"] == frame


def test_probe_rejects_brain_device_by_usb_id(monkeypatch):
    ports = [SimpleNamespace(vid=0x1A86, device="/dev/cu.usbmodem999", serial_number="2017"),
             SimpleNamespace(vid=0x303A, device="/dev/cu.usbmodem1301", serial_number="123")]
    monkeypatch.setattr(radar_probe.list_ports, "comports", lambda: ports)
    assert radar_probe.find_bridge("arduino") == "/dev/cu.usbmodem1301"
    ports.pop()
    assert radar_probe.find_bridge() is None
    assert client.find_port() is None
