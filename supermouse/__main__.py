"""SUPER MOUSE 入口。

    python -m supermouse                 连接真实硬件（脑机走串口，雷达暂用模拟器）
    python -m supermouse --sim           完全不连硬件，全靠 F13-F18 热键
    python -m supermouse --bci-only      只跑脑机链路，不开 Overlay（调设备用）
    python -m supermouse --calibrate     启动后立即进入校准向导
    python -m supermouse --record        把事件录到 recordings/
    python -m supermouse --replay f.jsonl 回放录制的数据
    python -m supermouse --debug         开 WebSocket 调试面板

Qt 主线程通过 qasync 承载 asyncio 事件循环：Overlay 用 QTimer 跟随光标，
设备读取在独立线程，Claude 调用在线程池——绝不阻塞主线程，否则老鼠会僵住。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path

from .core.bus import EventBus
from .core.config import load_config

log = logging.getLogger("supermouse")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="supermouse", description="SUPER MOUSE — 4 个模式的老鼠光标")
    p.add_argument("--sim", action="store_true", help="不连硬件，用热键模拟事件")
    p.add_argument("--bci-only", action="store_true", help="只跑脑机链路，不开 UI")
    p.add_argument("--no-ui", action="store_true", help="不开 Overlay（headless）")
    p.add_argument("--calibrate", action="store_true", help="启动后立即校准")
    p.add_argument("--record", action="store_true", help="录制事件到 recordings/")
    p.add_argument("--record-raw", action="store_true", help="录制时包含原始 EEG（文件很大）")
    p.add_argument("--replay", metavar="FILE", help="回放 JSONL 录制")
    p.add_argument("--speed", type=float, default=1.0, help="回放速度")
    p.add_argument("--loop", action="store_true", help="回放循环")
    p.add_argument("--debug", action="store_true", help="开 WebSocket 调试面板 + DEBUG 日志")
    p.add_argument("--config", metavar="FILE", help="指定配置文件")
    return p.parse_args(argv)


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-34s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("asyncio", "websockets", "urllib3", "httpx", "anthropic", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class App:
    """把所有部件装起来。"""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg = load_config(args.config)
        record_path = None
        if args.record:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            record_path = Path("recordings") / f"{stamp}.jsonl"
        self.bus = EventBus(record_path=record_path, record_raw=args.record_raw)

        self.bci = None
        self.radar = None
        self.sim = None
        self.eyes = None
        self.brain = None
        self.sense = None
        self.executor = None
        self.overlay = None
        self.sign = None
        self.panel = None
        self.ws = None
        self.tasks: list[asyncio.Task] = []

    # ---------- 装配 ----------

    def build_ui(self) -> None:
        if self.args.no_ui or self.args.bci_only:
            return
        from .ui.overlay import MouseOverlay, SignWindow
        self.overlay = MouseOverlay(self.cfg, self.bus)
        self.sign = SignWindow(self.cfg, self.bus)
        self.overlay.show()
        log.info("老鼠光标已上线")

        # 控制面板：每个模式一个开关。各模式实时读配置，所以拨了立刻生效。
        from .ui.panel import ControlPanel
        self.panel = ControlPanel(self.cfg, self.bus)
        self.panel.show_front()
        log.info("控制面板已打开")

    def build_devices(self) -> None:
        if self.args.replay:
            return
        use_sim = self.args.sim
        if not use_sim:
            from .devices.bci.serial_client import BCISerialClient, find_port
            configured = self.cfg.get("devices.bci.port")
            if configured in (None, "", "auto") and find_port() is None:
                log.warning("没找到脑机串口，自动切换到模拟器模式")
                use_sim = True
            else:
                self.bci = BCISerialClient(self.cfg, self.bus)
                self.bci.start()

        # 雷达线程自己等候正确端口，驱动就绪/热插拔后无需重启应用。
        if not self.args.sim and self.cfg.get("devices.radar.enabled", True):
            from .devices.radar import RadarSerialClient
            self.radar = RadarSerialClient(self.cfg, self.bus)
            self.radar.start()

        # 模拟器总是启用：任一硬件掉线都能立刻接手演示
        from .devices.simulator import HotkeySimulator
        self.sim = HotkeySimulator(self.cfg, self.bus)
        if use_sim:
            log.info("模拟器模式：F13 双眨 · F14 三眨 · F15 重眨 · F18 单眨")
        if self.radar is None:
            log.info("F16 模拟身后有人 · F17 清空 · Esc Esc 急停")

    def build_modes(self) -> None:
        from .actions.executor import ActionExecutor
        self.executor = ActionExecutor(self.cfg, self.bus)
        self.executor.install()

        from .modes.sharp_eyes import SharpEyes
        self.eyes = SharpEyes(self.cfg, self.bus, bci_client=self.bci)
        self.eyes.install()

        if self.args.bci_only:
            return

        from .modes.strong_sense import StrongSense
        self.sense = StrongSense(self.cfg, self.bus)
        self.sense.install()

        if self.cfg.get("brain.enabled", True):
            from .modes.smart_brain import SmartBrain
            self.brain = SmartBrain(self.cfg, self.bus)
            self.brain.install()

    # ---------- 运行 ----------

    async def run(self) -> None:
        self.bus.attach_loop(asyncio.get_running_loop())
        self.build_ui()
        self.build_devices()
        self.build_modes()

        if self.sim is not None:
            self.sim.start()

        if self.args.debug:
            from .core.ws import WSBroadcaster
            self.ws = WSBroadcaster(self.bus)
            self.tasks.append(asyncio.create_task(self.ws.run()))

        if self.args.replay:
            from .devices.simulator import Replayer
            rp = Replayer(self.bus, self.args.replay, self.args.speed, self.args.loop)
            self.tasks.append(asyncio.create_task(rp.run()))

        if self.args.calibrate:
            await asyncio.sleep(1.0)
            self.bus.publish("ui.calibrate")

        self._log_ready()
        self.bus.publish("mouse.state", anim="happy", ttl_ms=1500)

        try:
            await asyncio.Future()          # 一直跑到被取消
        except asyncio.CancelledError:
            pass
        finally:
            self.shutdown()

    def _log_ready(self) -> None:
        rows = [
            ("SHARP EYES", "✅ 真实脑机" if self.bci else "🔸 模拟器"),
            ("SMART BRAIN", self._brain_status()),
            ("STRONG SENSE", "✅ 真实雷达" if self.radar else "🔸 模拟器（F16/F17）"),
            ("SOFT HEART", "⏸ 待确认"),
        ]
        log.info("─" * 52)
        for name, status in rows:
            log.info("  %-13s %s", name, status)
        log.info("─" * 52)

    def _brain_status(self) -> str:
        if not self.brain:
            return "⏸ 已关闭"
        from .modes.smart_brain import client as bc
        if bc.available():
            return "✅ 在线"
        return f"🔸 离线（{bc.unavailable_reason()}）"

    def shutdown(self) -> None:
        log.info("正在退出…")
        for t in self.tasks:
            t.cancel()
        if self.brain:
            self.brain.stop()
        if self.sim:
            self.sim.stop()
        if self.radar:
            self.radar.stop()
        if self.bci:
            self.bci.stop()
        self.bus.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.debug)

    headless = args.no_ui or args.bci_only
    if headless:
        return _run_headless(args)
    return _run_qt(args)


def _run_headless(args: argparse.Namespace) -> int:
    app = App(args)

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: [t.cancel() for t in asyncio.all_tasks()])
            except NotImplementedError:
                pass
        await app.run()

    try:
        asyncio.run(runner())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    return 0


def _run_qt(args: argparse.Namespace) -> int:
    import qasync
    from PyQt6.QtWidgets import QApplication

    qt = QApplication(sys.argv[:1])
    qt.setQuitOnLastWindowClosed(False)

    # 有了控制面板就必须用 Regular 策略：Accessory 下窗口拿不到键盘焦点，
    # 面板上的开关点不动。代价是 Dock 里会出现图标 —— 值得，用户需要能操作它。
    if not args.no_ui:
        try:
            from AppKit import NSApp, NSApplicationActivationPolicyRegular
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        except Exception as e:
            log.debug("设置 activation policy 失败：%s", e)

    loop = qasync.QEventLoop(qt)
    asyncio.set_event_loop(loop)

    app = App(args)
    with loop:
        try:
            loop.run_until_complete(app.run())
        except (KeyboardInterrupt, asyncio.CancelledError):
            app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
