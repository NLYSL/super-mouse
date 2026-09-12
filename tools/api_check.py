#!/usr/bin/env python3
"""演示前自检：一条命令过一遍所有依赖。

    python tools/api_check.py            全部检查
    python tools/api_check.py --quick    跳过真实 API 调用（省时间、省额度）

对应 docs/05 的"演示前 10 分钟检查清单"。退出码 0 = 全部通过，
1 = 有硬伤（会让演示跑不起来），0 = 只有警告（可降级演示）。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = YELLOW = RED = DIM = RESET = ""

fails: list[str] = []
warns: list[str] = []


def ok(msg: str, detail: str = "") -> None:
    print(f"  {GREEN}✅{RESET} {msg}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def warn(msg: str, detail: str = "") -> None:
    warns.append(msg)
    print(f"  {YELLOW}⚠️{RESET}  {msg}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def bad(msg: str, detail: str = "") -> None:
    fails.append(msg)
    print(f"  {RED}❌{RESET} {msg}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def head(title: str) -> None:
    print(f"\n{title}")


# ---------------------------------------------------------------- 依赖

def check_deps() -> None:
    head("依赖库")
    required = {
        "PyQt6": "老鼠 Overlay",
        "numpy": "信号处理",
        "scipy": "带通滤波",
        "serial": "串口（pyserial）",
        "yaml": "配置（pyyaml）",
        "pydantic": "配置校验",
        "anthropic": "Claude API",
        "qasync": "Qt + asyncio",
        "pynput": "全局热键",
        "websockets": "调试面板推送",
        "PIL": "截图缩放（Pillow）",
        "Quartz": "macOS 光标/输入（pyobjc）",
    }
    for mod, why in required.items():
        if importlib.util.find_spec(mod) is None:
            bad(f"缺少 {mod}", why)
        else:
            ok(mod, why)


# ---------------------------------------------------------------- 配置

def check_config() -> None:
    head("配置")
    try:
        from supermouse.core.config import load_config
        cfg = load_config()
    except Exception as e:
        bad("配置加载失败", str(e))
        return
    ok("config.yaml 可加载", str(Path.home() / ".supermouse/config.yaml"))

    thr = cfg.get("eyes.calibration.hard_threshold")
    if thr is None:
        warn("重眨阈值未设置", "跑 python -m supermouse --calibrate")
    elif not (0.5 <= float(thr) <= 0.95):
        warn(f"重眨阈值 {thr} 偏离实测区间", "实测 0.70–0.90 无误判")
    else:
        ok(f"重眨阈值 {thr}", "实测 0.70–0.90 零误判")

    # 真实键名是 sense.work_scene（动作序列列表），不是 sense.scenes
    scene = cfg.get("sense.work_scene") or []
    if scene:
        steps = ", ".join(str(s.get("do", "?")) for s in scene if isinstance(s, dict))
        ok(f"工作场景 {len(scene)} 步", steps)
    else:
        warn("没有配置工作场景", "STRONG SENSE 切不回工作页面")

    count = cfg.get("sense.intruder_target_count", 2)
    if count not in (1, 2, 3):
        bad(f"雷达人数阈值无效: {count}", "sense.intruder_target_count 应为 1–3")
    else:
        ok(f"来人目标数阈值 {count}", "当前安装包含本人，应为 2；只扫过道才设 1")
    model = cfg.get("devices.radar.model", "ld2454")
    if count != 1 and model in ("ld2410", "text", "out"):
        warn(f"{model} 不支持可靠人数判定", "当前 MicroPython 桥应设 model: bridge；二进制直连才用 ld2454")

    fun = cfg.get("sense.fun_apps") or []
    ok(f"摸鱼名单 {len(fun)} 个", ", ".join(fun[:3])) if fun else \
        warn("摸鱼名单为空", "only_when_slacking 会一直不触发")


# ---------------------------------------------------------------- 硬件

def check_devices() -> None:
    head("硬件")
    try:
        from serial.tools import list_ports
    except Exception:
        bad("pyserial 不可用")
        return

    ports = [p for p in list_ports.comports()
             if "Bluetooth" not in p.device and "debug-console" not in p.device]
    if not ports:
        warn("没有任何 USB 串口", "全靠模拟器演示（F13–F18）")
    for p in ports:
        vid = f"VID={hex(p.vid)}" if p.vid is not None else "VID=?"
        pid = f"PID={hex(p.pid)}" if p.pid is not None else "PID=?"
        print(f"      {DIM}{p.device}  {vid} {pid}  {p.description}{RESET}")

    try:
        from supermouse.devices.bci.serial_client import find_port as bci_port
        bp = bci_port()
        ok(f"脑机 {bp}") if bp else warn("没找到脑机", "SHARP EYES 用 F13/F14/F15 模拟")
    except Exception as e:
        warn("脑机检测失败", str(e))

    try:
        from supermouse.core.config import load_config
        from supermouse.devices.radar.serial_client import RadarSerialClient, find_port, missing_port_hint
        from supermouse.core.bus import EventBus
        radar = RadarSerialClient(load_config(), EventBus())  # 只读配置，不启动线程/打开串口
        rp = radar.port or find_port(connection=radar.connection)
        if rp:
            present = any(p.device.replace("/dev/tty.", "/dev/cu.") == rp.replace("/dev/tty.", "/dev/cu.")
                          for p in ports)
            if present:
                ok(f"雷达数据串口 {rp}", f"{radar.connection} / {radar.model} / {radar.baud}")
                warn("尚未验证真实雷达帧", "运行 python tools/radar_probe.py --seconds 10；只找到端口不是验收")
            else:
                warn(f"配置的雷达端口不存在: {rp}", missing_port_hint(radar.connection))
        else:
            warn("没找到雷达数据串口", missing_port_hint(radar.connection))
    except Exception as e:
        warn("雷达检测失败", str(e))


# ---------------------------------------------------------------- 权限

def check_permissions() -> None:
    head("macOS 权限")
    if sys.platform != "darwin":
        warn("非 macOS，跳过")
        return
    try:
        import Quartz
        e = Quartz.CGEventCreate(None)
        p = Quartz.CGEventGetLocation(e)
        ok("可读光标位置", f"({p.x:.0f}, {p.y:.0f})")
    except Exception as ex:
        bad("读光标失败", str(ex))

    # AXIsProcessTrusted 在 ApplicationServices（HIServices）里，不在 Quartz
    try:
        from ApplicationServices import AXIsProcessTrusted
        if AXIsProcessTrusted():
            ok("辅助功能已授权", "眼势点击 / 悬停识别可用")
        else:
            bad("辅助功能未授权",
                "系统设置 → 隐私与安全性 → 辅助功能，勾选运行本程序的终端")
    except Exception as ex:
        warn("无法查询辅助功能授权", str(ex)[:60])

    # 屏幕录制权限：截图是慢路径建议的输入
    try:
        import Quartz
        img = Quartz.CGWindowListCreateImage(
            Quartz.CGRectMake(0, 0, 8, 8),
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault)
        if img and Quartz.CGImageGetWidth(img) > 0:
            ok("可截图", "SMART BRAIN 慢路径可用")
        else:
            warn("截图返回空", "屏幕录制权限可能未授权（慢路径会降级到快路径）")
    except Exception as ex:
        warn("截图检查失败", str(ex))

    if shutil.which("osascript"):
        try:
            r = subprocess.run(["osascript", "-e", 'return "ok"'],
                               capture_output=True, text=True, timeout=8)
            ok("AppleScript 可用") if r.returncode == 0 else \
                bad("AppleScript 失败", (r.stderr or "").strip()[:80])
        except Exception as ex:
            bad("AppleScript 超时/失败", str(ex))
    else:
        bad("找不到 osascript")


# ---------------------------------------------------------------- Claude API

def check_api(quick: bool) -> None:
    head("Claude API")
    try:
        from supermouse.modes.smart_brain import client
    except Exception as e:
        bad("client 导入失败", str(e))
        return

    if not client.available():
        bad("不可用", client.unavailable_reason() or "未知原因")
        print(f"      {DIM}export ANTHROPIC_API_KEY=sk-ant-…{RESET}")
        return
    ok("凭据已配置", f"模型 {client.MODEL}")

    if quick:
        print(f"      {DIM}--quick：跳过真实调用{RESET}")
        return

    t0 = time.time()
    try:
        text = client.summarize_text(
            "Super Mouse 是一只把脑机、雷达和 AI 装进光标里的老鼠。", name="自检", timeout=25.0)
        dt = time.time() - t0
        if text:
            ok(f"真实调用成功 {dt:.1f}s", text.strip()[:40])
            if dt > 6:
                warn(f"响应偏慢 {dt:.1f}s", "会场网络差 → 演示走快路径")
        else:
            warn(f"调用返回空 {dt:.1f}s", "演示走快路径（DEFAULT_PLANS）")
    except Exception as e:
        warn("调用失败", str(e)[:90])
        print(f"      {DIM}快路径不依赖网络，仍可演示{RESET}")


# ---------------------------------------------------------------- 演示物料

def check_assets() -> None:
    head("演示物料")
    for rel, why in (
        ("tools/dashboard.html", "评委看的波形面板"),
        ("firmware/ld2454_micropython/main.py", "S3 MicroPython 已跑通脚本存档"),
        ("README.md", "评委 10 分钟跑起来"),
    ):
        ok(rel, why) if (ROOT / rel).exists() else warn(f"缺 {rel}", why)

    rec = ROOT / "recordings"
    files = sorted(rec.glob("*.jsonl")) + sorted(rec.glob("*.bin")) if rec.exists() else []
    files = [p for p in files if p.stat().st_size > 0]
    if files:
        ok(f"非空录制文件 {len(files)} 份", "内容需核验；文件存在不代表雷达已验收")
    else:
        warn("没有录制数据", "python -m supermouse --record 留一份")


def main() -> int:
    ap = argparse.ArgumentParser(description="Super Mouse 演示前自检")
    ap.add_argument("--quick", action="store_true", help="跳过真实 API 调用")
    args = ap.parse_args()

    print(f"{DIM}Super Mouse 自检 · {time.strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    check_deps()
    check_config()
    check_devices()
    check_permissions()
    check_api(args.quick)
    check_assets()

    print("\n" + "─" * 56)
    if fails:
        print(f"{RED}{len(fails)} 项硬伤{RESET}" +
              (f" · {YELLOW}{len(warns)} 项警告{RESET}" if warns else ""))
        for f in fails:
            print(f"  {RED}•{RESET} {f}")
        return 1
    if warns:
        print(f"{GREEN}无硬伤{RESET} · {YELLOW}{len(warns)} 项警告（可降级演示）{RESET}")
        for w in warns:
            print(f"  {YELLOW}•{RESET} {w}")
        return 0
    print(f"{GREEN}全部通过 —— 可以上台{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
