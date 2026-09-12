"""ThinkGear 协议解析器（NeuroSky TGAM 系）。

实测数据（2026-09-12，USB 接收器 /dev/tty.usbmodem2017_2_251）：
    aa aa 04 80 02 ff c3 bb
    ^^^^^ 同步头  ^^ 长度  ^^ CODE=0x80 (RAW)  ^^ 长度=2  ^^^^^ 大端有符号  ^^ 校验
- 采样率 ≈ 250 Hz（不是手册常写的 512）
- 每秒一次 0x02/0x04/0x05/0x83（信号质量 / 专注度 / 放松度 / 频段能量）
- 该固件不发 0x16 BLINK_STRENGTH，眨眼必须从原始波自己检测

包格式：0xAA 0xAA | PLENGTH | PAYLOAD | CHKSUM
    CHKSUM = (~sum(PAYLOAD)) & 0xFF，PLENGTH ≤ 169
PAYLOAD 是若干行：[EXCODE...] [CODE] [LEN(仅 CODE≥0x80)] [VALUE]
"""

from __future__ import annotations

from typing import Iterator

SYNC = b"\xaa\xaa"
MAX_PLENGTH = 169

CODE_POOR_SIGNAL = 0x02
CODE_ATTENTION = 0x04
CODE_MEDITATION = 0x05
CODE_BLINK = 0x16
CODE_RAW = 0x80
CODE_EEG_POWER = 0x83
EXCODE = 0x55

BAND_NAMES = ("delta", "theta", "low_alpha", "high_alpha",
              "low_beta", "high_beta", "low_gamma", "mid_gamma")


class ThinkGearParser:
    """字节流解析器。BLE/串口会把包切成任意长度，所以必须缓冲。"""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.packets = 0
        self.bad_checksum = 0

    def feed(self, data: bytes) -> Iterator[tuple[str, object]]:
        """喂入任意长度字节，产出 (name, value)。

        name ∈ poor_signal | attention | meditation | blink | raw | eeg_power
        """
        self.buf += data
        while True:
            i = self.buf.find(SYNC)
            if i < 0:
                # 没有同步头；留最后一个字节，它可能是下一个 0xAA
                if len(self.buf) > 1:
                    del self.buf[:-1]
                return
            if len(self.buf) < i + 3:
                del self.buf[:i]
                return

            plen = self.buf[i + 2]
            if plen > MAX_PLENGTH:
                # 假同步头（比如 payload 里刚好出现 aa aa），跳过一个字节重找
                del self.buf[: i + 1]
                continue

            end = i + 3 + plen + 1
            if len(self.buf) < end:
                del self.buf[:i]
                return

            payload = bytes(self.buf[i + 3 : i + 3 + plen])
            chk = self.buf[end - 1]
            del self.buf[:end]

            if ((~sum(payload)) & 0xFF) != chk:
                self.bad_checksum += 1
                continue

            self.packets += 1
            yield from self._rows(payload)

    def _rows(self, p: bytes) -> Iterator[tuple[str, object]]:
        j = 0
        n = len(p)
        while j < n:
            code = p[j]
            j += 1
            while code == EXCODE and j < n:      # 扩展码前缀，本项目用不到
                code = p[j]
                j += 1
            if code >= 0x80:
                if j >= n:
                    return
                length = p[j]
                j += 1
            else:
                length = 1
            if j + length > n:
                return
            v = p[j : j + length]
            j += length

            if code == CODE_POOR_SIGNAL:
                yield ("poor_signal", v[0])
            elif code == CODE_ATTENTION:
                yield ("attention", v[0])
            elif code == CODE_MEDITATION:
                yield ("meditation", v[0])
            elif code == CODE_BLINK:
                yield ("blink", v[0])
            elif code == CODE_RAW and length == 2:
                yield ("raw", int.from_bytes(v, "big", signed=True))
            elif code == CODE_EEG_POWER and length == 24:
                bands = {BAND_NAMES[k]: int.from_bytes(v[k * 3 : k * 3 + 3], "big")
                         for k in range(8)}
                yield ("eeg_power", bands)
