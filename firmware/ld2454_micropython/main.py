# HLK-LD2454 24GHz 多目标追踪雷达 -> USB 串口数据上报
# 硬件: 立创·ESP32S3R8N8 开发板
# 接线: 雷达 5V->VBUS(5V)  G->GND  T->G10(GPIO10, UART1 RX)  R->G11(GPIO11, UART1 TX)
# 上位机: CH340K 对应的串口(COM6), 115200 8N1
# 串口命令: r = 切换原始帧hex输出
#
# 协议依据《LD2454串口通信协议 V1.00》:
#   上报帧: AA FF 03 00 + 3个目标x8字节(x/y/速度/距离分辨率) + 55 CC, 约10帧/秒
#   字段编码: 最高位1=正值, 0=负值, 低15位为幅值
#   命令帧: FD FC FB FA + 长度 + 命令字 + 命令值 + 04 03 02 01

import math
import struct
import sys
import time
from machine import UART
import uselect

RADAR_BAUD = 256000
RADAR_TX_PIN = 11          # G11 -> 雷达 R
RADAR_RX_PIN = 10          # G10 <- 雷达 T
TRACK_MULTI_TARGET = True  # True=多目标追踪(最多3个), False=单目标追踪
RAW_HEX = False            # 是否打印原始帧

CMD_HEADER = bytes((0xFD, 0xFC, 0xFB, 0xFA))
CMD_FOOTER = bytes((0x04, 0x03, 0x02, 0x01))
DATA_HEADER = b"\xAA\xFF\x03\x00"
DATA_FOOTER = b"\x55\xCC"
FRAME_LEN = 30             # 4帧头 + 24数据 + 2帧尾

uart = UART(1, baudrate=RADAR_BAUD, tx=RADAR_TX_PIN, rx=RADAR_RX_PIN,
            bits=8, parity=None, stop=1, rxbuf=1024)
spoll = uselect.poll()
spoll.register(sys.stdin, uselect.POLLIN)


def build_cmd(cmd_word, value=b""):
    payload = struct.pack("<H", cmd_word) + value
    return CMD_HEADER + struct.pack("<H", len(payload)) + payload + CMD_FOOTER


def read_ack(timeout_ms=800):
    """在数据流中扫描命令ACK帧, 返回 (命令字, 状态, 附加数据)"""
    end = time.ticks_add(time.ticks_ms(), timeout_ms)
    buf = b""
    while time.ticks_diff(end, time.ticks_ms()) > 0:
        chunk = uart.read(256)
        if chunk:
            buf += chunk
        idx = buf.find(CMD_HEADER)
        if idx >= 0 and len(buf) - idx >= 6:
            length = struct.unpack_from("<H", buf, idx + 4)[0]
            total = 10 + length
            if len(buf) - idx >= total:
                frame = buf[idx:idx + total]
                if frame[-4:] == CMD_FOOTER:
                    data = frame[6:6 + length]
                    if len(data) >= 4:
                        cmd_field, status = struct.unpack_from("<HH", data)
                        return cmd_field, status, data[4:]
                buf = buf[idx + 4:]
    return None, None, None


def configure(cmd_word, value, name):
    uart.write(build_cmd(cmd_word, value))
    _, status, _ = read_ack()
    if status is None:
        log("[配置] %s: 无ACK(超时)" % name)
        return False
    log("[配置] %s: %s" % (name, "成功" if status == 0 else "失败(status=%d)" % status))
    return status == 0


def radar_setup():
    time.sleep(1.0)               # 等雷达上电稳定
    if uart.any():
        uart.read(uart.any())     # 丢弃上电残留字节
    configure(0x00FF, struct.pack("<H", 1), "使能配置")
    if TRACK_MULTI_TARGET:
        configure(0x0090, b"", "多目标追踪")
    else:
        configure(0x0080, b"", "单目标追踪")
    configure(0x00FE, b"", "结束配置")
    if uart.any():
        uart.read(uart.any())     # 丢弃配置期间收到的旧数据帧


def signed(raw):
    # 表9: 最高位1为正, 0为负, 低15位为幅值
    mag = raw & 0x7FFF
    return mag if raw & 0x8000 else -mag


def parse_targets(payload):
    targets = []
    for i in range(3):
        x, y, v, res = struct.unpack_from("<HHHH", payload, i * 8)
        if x == 0 and y == 0 and v == 0:
            targets.append(None)  # 协议示例: 不存在的目标全为0
        else:
            xx, yy, vv = signed(x), signed(y), signed(v)
            dist = math.sqrt(xx * xx + yy * yy) / 1000.0
            angle = math.degrees(math.atan2(xx, yy)) if yy or xx else 0.0
            targets.append((xx, yy, vv, res, dist, angle))
    return targets


def log(msg):
    print(msg)


def report(frame_no, frame, targets):
    if RAW_HEX:
        log("RAW: " + " ".join("%02X" % b for b in frame))
    parts = []
    for i, t in enumerate(targets):
        if t is None:
            parts.append("T%d: 无" % (i + 1))
        else:
            x, y, v, res, dist, ang = t
            parts.append("T%d: x=%dmm y=%dmm v=%dcm/s 距离=%.2fm 角=%.1f° 分辨率=%dmm"
                         % (i + 1, x, y, v, dist, ang, res))
    log("#%06d %s" % (frame_no, " | ".join(parts)))


def main_loop():
    buf = b""
    frame_no = 0
    last_frame_ms = time.ticks_ms()
    warned = False
    log("LD2454 雷达数据上报中... (r=切换原始hex输出)")
    while True:
        if spoll.poll(0):
            ch = sys.stdin.read(1)
            if ch == "r":
                # RAW_HEX 为模块级变量, 用 globals() 兼容 REPL 与 main.py 两种运行方式
                globals()["RAW_HEX"] = not globals()["RAW_HEX"]
                log("原始hex输出: %s" % ("开" if globals()["RAW_HEX"] else "关"))
        n = uart.any()
        chunk = uart.read(n) if n else None
        if chunk:
            buf += chunk
        while True:
            i = buf.find(DATA_HEADER)
            if i < 0:
                buf = buf[-3:]    # 保留尾部, 帧头可能被截断
                break
            if len(buf) - i < FRAME_LEN:
                buf = buf[i:]
                break
            frame = buf[i:i + FRAME_LEN]
            if frame[-2:] == DATA_FOOTER:
                frame_no += 1
                last_frame_ms = time.ticks_ms()
                warned = False
                report(frame_no, frame, parse_targets(frame[4:28]))
                buf = buf[i + FRAME_LEN:]
            else:
                buf = buf[i + 4:]  # 帧尾校验失败, 丢弃帧头重同步
        if not warned and time.ticks_diff(time.ticks_ms(), last_frame_ms) > 3000:
            warned = True
            log("[警告] 3秒未收到雷达数据, 请检查 T->D7 / R->D6 接线与雷达供电")


radar_setup()
main_loop()
