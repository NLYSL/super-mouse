"""毫米波雷达设备层。

    LD2454 → UART1 256000 → 立创 S3 MicroPython → CH340K 115200 文本 → 这里
    另兼容旧 Arduino 二进制桥与 USB-TTL 直连

对上层只暴露 `radar.frame` 事件，型号差异全部吃在 parsers.py 里。
"""

from __future__ import annotations

from .parsers import AutoParser, BridgeTextParser, LD2410Parser, LD2450Parser, LD2454Parser, TextParser, make_parser
from .serial_client import DEFAULT_BAUD, RadarSerialClient, find_port

__all__ = [
    "RadarSerialClient", "find_port", "DEFAULT_BAUD", "make_parser",
    "AutoParser", "BridgeTextParser", "LD2410Parser", "LD2450Parser", "LD2454Parser", "TextParser",
]
