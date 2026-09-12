"""毫米波雷达协议解析。

设计成可插拔：每个型号一个解析类，都实现 `feed(bytes) -> list[dict]`，
返回的 dict 就是 `radar.frame` 事件的 payload（字段见 docs/02 §5.2）。

已实现（协议有据可查）：
    LD2410Parser   F4F3F2F1 … F8F7F6F5，单目标 + 能量
    LD2450Parser   AAFF0300 … 55CC，最多 3 个目标的 x/y/速度
    BridgeTextParser  立创 S3 / MicroPython 的 #序号 T1/T2/T3 文本
    TextParser     LD1115/LD1125 这类输出 "mov, dis=123" 文本的型号

LD2454 已按官方《串口通信协议 V1.00》示例帧核对，与 LD2450 使用同一格式。
显式 model=ld2454 时输出正确型号；自动识别只能判断协议族，不能区分这两个型号。
LD2454 没有 OUT 引脚，不接受电平文本伪造的距离。

AutoParser 会同时尝试所有已知解析器，谁先出帧就认谁——接线时不用先知道型号。
"""

from __future__ import annotations

import logging
import math
import re

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- LD2410

LD2410_HEAD = b"\xf4\xf3\xf2\xf1"
LD2410_TAIL = b"\xf8\xf7\xf6\xf5"
LD2410_ACK_HEAD = b"\xfd\xfc\xfb\xfa"
LD2410_ACK_TAIL = b"\x04\x03\x02\x01"

_STATE = {0x00: "none", 0x01: "moving", 0x02: "static", 0x03: "both"}


class LD2410Parser:
    """LD2410：单目标，给出移动/静止距离与能量。

    帧结构：F4F3F2F1 | len(2, LE) | data | F8F7F6F5
    data（基本模式，type=0x02）：
        [0] 0x02 数据类型   [1] 0xAA 帧内头
        [2] 目标状态        [3:5] 移动距离 cm(LE)   [5] 移动能量
        [6:8] 静止距离 cm   [8] 静止能量            [9:11] 探测距离 cm
        [11] 0x55 帧内尾    [12] 校验
    工程模式（type=0x01）在后面附加各距离门能量，这里只取基本字段。
    """

    name = "ld2410"

    def __init__(self) -> None:
        self.buf = bytearray()
        self.frames = 0
        self.bad = 0

    def feed(self, data: bytes) -> list[dict]:
        self.buf += data
        out: list[dict] = []
        while True:
            i = self.buf.find(LD2410_HEAD)
            if i < 0:
                # 只保留可能是半个帧头的尾巴
                if len(self.buf) > 3:
                    del self.buf[:-3]
                return out
            if i:
                del self.buf[:i]
            if len(self.buf) < 6:
                return out
            n = int.from_bytes(self.buf[4:6], "little")
            if n > 512:                       # 长度不合理，说明这个帧头是假的
                del self.buf[:1]
                self.bad += 1
                continue
            end = 6 + n + 4
            if len(self.buf) < end:
                return out
            if bytes(self.buf[6 + n:end]) != LD2410_TAIL:
                del self.buf[:1]
                self.bad += 1
                continue
            frame = self._decode(bytes(self.buf[6:6 + n]))
            del self.buf[:end]
            if frame:
                self.frames += 1
                out.append(frame)

    def _decode(self, d: bytes) -> dict | None:
        if len(d) < 13 or d[1] != 0xAA:
            return None
        state = _STATE.get(d[2])
        if state is None:
            return None
        return {
            "state": state,
            "moving_cm": int.from_bytes(d[3:5], "little"),
            "moving_energy": d[5],
            "static_cm": int.from_bytes(d[6:8], "little"),
            "static_energy": d[8],
            "detect_cm": int.from_bytes(d[9:11], "little"),
            "model": self.name,
        }


# ---------------------------------------------------------------- LD2450

LD2450_HEAD = b"\xaa\xff\x03\x00"
LD2450_TAIL = b"\x55\xcc"
LD2450_LEN = 4 + 8 * 3 + 2        # 头 + 3 个目标 + 尾 = 30


class LD2450Parser:
    """LD2450：最多 3 个目标的 x/y 坐标与速度。

    帧结构：AAFF0300 | 目标×3（各 8 字节）| 55CC
    每个目标：x(2) y(2) speed(2) resolution(2)，全部小端。
    符号约定特殊：**最高位为 1 表示正数**，为 0 表示负数，
    数值取低 15 位。全零表示该目标槽位为空。
    """

    name = "ld2450"

    def __init__(self) -> None:
        self.buf = bytearray()
        self.frames = 0
        self.bad = 0

    def feed(self, data: bytes) -> list[dict]:
        self.buf += data
        out: list[dict] = []
        while True:
            i = self.buf.find(LD2450_HEAD)
            if i < 0:
                if len(self.buf) > 3:
                    del self.buf[:-3]
                return out
            if i:
                del self.buf[:i]
            if len(self.buf) < LD2450_LEN:
                return out
            if bytes(self.buf[LD2450_LEN - 2:LD2450_LEN]) != LD2450_TAIL:
                del self.buf[:1]
                self.bad += 1
                continue
            body = bytes(self.buf[4:LD2450_LEN - 2])
            del self.buf[:LD2450_LEN]
            self.frames += 1
            out.append(self._decode(body))

    @staticmethod
    def _signed(raw: int) -> int:
        """LD2450 的符号位约定：最高位 1 = 正，0 = 负。"""
        v = raw & 0x7FFF
        return v if raw & 0x8000 else -v

    def _decode(self, body: bytes) -> dict:
        targets = []
        for k in range(3):
            chunk = body[k * 8:(k + 1) * 8]
            if len(chunk) < 8 or not any(chunk):
                continue                       # 空槽位
            x = self._signed(int.from_bytes(chunk[0:2], "little"))
            y = self._signed(int.from_bytes(chunk[2:4], "little"))
            sp = self._signed(int.from_bytes(chunk[4:6], "little"))
            targets.append({
                "x_mm": x,
                "y_mm": y,
                "speed_cms": sp,
                "dist_mm": int((x * x + y * y) ** 0.5),
            })

        return target_payload(targets, self.name)


def target_payload(targets: list[dict], model: str) -> dict:
    """文本桥/二进制桥使用相同的事件口径；能量为兼容字段，非实测值。"""
    nearest = min((t["dist_mm"] for t in targets), default=0)
    moving = [t for t in targets if t["speed_cms"] != 0]
    return {
        "state": ("moving" if moving else "static") if targets else "none",
        "targets": targets,
        "moving_cm": min((t["dist_mm"] for t in moving), default=0) // 10,
        "moving_energy": 100 if moving else 0,
        "static_cm": nearest // 10 if targets and not moving else 0,
        "static_energy": 100 if targets and not moving else 0,
        "detect_cm": nearest // 10,
        "model": model,
    }


# ---------------------------------------------------------------- 文本协议

_TEXT_RE = re.compile(rb"(mov|occ|som|static|moving)\b[,:\s]*(?:dis\s*=\s*(\d+))?",
                      re.IGNORECASE)


class TextParser:
    """LD1115H / LD1125H 这类输出文本的型号，例如 "mov, dis=123"。

    dis 的单位各型号不一（cm 或自定义刻度），先按 cm 处理，
    等真实数据到手再校正。
    """

    name = "text"

    def __init__(self) -> None:
        self.buf = bytearray()
        self.frames = 0
        self.bad = 0

    def feed(self, data: bytes) -> list[dict]:
        self.buf += data
        out: list[dict] = []
        while b"\n" in self.buf:
            line, _, rest = bytes(self.buf).partition(b"\n")
            self.buf = bytearray(rest)
            if line.lstrip().startswith(b"#"):
                continue
            m = _TEXT_RE.fullmatch(line.strip())
            if not m:
                continue
            kind = m.group(1).lower()
            dist = int(m.group(2)) if m.group(2) else 0
            moving = kind in (b"mov", b"moving")
            self.frames += 1
            out.append({
                "state": "moving" if moving else "static",
                "moving_cm": dist if moving else 0,
                "moving_energy": 100 if moving else 0,
                "static_cm": 0 if moving else dist,
                "static_energy": 0 if moving else 100,
                "detect_cm": dist,
                "model": self.name,
                "raw_text": line.decode("utf-8", "replace").strip(),
            })
        if len(self.buf) > 512:               # 没有换行的垃圾，别让缓冲无限膨胀
            del self.buf[:-64]
        return out


class LD2454Parser(LD2450Parser):
    """LD2454 V1.00：复用已验证的 30 字节目标协议。"""

    name = "ld2454"


# 固件源文件：firmware/ld2454_micropython/main.py::report（不修改板上固件）。
_BRIDGE_LINE_RE = re.compile(rb"^#[0-9]+\s+(.+)$")
_BRIDGE_SLOT_RE = re.compile(rb"T([123]):\s*(.+)")
_BRIDGE_TARGET_RE = re.compile(
    (r"x=(-?\d+)mm\s+y=(-?\d+)mm\s+v=(-?\d+)cm/s\s+"
     r"距离=-?\d+\.\d+m\s+角=-?\d+\.\d+°\s+分辨率=(\d+)mm").encode("utf-8")
)


class BridgeTextParser:
    """立创 ESP32S3R8N8 / MicroPython 的 #序号 T1/T2/T3 UTF-8 文本。

    按完整三槽行校验；损坏/截断行丢弃，不能把解析失败伪装成“无人”。
    x/y/v 已在板上解码，距离由坐标重算，避免文本两位小数造成精度损失。
    中文 UTF-8 可能跨任意串口块，只在收到换行后解析。
    """

    name = "bridge"
    MAX_LINE = 1024

    def __init__(self) -> None:
        self.buf = bytearray()
        self.frames = 0
        self.bad = 0
        self._dropping_line = False

    def feed(self, data: bytes) -> list[dict]:
        self.buf += data
        out: list[dict] = []
        while b"\n" in self.buf:
            line, _, rest = bytes(self.buf).partition(b"\n")
            self.buf = bytearray(rest)
            if self._dropping_line:
                self._dropping_line = False
                continue
            if len(line) > self.MAX_LINE:
                self.bad += 1
                continue
            match = _BRIDGE_LINE_RE.fullmatch(line.strip())
            if not match:
                continue  # RAW:/[配置]/启动横幅/REPL 等不是目标帧
            frame = self._decode(match.group(1))
            if frame is None:
                self.bad += 1
                continue
            self.frames += 1
            out.append(frame)
        if len(self.buf) > self.MAX_LINE:
            if not self._dropping_line:
                self.bad += 1
            self.buf.clear()
            self._dropping_line = True  # 超长行的后半截不能被当成一个新帧
        return out

    def _decode(self, body: bytes) -> dict | None:
        slots = body.split(b"|")
        if len(slots) != 3:
            return None
        targets = []
        for number, slot in enumerate(slots, 1):
            match = _BRIDGE_SLOT_RE.fullmatch(slot.strip())
            if not match or int(match.group(1)) != number:
                return None
            value = match.group(2)
            if value == "无".encode("utf-8"):
                continue
            match = _BRIDGE_TARGET_RE.fullmatch(value)
            if not match:
                return None
            x, y, speed, resolution = map(int, match.groups())
            if any(abs(v) > 32767 for v in (x, y, speed)) or resolution > 65535:
                return None
            targets.append({"x_mm": x, "y_mm": y, "speed_cms": speed,
                            "dist_mm": int(math.hypot(x, y))})
        return target_payload(targets, self.name)


PARSERS = {
    "ld2410": LD2410Parser,
    "ld2450": LD2450Parser,
    "ld2454": LD2454Parser,
    "bridge": BridgeTextParser,
    "text": TextParser,
}


class AutoParser:
    """同时喂给所有已知解析器，谁先出帧就锁定谁。

    接线阶段很有用：不需要事先知道型号，插上就能出事件。
    锁定后只走那一个解析器（省 CPU，也避免误判）。
    """

    name = "auto"

    def __init__(self) -> None:
        self.cands = [cls() for cls in PARSERS.values()]
        self.locked = None

    def feed(self, data: bytes) -> list[dict]:
        if self.locked is not None:
            return self.locked.feed(data)
        for p in self.cands:
            try:
                out = p.feed(data)
            except Exception:
                log.exception("解析器 %s 出错", p.name)
                continue
            if out:
                self.locked = p
                self.cands = []
                log.info("雷达协议已识别：%s", p.name)
                return out
        return []


def make_parser(model: str):
    if model in ("auto", "", None):
        return AutoParser()
    cls = PARSERS.get(model)
    if cls is None:
        log.warning("未知雷达型号 %s，改用自动识别", model)
        return AutoParser()
    return cls()
