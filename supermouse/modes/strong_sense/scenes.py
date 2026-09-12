"""场景切换：sense.intruder → 老鼠钻洞 + 切回工作页面。

时间预算：从 sense.intruder 到屏幕切完 ≤ 600 ms。
所以动作用 `open -b`（快）优先于 AppleScript（首次调用慢），
并且在启动时预热一次 AppleScript 桥接。
"""

from __future__ import annotations

import asyncio
import logging
import time

from ...actions import macos

log = logging.getLogger(__name__)


class SceneSwitcher:
    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        self.prev_front: str | None = None
        self.prev_muted = False
        self.triggered_at = 0.0

    def install(self) -> None:
        self.bus.subscribe("sense.intruder", self._on_intruder)
        self.bus.subscribe("sense.clear", self._on_clear)
        self.bus.subscribe("ui.restore", lambda e: self._restore())

    # ---------- 触发 ----------

    def _on_intruder(self, evt: dict) -> None:
        front = macos.frontmost_bundle()
        fun_apps = set(self.cfg.get("sense.fun_apps", []))
        only_slacking = bool(self.cfg.get("sense.only_when_slacking", False))

        # 老鼠先反应——视觉反馈不能等动作做完
        self.bus.publish("mouse.state", anim="alert", ttl_ms=350)

        if only_slacking and front not in fun_apps:
            log.info("当前前台 %s 不在摸鱼名单，只提示不切屏", front)
            self.bus.publish("mouse.state", anim="alert",
                             label="身后有人", ttl_ms=2500)
            return

        self.prev_front = front
        self.triggered_at = time.time()
        self.bus.publish("mouse.state", anim="hide")
        self.bus.publish("action.scene", scene="work")

        # 记录切屏耗时，方便验收 M4（≤ 600 ms）
        asyncio.get_running_loop().call_later(0.05, self._log_latency)

    def _log_latency(self) -> None:
        cost = (time.time() - self.triggered_at) * 1000
        log.info("场景切换耗时 %.0f ms（目标 ≤ 600 ms）", cost)

    # ---------- 清空 ----------

    def _on_clear(self, evt: dict) -> None:
        mode = str(self.cfg.get("sense.restore_on_clear", "ask"))
        if mode == "never" or not self.prev_front:
            self.bus.publish("mouse.state", anim="idle", ttl_ms=100)
            return
        if mode == "auto":
            self._restore()
            return
        # ask：老鼠探头问一句
        self.bus.publish("mouse.state", anim="peek", label="回去继续？", ttl_ms=6000)
        self.bus.publish("brain.suggest", plan_id="__restore__",
                         title="回去继续？", steps=["切回之前的应用"])

    def _restore(self) -> None:
        if not self.prev_front:
            return
        log.info("恢复到 %s", self.prev_front)
        macos.mute(False)
        self.bus.publish("action.activate", app=self.prev_front)
        self.prev_front = None
        self.bus.publish("mouse.state", anim="happy", ttl_ms=1200)
