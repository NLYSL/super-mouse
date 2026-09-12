#!/usr/bin/env python3
"""脑机实时监控：验证眨眼检测是否工作。

    python tools/bci_monitor.py                 实时显示信号与眨眼
    python tools/bci_monitor.py --record 20     录 20 秒原始数据到 recordings/
    python tools/bci_monitor.py --analyze FILE  离线分析录制的数据

实时模式下每检测到一次眨眼会打印一行，并显示信号条，
用来在没有 UI 的情况下确认"眨眼 → 事件"这条链路通了。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import serial

from supermouse.devices.bci.blink_detector import BlinkDetector
from supermouse.devices.bci.serial_client import find_port
from supermouse.devices.bci.thinkgear import ThinkGearParser

CHUNK = 32


def bar(value: float, width: int = 40, vmax: float = 300.0) -> str:
    n = int(min(abs(value) / vmax, 1.0) * width)
    return ("█" * n).ljust(width)


def live(port: str, baud: int, fs: int, seconds: float | None) -> int:
    parser = ThinkGearParser()
    det = BlinkDetector(fs=fs)
    buf: list[int] = []
    blinks = 0
    quality: int | None = None
    t0 = time.time()
    last_print = 0.0

    print(f"连接 {port} @ {baud}，fs={fs} Hz")
    print("请正常眨眼几次，然后用力眨几次。Ctrl-C 结束。\n")

    with serial.Serial(port, baud, timeout=0.05) as ser:
        time.sleep(0.2)
        ser.reset_input_buffer()
        while seconds is None or time.time() - t0 < seconds:
            data = ser.read(ser.in_waiting or 1)
            if not data:
                continue
            for name, value in parser.feed(data):
                if name == "raw":
                    buf.append(int(value))
                    if len(buf) >= CHUNK:
                        found = det.feed(buf)
                        level = max(abs(v) for v in det.filtered) if det.filtered else 0
                        buf = []
                        for b in found:
                            blinks += 1
                            kind = "用力眨眼" if b.strength > 0.55 else "眨眼"
                            print(f"\r{time.time()-t0:6.1f}s  ● {kind}  "
                                  f"强度={b.strength:.2f}  时长={b.duration_ms:5.1f}ms  "
                                  f"峰值={b.peak:6.1f}   (累计 {blinks})")
                        now = time.time()
                        if now - last_print > 0.08:
                            last_print = now
                            q = "?" if quality is None else str(quality)
                            print(f"\r  信号 {bar(level)} {level:6.1f}  "
                                  f"阈值={det.threshold():6.1f}  质量={q}  极性="
                                  f"{'+' if det.polarity >= 0 else '-'}", end="", flush=True)
                elif name == "poor_signal":
                    quality = int(value)
                elif name == "attention":
                    pass

    print(f"\n\n共检测到 {blinks} 次眨眼，"
          f"坏包 {parser.bad_checksum}/{parser.packets}")
    return 0


def record(port: str, baud: int, fs: int, seconds: float) -> int:
    parser = ThinkGearParser()
    out_dir = Path(__file__).resolve().parent.parent / "recordings"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"bci-raw-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"

    samples: list[int] = []
    meta: list[dict] = []
    t0 = time.time()
    print(f"录制 {seconds} 秒到 {path.name} …")
    print("提示：先自然眨眼 ~10 秒，再用力眨 5 次，最后快速双眨 3 次")

    with serial.Serial(port, baud, timeout=0.05) as ser:
        time.sleep(0.2)
        ser.reset_input_buffer()
        while time.time() - t0 < seconds:
            data = ser.read(ser.in_waiting or 1)
            if not data:
                continue
            for name, value in parser.feed(data):
                if name == "raw":
                    samples.append(int(value))
                else:
                    meta.append({"t": round(time.time() - t0, 3), name: value})
            done = time.time() - t0
            print(f"\r  {done:5.1f}/{seconds:.0f}s  {len(samples)} 个样本", end="", flush=True)

    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"fs": fs, "port": port, "samples": samples,
                            "meta": meta}, ensure_ascii=False) + "\n")
    print(f"\n已保存 {len(samples)} 个样本 ({len(samples)/seconds:.0f} Hz) → {path}")
    return 0


def analyze(path: str) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])
    samples = data["samples"]
    fs = data.get("fs", 250)
    a = np.array(samples, dtype=float)

    print(f"样本 {len(a)} 个，{len(a)/fs:.1f} 秒，实测 {len(a)/(len(a)/fs):.0f} Hz")
    print(f"原始幅度 min={a.min():.0f} max={a.max():.0f} std={a.std():.1f}")

    quals = [m["poor_signal"] for m in data.get("meta", []) if "poor_signal" in m]
    if quals:
        print(f"信号质量 poor_signal: min={min(quals)} max={max(quals)} "
              f"（0=良好，200=未接触）")

    det = BlinkDetector(fs=fs)
    found = []
    for i in range(0, len(a), CHUNK):
        for b in det.feed(a[i:i + CHUNK]):
            found.append((i / fs, b))

    print(f"\n检测到 {len(found)} 次眨眼，学到的极性 = "
          f"{'+' if det.polarity >= 0 else '-'}，阈值 = {det.threshold():.1f}")
    if not found:
        print("没检测到眨眼。可能原因：电极没贴好、佩戴时没眨眼、或阈值需要调整")
        return 1

    for t, b in found:
        print(f"  {t:6.2f}s  强度={b.strength:.2f}  时长={b.duration_ms:5.1f}ms  "
              f"峰值={b.peak:6.1f}")

    peaks = np.array([b.peak for _, b in found])
    gaps = np.diff([t for t, _ in found])
    print(f"\n峰值分布：中位数={np.median(peaks):.0f} "
          f"P25={np.percentile(peaks,25):.0f} P75={np.percentile(peaks,75):.0f} "
          f"最大={peaks.max():.0f}")
    if len(gaps):
        print(f"间隔分布：最短={gaps.min()*1000:.0f}ms 中位数={np.median(gaps)*1000:.0f}ms")
        doubles = (gaps < 0.5).sum()
        print(f"疑似双眨（间隔 < 500ms）：{doubles} 组")
    rate = len(found) / (len(a) / fs) * 60
    print(f"眨眼频率：{rate:.1f} 次/分钟（自然状态通常 15-20）")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="脑机眨眼检测验证工具")
    p.add_argument("--port", help="串口，默认自动查找")
    p.add_argument("--baud", type=int, default=57600)
    p.add_argument("--fs", type=int, default=250)
    p.add_argument("--record", type=float, metavar="SECONDS", help="录制原始数据")
    p.add_argument("--analyze", metavar="FILE", help="离线分析录制文件")
    p.add_argument("--seconds", type=float, help="实时模式运行时长")
    args = p.parse_args()

    if args.analyze:
        return analyze(args.analyze)

    port = args.port or find_port()
    if not port:
        print("没找到脑机串口。检查 USB 接收器是否插好（ls /dev/tty.*）")
        return 1

    try:
        if args.record:
            return record(port, args.baud, args.fs, args.record)
        return live(port, args.baud, args.fs, args.seconds)
    except KeyboardInterrupt:
        print("\n已停止")
        return 0
    except serial.SerialException as e:
        print(f"\n串口错误：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
