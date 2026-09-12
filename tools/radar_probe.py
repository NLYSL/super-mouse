#!/usr/bin/env python3
"""雷达抓包与协议识别。

默认只读立创 ESP32S3R8N8 MicroPython 的 CH340K 文本，不给板子发命令。

    python tools/radar_probe.py --list                    列端口/USB身份，不打开设备
    python tools/radar_probe.py --seconds 10              使用项目配置，抓包并统计目标数
    python tools/radar_probe.py --port COM6 --seconds 10  Windows 数据口示例
    python tools/radar_probe.py --analyze FILE            离线分析中文文本或二进制帧
    python tools/radar_probe.py --connection arduino --scan  仅旧 Arduino 桥可用
"""

from __future__ import annotations

import argparse
import math
import signal
from contextlib import contextmanager
import sys
import time
from collections import Counter
from pathlib import Path

import serial
from serial.tools import list_ports

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from supermouse.core.config import load_config
from supermouse.devices.radar.parsers import LD2454Parser, make_parser
from supermouse.devices.radar.serial_client import (
    CONNECTIONS, connection_type, find_port, missing_port_hint, open_serial,
)

BRIDGE_BAUD = 115200          # 固件与主机之间的 USB CDC 速率（与雷达波特率无关）

# 已知帧头，用来快速判断协议族
KNOWN = {
    "LD2410 数据帧 F4F3F2F1": bytes.fromhex("f4f3f2f1"),
    "LD24xx 命令应答 FDFCFBFA": bytes.fromhex("fdfcfbfa"),
    "LD2454/LD2450 目标帧 AAFF0300": bytes.fromhex("aaff0300"),
    "通用帧尾 55CC": bytes.fromhex("55cc"),
    "通用帧尾 F8F7F6F5": bytes.fromhex("f8f7f6f5"),
    "LD1115/1125 文本": b"mov",
}


def find_bridge(connection: str = "micropython") -> str | None:
    return find_port(connection=connection)


@contextmanager
def serial_deadline(seconds: float):
    """macOS/Linux 硬超时包含 open/read；正常退出和异常都会恢复 signal handler。"""
    if not hasattr(signal, "setitimer"):
        yield  # Windows 使用 pyserial 的有限读写 timeout
        return
    def timed_out(*_):
        raise TimeoutError("串口操作超过时限，已停止读取；请检查是否有其他程序占用端口")
    old_handler = signal.signal(signal.SIGALRM, timed_out)
    old_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *old_timer)


def read_until_bridge(ser: serial.Serial, timeout: float = 40.0) -> tuple[bool, dict]:
    """既接受启动标记、已运行的 #INFO，也接受无标记的有效目标帧。"""
    info: dict = {"candidates": [], "best": None}
    line = bytearray()
    recent = bytearray()
    parser = LD2454Parser()
    t0 = time.monotonic()
    last_data = t0
    failed = False
    while time.monotonic() - t0 < timeout:
        b = ser.read(1)
        if not b:
            # 保留扫描失败后的引脚诊断，且给排队的 !pins 命令执行时间。
            if failed and time.monotonic() - last_data > 2:
                return False, info
            continue
        last_data = time.monotonic()
        recent += b
        if len(recent) > 30:
            del recent[:-30]
        if parser.feed(b):
            info["prefetched"] = bytes(recent)  # 捕获到的第一帧不能丢掉
            return True, info
        if b in (b"\n", b"\r"):
            if not line:
                continue
            text = line.decode("utf-8", "replace").rstrip()
            line.clear()
            if not text.startswith("#"):
                continue
            print(f"  {text}")
            if text.startswith(("#BRIDGE", "#INFO")):
                for tok in text.split():
                    if "=" in tok:
                        k, v = tok.split("=", 1)
                        info[k] = v
                if text.startswith("#BRIDGE") or info.get("bridging") == "1":
                    return True, info
            if text.startswith("#SCAN start"):
                failed = False
            if "没有任何引脚收到数据" in text:
                failed = True
            if "字节" in text:
                info["candidates"].append(text)
        else:
            line += b
            if len(line) > 1000:
                line.clear()
    return False, info


def capture(ser: serial.Serial, seconds: float) -> bytes:
    """抓原始雷达字节流。"""
    buf = bytearray()
    t0 = time.monotonic()
    last = 0.0
    while time.monotonic() - t0 < seconds:
        d = ser.read(4096)
        if d:
            buf += d
        now = time.monotonic() - t0
        if now - last > 0.5:
            last = now
            print(f"\r  抓包 {now:4.1f}/{seconds:.0f}s  {len(buf)} 字节 "
                  f"({len(buf)/max(now,0.01):.0f} B/s)", end="", flush=True)
    print()
    return bytes(buf)


def guess_period(data: bytes, header: bytes) -> tuple[int | None, list[int]]:
    """找帧头出现的间隔，推断帧长。"""
    pos = []
    i = data.find(header)
    while i >= 0:
        pos.append(i)
        i = data.find(header, i + 1)
    gaps = [b - a for a, b in zip(pos, pos[1:])]
    if not gaps:
        return None, pos
    common = Counter(gaps).most_common(1)[0]
    # 只有当主间隔占多数时才认为是定长帧
    return (common[0] if common[1] >= len(gaps) * 0.6 else None), pos


def analyze(data: bytes, model: str = "auto") -> int:
    """验证 MicroPython 文本/二进制数据，返回完整有效帧数（不计算 RAW hex 行）。"""
    print(f"\n=== 分析 {len(data)} 字节 ===")
    if not data:
        print("没有接收到字节；不能据此判断雷达故障。检查数据端口、主机波特率及板上程序是否正在输出。")
        return 0

    frames = make_parser(model).feed(data)
    if frames:
        counts = Counter(len(f.get("targets", [])) for f in frames)
        print(f"有效目标帧: {len(frames)}；协议={frames[0]['model']}；目标数分布: {dict(sorted(counts.items()))}")
        for f in frames[:5]:
            print(f"  targets={f.get('targets', [])}")
        if max(counts) < 2:
            print("尚未观测到两个目标；需本人 + 第二人实测，不把单人数据当成多目标验收。")
        return len(frames)

    printable = sum(1 for b in data if 32 <= b < 127 or b in (10, 13)) / len(data)
    print(f"可打印字符比例 {printable*100:.0f}%")
    if printable > 0.75:
        print("收到文本，但未解析出有效帧；下列可能只是启动/配置诊断或截断数据")
        txt = data.decode("utf-8", "replace")
        for ln in [l for l in txt.splitlines() if l.strip()][:12]:
            print(f"    {ln.strip()!r}")
        return 0

    print(f"前 64 字节: {data[:64].hex(' ')}")

    found = {n: h for n, h in KNOWN.items() if h in data}
    if found:
        print("\n命中已知帧头:")
        for name, h in found.items():
            period, pos = guess_period(data, h)
            print(f"  {name}: 出现 {len(pos)} 次"
                  + (f"，帧长稳定 {period} 字节" if period else "，间隔不固定"))
    else:
        print("\n未命中已知帧头，尝试自动找周期性前缀…")

    # 自动找候选帧头：统计高频 4 字节序列，取间隔稳定的
    print("\n高频 4 字节序列（可能是帧头/帧尾）:")
    quads = Counter(bytes(data[i:i + 4]) for i in range(0, len(data) - 4))
    shown = 0
    for q, cnt in quads.most_common(40):
        if cnt < 3:
            break
        period, pos = guess_period(data, q)
        if period and 4 <= period <= 512:
            print(f"  {q.hex(' ')}  出现 {cnt:4d} 次  帧长 {period} 字节")
            shown += 1
            if shown >= 6:
                break
    if not shown:
        print("  没找到稳定周期。可能波特率不对（数据像噪声）或帧长可变。")
        bad = sum(1 for b in data if b in (0x00, 0xFF)) / len(data)
        if bad > 0.5:
            print(f"  {bad*100:.0f}% 的字节是 00/FF → **波特率很可能不匹配**，"
                  f"试 --baud 115200 / 9600 / 38400")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="雷达抓包（默认 MicroPython 只读，不修改固件）")
    ap.add_argument("--config", help="配置文件，默认 ~/.supermouse/config.yaml")
    ap.add_argument("--connection", choices=CONNECTIONS, help="连接方案；默认使用配置")
    ap.add_argument("--model", help="解析器；当前文本用 bridge，二进制用 ld2454，也可 auto")
    ap.add_argument("--port", help="CH340K 数据串口，可为 cu.wchusbserial* 或 COM6")
    ap.add_argument("--baud", type=int, help="主机侧波特率；当前板为 115200，不下发雷达改速命令")
    ap.add_argument("--seconds", type=float, default=6.0, help="抓包时长")
    ap.add_argument("--raw", type=float, metavar="SECONDS", help="跳过旧桥握手，直接抓取并保存原始数据")
    ap.add_argument("--analyze", metavar="FILE", help="离线分析，不打开任何串口")
    ap.add_argument("--list", action="store_true", help="仅列出串口与 USB 身份")
    commands = ap.add_mutually_exclusive_group()
    commands.add_argument("--scan", action="store_true", help="仅 Arduino 旧桥：扫描引脚")
    commands.add_argument("--multi", action="store_true", help="仅 Arduino 旧桥：配置多目标")
    commands.add_argument("--pins", nargs=2, type=int, metavar=("RX", "TX"), help="仅 Arduino 旧桥：指定引脚")
    commands.add_argument("--radar-baud", type=int, help="仅 Arduino 旧桥：改变桥接 UART 速率")
    args = ap.parse_args()

    if args.analyze:
        return 0 if analyze(Path(args.analyze).read_bytes(), args.model or "auto") else 1

    cfg = load_config(args.config)
    connection = args.connection or connection_type(cfg)
    model = args.model or (cfg.get("devices.radar.model", "bridge") if args.connection is None
                           else ("bridge" if connection == "micropython" else "ld2454"))
    has_command = args.scan or args.multi or args.pins is not None or args.radar_baud is not None
    if has_command and connection != "arduino":
        ap.error("MicroPython/直连模式禁止旧桥控制命令；新板只需 --seconds 10，不要用 --scan")
    if connection == "arduino" and model == "bridge":
        ap.error("bridge 是 MicroPython 文本解析器，不能使用 Arduino 控制协议")
    if args.list:
        for p in list_ports.comports():
            vid = f"{p.vid:04X}" if p.vid is not None else "????"
            pid = f"{p.pid:04X}" if p.pid is not None else "????"
            print(f"{p.device}  {vid}:{pid}  {p.description}  serial={p.serial_number or '-'}")
        chosen = find_bridge(connection)
        print(f"雷达候选: {chosen}" if chosen else missing_port_hint(connection))
        return 0

    seconds = args.raw if args.raw is not None else args.seconds
    if not math.isfinite(seconds) or not 0 < seconds <= 3600:
        ap.error("抓包时长必须为 0–3600 秒之间的有限正数")
    configured = args.port or cfg.get("devices.radar.port")
    port = find_bridge(connection) if configured in (None, "", "auto") else str(configured)
    if not port:
        print(missing_port_hint(connection))
        return 1
    default_baud = 256000 if connection == "direct" else BRIDGE_BAUD
    baud = args.baud or (cfg.get("devices.radar.baud", default_baud) if args.connection is None else default_baud)
    if connection == "arduino":
        baud = BRIDGE_BAUD
    print(f"数据口: {port} @ {baud}；连接={connection}；解析={model}")
    info = {}
    try:
        with serial_deadline(seconds + (45 if connection == "arduino" else 5)):
            with open_serial(port, int(baud)) as ser:
                if connection == "arduino":
                    cmd = ("!scan" if args.scan else "!multi" if args.multi else
                           f"!pins {args.pins[0]} {args.pins[1]}" if args.pins else
                           f"!baud {args.radar_baud}" if args.radar_baud else "!info")
                    print(f"旧 Arduino 桥命令: {cmd}")
                    ser.write((cmd + "\n").encode())
                    ser.flush()
                    if args.raw is None:
                        ok, info = read_until_bridge(ser)
                        if not ok:
                            print("旧桥未就绪，检查旧桥接线/固件；不要把此流程用于 MicroPython。")
                            return 1
                else:
                    print("只读采集：不发 !info/!scan/r，不复位或改写板上程序。")
                data = info.get("prefetched", b"") + capture(ser, seconds)
    except (serial.SerialException, OSError, TimeoutError) as e:
        print(f"串口采集失败: {e}")
        return 1

    out_dir = ROOT / "recordings"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"radar-raw-{time.strftime('%Y%m%d-%H%M%S')}.bin"
    path.write_bytes(data)
    print(f"已保存 → {path}")
    if connection == "micropython":
        for line in data.decode("utf-8", "replace").splitlines():
            if line.startswith(("[配置]", "[警告]")):
                print(line)
    frames = analyze(data, model)
    if frames:
        print(f"采样约 {seconds:g} 秒，平均 {frames / seconds:.1f} 帧/秒（启动/末尾半帧可能影响统计）")
    return 0 if frames else 1


if __name__ == "__main__":
    sys.exit(main())
