"""雷达串口客户端。

在独立线程里阻塞读串口，解析后通过 bus.publish 跨线程投递到事件循环
（和 devices/bci/serial_client.py 同一套模式）。

LD2454 → 立创 ESP32S3R8N8（MicroPython）→ CH340K 115200 文本。
主机只读；旧 Arduino 桥控制必须显式选择 connection=arduino。
USB 打开成功仅代表桥接板在线，收到 radar.frame 才证明雷达链路有数据。
桥接板可能早已启动，不能依赖只打印一次的 #BRIDGE 标记才能解析。
"""

from __future__ import annotations

import logging
import os
import threading
import time

import serial
from serial.tools import list_ports

from .parsers import make_parser
from ..serial_ports import ESPRESSIF_VID, is_bci_port, is_radar_text_port, usb_id

log = logging.getLogger(__name__)

DEFAULT_BAUD = 115200  # 当前 CH340K 主机侧速率，雷达到 UART1 仍是 256000
BRIDGE_MARKS = (b"#BRIDGE",)
CONNECTIONS = ("micropython", "arduino", "direct")


def connection_type(cfg) -> str:
    """新配置显式选连接方案；兼容旧 bridge 布尔值，但 model=bridge 始终只读。"""
    kind = cfg.get("devices.radar.connection")
    if kind is None:
        if cfg.get("devices.radar.model", "bridge") == "bridge":
            kind = "micropython"
        else:
            kind = "arduino" if cfg.get("devices.radar.bridge", True) else "direct"
    if kind not in CONNECTIONS:
        raise ValueError(f"devices.radar.connection 必须为 {CONNECTIONS}，实际 {kind!r}")
    if kind == "arduino" and cfg.get("devices.radar.model") == "bridge":
        raise ValueError("model=bridge 是 MicroPython 文本，不能选择 Arduino 控制协议")
    return kind


def find_port(prefer_bridge: bool = True, *, connection: str = "micropython") -> str | None:
    """按连接方案选口；默认只认 CH340K，不退回脑机/原生 USB REPL。

    多个同类候选时要求显式配置，避免随枚举顺序连接错误的设备。
    prefer_bridge 保留旧调用兼容性；新调用应传 connection。
    """
    if connection not in CONNECTIONS:
        raise ValueError(f"未知雷达连接方案 {connection!r}")
    candidates = []
    for p in list_ports.comports():
        if any(word in p.device for word in ("Bluetooth", "debug-console")) or is_bci_port(p):
            continue
        vid, pid = usb_id(p)
        if connection == "micropython":
            matches = is_radar_text_port(p)
        elif connection == "arduino":
            matches = vid == ESPRESSIF_VID and pid in (None, 0x1001)
        else:
            matches = vid in (0x1A86, 0x10C4, 0x0403)
        if matches:
            candidates.append(p.device)
    if len(candidates) > 1:
        log.warning("有多个雷达候选端口 %s；请设置 devices.radar.port", candidates)
        return None
    return candidates[0] if candidates else None


def missing_port_hint(connection: str) -> str:
    if connection == "micropython":
        return ("未找到雷达 CH340K 数据串口（USB 1A86:7522）；macOS 若 USB 可见却无 "
                "cu.wchusbserial 节点，请检查 WCH 驱动和系统扩展授权。"
                "不使用 303A:4001 原生 USB REPL、debug-console 或脑机端口。")
    return f"未找到雷达 {connection} 串口，请检查连接或显式配置 devices.radar.port。"


def open_serial(port: str, baud: int, timeout: float = 0.1) -> serial.Serial:
    """打开前设置 DTR/RTS，避免 USB CDC 默认握手干扰桥接板。"""
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = timeout
    ser.write_timeout = 1.0
    if os.name == "posix":
        ser.exclusive = True  # 避免本程序的多个串口工具互相抢读
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


class RadarSerialClient:
    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        # "auto" / "" / None 都表示自动查找。不能只用 `or None`——
        # 字符串 "auto" 是真值，会被原样送进 serial.Serial() 然后无限重连。
        configured = cfg.get("devices.radar.port")
        self.port = None if configured in (None, "", "auto") else str(configured)
        self.connection = connection_type(cfg)
        self.model = str(cfg.get("devices.radar.model", "bridge" if self.connection == "micropython" else "auto"))
        default_baud = 256000 if self.connection == "direct" else DEFAULT_BAUD
        self.baud = int(cfg.get("devices.radar.baud", default_baud))
        self.bridge = self.connection != "direct"  # 兼容 radar.connected 的现有字段
        self.parser = make_parser(self.model)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._in_bridge = self.connection != "arduino"  # 仅旧桥有 #BRIDGE 标记
        self._diag = bytearray()
        self.frames = 0
        self.connected = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="radar-serial", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)
            self._thread = None

    # ---------- 读取循环 ----------

    def _run(self) -> None:
        while not self._stop.is_set():
            port = self.port or find_port(connection=self.connection)
            if not port:
                log.warning("%s 5 s 后重试", missing_port_hint(self.connection))
                self._stop.wait(5.0)
                continue
            try:
                self._session(port)
            except serial.SerialException as e:
                log.warning("雷达串口断开：%s，2 s 后重连", e)
                self._publish_disconnected(str(e))
                self._stop.wait(2.0)
            except Exception:
                log.exception("雷达读取线程出错")
                self._publish_disconnected("internal error")
                self._stop.wait(2.0)

    def _session(self, port: str) -> None:
        # 桥接模式下 USB CDC 速率固定 115200（与雷达波特率无关，由固件负责）
        baud = 115200 if self.connection == "arduino" else self.baud
        log.info("连接雷达 %s @ %d%s", port, baud, "（桥接板）" if self.bridge else "")

        with open_serial(port, baud) as ser:
            # 不清空输入：首帧或开机诊断可能已在缓冲区里。
            self.parser = make_parser(self.model)  # 重连不能沿用上次半帧/自动锁定
            self.connected = True
            self.bus.publish("radar.connected", port=port, model=self.model,
                             baud=baud, bridge=self.bridge)
            self._in_bridge = self.connection != "arduino"
            self._diag.clear()

            if self.connection == "arduino":
                ser.write(b"!info\n")  # 重连时也能拿到配置状态；不重扫、不改雷达配置
                ser.flush()
            last_frame_at = time.monotonic()
            warned = False
            while not self._stop.is_set():
                data = ser.read(2048)
                if data:
                    if self.bridge:
                        self._consume_diag(data)
                    before = self.frames
                    self._handle(data)  # 无论是否看见 #BRIDGE，都允许解析器同步真实帧
                    if self.frames != before:
                        last_frame_at = time.monotonic()
                        warned = False
                if not warned and time.monotonic() - last_frame_at > 30:
                    log.warning("桥接串口已打开，但 30 秒未收到有效 radar.frame；"
                                "检查雷达供电/接线，串口在线不等于雷达正常")
                    warned = True
        self._publish_disconnected("stopped")

    def _consume_diag(self, data: bytes) -> bytes:
        """旁路记录诊断，不吞掉任何二进制数据（标记可缺失、分包或再次出现）。"""
        self._diag += data
        while b"\n" in self._diag:
            line, _, rest = bytes(self._diag).partition(b"\n")
            self._diag = bytearray(rest)
            if self.connection == "micropython":
                line = line.strip()
                if not line.startswith((b"#", b"LD2454", "[配置]".encode(), "[警告]".encode())):
                    continue
            else:
                idx = line.find(b"#")
                if idx < 0:
                    continue
                line = line[idx:].strip()
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if line.startswith(b"#") and len(line) > 1 and line[1:2].isdigit():
                continue  # MicroPython 目标行不是诊断，不以 INFO 刷屏
            if not text.isprintable():
                continue
            logger = log.warning if any(word in text for word in ("无ACK", "失败", "[警告]")) else log.info
            logger("桥接板: %s", text)
            if line.startswith(BRIDGE_MARKS) or (line.startswith(b"#INFO") and b"bridging=1" in line):
                self._in_bridge = True
            if b"multi=0" in line:
                log.warning("多目标模式未确认；仅有单目标时无法可靠检测第二人")
        if len(self._diag) > 4096:
            del self._diag[:-256]
        return data

    def _handle(self, data: bytes) -> None:
        try:
            frames = self.parser.feed(data)
        except Exception:
            log.exception("雷达解析出错")
            return
        for f in frames:
            self.frames += 1
            self.bus.publish("radar.frame", **f)

    def _publish_disconnected(self, reason: str) -> None:
        if self.connected:
            self.connected = False
            self.bus.publish("radar.disconnected", reason=reason)
