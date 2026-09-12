"""模拟器：没有硬件也能跑完整链路。

两种用法
  热键模拟   F13-F18 触发伪事件（雷达没接上时，Strong Sense 全靠这个）
  JSONL 回放 把录制的真实数据重新灌进总线

热键用 pynput 全局监听，需要"输入监控"权限。
另外注册了 Esc Esc 急停 和 ⌥⌘S 切换模拟器。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


class HotkeySimulator:
    """把功能键映射成伪硬件事件。"""

    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        self.loop: asyncio.AbstractEventLoop | None = None
        self._listener = None
        self._esc_times: list[float] = []
        self._radar_task: asyncio.Task | None = None

        keys = cfg.section("sim.hotkeys")
        self.bindings = {
            str(keys.get("double_blink", "f13")).lower(): self.fake_double_blink,
            str(keys.get("triple_blink", "f14")).lower(): self.fake_triple_blink,
            str(keys.get("hard_blink", "f15")).lower(): self.fake_hard_blink,
            str(keys.get("intruder", "f16")).lower(): self.fake_intruder,
            str(keys.get("clear", "f17")).lower(): self.fake_clear,
            str(keys.get("blink", "f18")).lower(): self.fake_blink,
        }

    def start(self) -> None:
        try:
            from pynput import keyboard
        except Exception as e:
            log.warning("pynput 不可用，热键模拟关闭：%s", e)
            return

        self.loop = asyncio.get_running_loop()

        def on_press(key) -> None:
            name = _key_name(key)
            if name is None:
                return
            if name == "esc":
                self._on_esc()
                return
            fn = self.bindings.get(name)
            if fn is not None and self.loop is not None:
                self.loop.call_soon_threadsafe(fn)

        self._listener = keyboard.Listener(on_press=on_press)
        self._listener.daemon = True
        self._listener.start()
        log.info("模拟器热键已启用：%s", " ".join(f"{k}" for k in self.bindings))

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None

    def _on_esc(self) -> None:
        now = time.time()
        self._esc_times = [t for t in self._esc_times if now - t < 0.6] + [now]
        if len(self._esc_times) >= 2 and self.loop:
            self._esc_times.clear()
            self.loop.call_soon_threadsafe(lambda: self.bus.publish("sys.stop_all"))

    # ---------- 伪事件 ----------

    def fake_blink(self, strength: float = 0.3, duration_ms: float = 140) -> None:
        self.bus.publish("bci.blink", strength=strength, duration_ms=duration_ms,
                         peak=strength * 100, source="sim")

    def fake_double_blink(self) -> None:
        asyncio.create_task(self._burst(2, gap=0.22))

    def fake_triple_blink(self) -> None:
        asyncio.create_task(self._burst(3, gap=0.20))

    def fake_hard_blink(self) -> None:
        self.fake_blink(strength=0.92, duration_ms=260)

    async def _burst(self, n: int, gap: float) -> None:
        for i in range(n):
            self.fake_blink()
            if i < n - 1:
                await asyncio.sleep(gap)

    def fake_intruder(self) -> None:
        """模拟有人从 3 m 走到 1.2 m：连发几帧递进的雷达数据。"""
        if self._radar_task and not self._radar_task.done():
            return
        self._radar_task = asyncio.create_task(self._approach())

    def _radar_frame(self, intruder_cm: int | None, speed: int = 0) -> None:
        # 模拟与真实 LD2454 相同的目标列表：本人在 0.5 m，第二人在后方。
        count = int(self.cfg.get("sense.intruder_target_count", 2))
        targets = [{"x_mm": 0, "y_mm": 500 + i * 500, "speed_cms": 0,
                    "dist_mm": 500 + i * 500} for i in range(count - 1)]
        if intruder_cm is not None:
            targets.append({"x_mm": 0, "y_mm": intruder_cm * 10,
                            "speed_cms": speed, "dist_mm": intruder_cm * 10})
        moving = intruder_cm is not None and speed != 0
        self.bus.publish("radar.frame", targets=targets,
                         state=("moving" if moving else "static") if targets else "none",
                         moving_cm=intruder_cm if moving else 0,
                         moving_energy=100 if moving else 0,
                         static_cm=targets[0]["dist_mm"] // 10 if targets else 0,
                         static_energy=100 if targets else 0,
                         detect_cm=targets[0]["dist_mm"] // 10 if targets else 0,
                         model="ld2454", source="sim")

    async def _approach(self) -> None:
        for dist_cm in (300, 265, 230, 190, 150, 120):
            self._radar_frame(dist_cm, speed=-40)
            await asyncio.sleep(0.1)
        for _ in range(30):
            self._radar_frame(120)
            await asyncio.sleep(0.3)

    def fake_clear(self) -> None:
        if self._radar_task and not self._radar_task.done():
            self._radar_task.cancel()
        self._radar_frame(None)  # 第二人离开，留下本人
        self.bus.publish("sense.clear")  # 演示快捷键立即清空，同步 detector 的 active


class Replayer:
    """回放录制的 JSONL。用来在没有硬件的机器上开发上层逻辑。"""

    def __init__(self, bus, path: str | Path, speed: float = 1.0, loop: bool = False):
        self.bus = bus
        self.path = Path(path).expanduser()
        self.speed = max(speed, 0.01)
        self.loop = loop

    async def run(self) -> None:
        events = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        if not events:
            log.warning("回放文件 %s 里没有事件", self.path)
            return

        log.info("回放 %s：%d 个事件，速度 %.1fx", self.path.name, len(events), self.speed)
        while True:
            t0 = events[0].get("t", 0)
            wall0 = time.time()
            for evt in events:
                target = (evt.get("t", t0) - t0) / self.speed
                delay = target - (time.time() - wall0)
                if delay > 0:
                    await asyncio.sleep(delay)
                etype = evt.pop("type", None)
                evt.pop("t", None)
                if etype:
                    self.bus.publish(etype, **evt)
                evt["type"] = etype     # 放回去，方便 loop 时复用
            if not self.loop:
                return
            log.info("回放结束，重新开始")


def _key_name(key) -> str | None:
    """把 pynput 的 key 对象转成小写名字，如 "f13" / "esc"。"""
    name = getattr(key, "name", None)
    if name:
        return name.lower()
    ch = getattr(key, "char", None)
    return ch.lower() if ch else None
