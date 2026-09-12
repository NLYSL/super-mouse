"""主机连接方案回归：不会对 MicroPython 发 Arduino 指令/抢脑机端口。"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from supermouse.core.bus import EventBus
from supermouse.core.config import Config
from supermouse.devices.bci.serial_client import find_port as find_bci
from supermouse.devices.radar import serial_client as client
from supermouse.modes.strong_sense.presence import IntruderDetector
from tools import radar_probe
from test_radar import BRIDGE_EXAMPLE, bridge_line
from test_radar_regressions import FakeSerial


def port(device, vid=None, pid=None, sn=None):
    return SimpleNamespace(device=device, vid=vid, pid=pid, serial_number=sn, description="test")


def config(**radar):
    return Config({"devices":{"radar":{"connection":"micropython", "model":"bridge", "baud":115200, **radar}}})


@pytest.mark.parametrize("windows", [False, True])
def test_ch340k_selected_not_brain_or_native_usb(monkeypatch, windows):
    ch = port("COM6" if windows else "/dev/cu.wchusbserial123", 0x1A86, 0x7522)
    brain = port("COM7" if windows else "/dev/cu.usbmodem2017_2_251", 0x1A86, 0x5722)
    native = port("COM5" if windows else "/dev/cu.usbmodem11401", 0x303A, 0x4001)
    monkeypatch.setattr(client.list_ports, "comports", lambda: [native, ch, brain])
    assert client.find_port() == ch.device
    assert radar_probe.find_bridge() == ch.device
    assert find_bci() == brain.device.replace("/dev/cu.", "/dev/tty.")


def test_no_driver_node_does_not_fall_back_to_other_interfaces(monkeypatch):
    ports = [port("/dev/cu.usbmodem11401",0x303A,0x4001),
             port("/dev/cu.usbmodem2017",0x1A86,0x5722), port("/dev/cu.debug-console")]
    monkeypatch.setattr(client.list_ports, "comports", lambda: ports)
    assert client.find_port() is None
    assert client.find_port(connection="arduino") is None
    assert "WCH" in client.missing_port_hint("micropython")
    ports.pop(1)
    assert find_bci() is None


def test_ch340k_alone_is_not_brain(monkeypatch):
    monkeypatch.setattr(client.list_ports, "comports", lambda: [port("/dev/cu.wchusbserial1",0x1A86,0x7522)])
    assert find_bci() is None


@pytest.mark.parametrize("p, expected", [
    (port("/dev/cu.wchusbserial123"), "/dev/cu.wchusbserial123"),
    (port("/dev/cu.wchusbserial2017",sn="2017-2-25"), None),
    (port("/dev/cu.wchusbserial123",0x1A86,0x5722), None),
    (port("/dev/cu.wchusbserial123",0x303A,0x4001), None),
    (port("/dev/cu.usbserial123",0x1A86,0x7523), None),
])
def test_missing_metadata_fallback_is_narrow(monkeypatch, p, expected):
    monkeypatch.setattr(client.list_ports, "comports", lambda: [p])
    assert client.find_port() == expected


def test_multiple_ch340k_ports_require_explicit_choice(monkeypatch):
    monkeypatch.setattr(client.list_ports, "comports", lambda: [port("COM6",0x1A86,0x7522), port("COM8",0x1A86,0x7522)])
    assert client.find_port() is None
    assert client.RadarSerialClient(config(port="COM8"), EventBus()).port == "COM8"


def test_micropython_session_is_read_only_and_publishes_frames(monkeypatch, caplog):
    bus = EventBus(); events = []
    bus.subscribe("radar.*", events.append)
    c = client.RadarSerialClient(config(), bus)
    data = "[配置] 多目标追踪: 成功\n".encode() + BRIDGE_EXAMPLE
    serial = FakeSerial(data, c._stop.set)
    opens = []
    monkeypatch.setattr(client, "open_serial", lambda *args: opens.append(args) or serial)
    with caplog.at_level(logging.INFO): c._session("COM6")
    assert opens == [("COM6",115200)]
    assert serial.writes == []  # 禁止 !info/!scan/r/CTRL-C 等任何写入
    assert [e["type"] for e in events] == ["radar.connected", "radar.frame", "radar.disconnected"]
    assert events[0]["baud"] == 115200
    assert "多目标追踪: 成功" in caplog.text
    assert "#000001" not in caplog.text


def test_bridge_model_remains_read_only_with_legacy_boolean(monkeypatch):
    cfg = Config({"devices":{"radar":{"model":"bridge", "bridge":True, "baud":115200}}})
    c = client.RadarSerialClient(cfg, EventBus())
    assert c.connection == "micropython"
    serial = FakeSerial(BRIDGE_EXAMPLE, c._stop.set)
    monkeypatch.setattr(client,"open_serial",lambda *args: serial)
    c._session("COM6")
    assert not serial.writes and c.frames == 1


def test_conflicting_protocol_and_connection_rejected():
    with pytest.raises(ValueError):
        client.RadarSerialClient(config(connection="arduino"), EventBus())


def test_binary_direct_connection_uses_configured_baud(monkeypatch):
    from test_radar import ld2450_frame
    c = client.RadarSerialClient(config(connection="direct",model="ld2454",baud=256000), EventBus())
    serial = FakeSerial(ld2450_frame([(0,2000,0)]), c._stop.set)
    opens=[]
    monkeypatch.setattr(client,"open_serial",lambda *args: opens.append(args) or serial)
    c._session("/dev/test")
    assert opens==[("/dev/test",256000)] and not serial.writes and c.frames==1


def test_read_thread_to_event_bus_and_second_person(monkeypatch):
    async def run():
        bus=EventBus(); bus.attach_loop(asyncio.get_running_loop())
        events=[]; bus.subscribe("sense.*",events.append)
        detector=IntruderDetector(config(),bus); detector.install()
        c=client.RadarSerialClient(config(port="COM6"),bus)
        data=bridge_line([(0,500,0)])*4
        data+=bridge_line([(0,500,0),(0,2000,-40)])*3
        serial=FakeSerial(data,c._stop.set)
        monkeypatch.setattr(client,"open_serial",lambda *args: serial)
        c.start()
        await asyncio.to_thread(c._thread.join, 2)
        await asyncio.sleep(0)
        c.stop()
        assert c.frames==7 and not serial.writes
        assert [e["type"] for e in events]==["sense.intruder"]
        assert events[0]["distance_m"]==2 and events[0]["approaching"] is True
    asyncio.run(run())


def test_probe_analyzes_real_format_not_raw_double_count(capsys):
    data=bridge_line([(0,500,0),(0,2000,-40)],raw_hex=True)+bridge_line([])
    assert radar_probe.analyze(data)==2
    output=capsys.readouterr().out
    assert "协议=bridge" in output and "0: 1" in output and "2: 1" in output


def test_probe_does_not_open_wrong_port_when_driver_missing(monkeypatch):
    monkeypatch.setattr(radar_probe,"load_config",lambda *a: config())
    monkeypatch.setattr(client.list_ports,"comports",lambda: [port("/dev/cu.usbmodem11401",0x303A,0x4001)])
    monkeypatch.setattr(radar_probe,"open_serial",lambda *a: pytest.fail("不应打开 REPL"))
    monkeypatch.setattr("sys.argv",["radar_probe.py","--seconds",".1"])
    assert radar_probe.main()==1


def test_probe_default_is_passive_capture(monkeypatch,tmp_path):
    serial=FakeSerial()
    monkeypatch.setattr(radar_probe,"load_config",lambda *a: config(port="COM6"))
    monkeypatch.setattr(radar_probe,"open_serial",lambda *a: serial)
    monkeypatch.setattr(radar_probe,"capture",lambda *a: BRIDGE_EXAMPLE)
    monkeypatch.setattr(radar_probe,"ROOT",tmp_path)
    monkeypatch.setattr("sys.argv",["radar_probe.py","--seconds",".1"])
    assert radar_probe.main()==0 and not serial.writes
    assert list((tmp_path/"recordings").glob("*.bin"))[0].read_bytes()==BRIDGE_EXAMPLE


@pytest.mark.parametrize("args", [["--scan"],["--multi"],["--pins","10","11"],["--radar-baud","256000"]])
def test_probe_refuses_arduino_commands_on_micropython(monkeypatch,args):
    monkeypatch.setattr(radar_probe,"load_config",lambda *a: config())
    monkeypatch.setattr(radar_probe,"open_serial",lambda *a: pytest.fail("禁止发送控制命令"))
    monkeypatch.setattr("sys.argv",["radar_probe.py",*args])
    with pytest.raises(SystemExit) as e: radar_probe.main()
    assert e.value.code==2


def test_missing_port_waits_then_hotplug_uses_ch340k(monkeypatch):
    c=client.RadarSerialClient(config(),EventBus())
    choices=iter([None,"COM6"])
    lookups=[]
    def choose(**kwargs):
        lookups.append(kwargs)
        return next(choices)
    monkeypatch.setattr(client,"find_port",choose)
    monkeypatch.setattr(c._stop,"wait",lambda timeout: None)
    serial=FakeSerial(BRIDGE_EXAMPLE,c._stop.set)
    opens=[]
    monkeypatch.setattr(client,"open_serial",lambda *args: opens.append(args) or serial)
    c._run()
    assert lookups==[{"connection":"micropython"}]*2
    assert opens==[("COM6",115200)] and c.frames==1 and not serial.writes


def test_reconnecting_text_session_discards_previous_partial_line(monkeypatch):
    c=client.RadarSerialClient(config(),EventBus())
    c.parser.feed(BRIDGE_EXAMPLE[:30])
    serial=FakeSerial(BRIDGE_EXAMPLE,c._stop.set)
    monkeypatch.setattr(client,"open_serial",lambda *args: serial)
    c._session("COM6")
    assert c.frames==1 and c.parser.bad==0


def test_default_config_selects_s3_text_bridge():
    import yaml
    from supermouse.core.config import DEFAULT_CONFIG
    cfg=Config(yaml.safe_load(DEFAULT_CONFIG.read_text()))
    c=client.RadarSerialClient(cfg,EventBus())
    assert (c.connection,c.model,c.baud)==("micropython","bridge",115200)
    assert cfg.get("sense.intruder_target_count")==2
