"""悬停识别：光标停在哪个应用/文件上。

每 100 ms 读光标位置，位移 < 6 px 算静止；累计静止 ≥ hover_dwell_ms 时
用辅助功能 API 查出光标下是什么，发 brain.hover。

过滤规则（避免在用户正在用的窗口里到处弹建议）：
  - 目标就是当前前台应用，且不是文件或 Dock 图标 → 忽略
  - 同一目标在 repeat_cooldown_s 内已经建议过 → 忽略
  - 光标移开 > 40 px → 取消慢路径；已显示的牌子保留到 TTL，允许移动过去点击
"""

from __future__ import annotations

import asyncio
import logging
import time

from ...actions import macos

log = logging.getLogger(__name__)

POLL_S = 0.1
STILL_PX = 6
LEAVE_PX = 40


class HoverWatcher:
    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        self.dwell_s = float(cfg.get("brain.hover_dwell_ms", 700)) / 1000.0
        self.cooldown = float(cfg.get("brain.repeat_cooldown_s", 30))

        self._last = (0.0, 0.0)
        self._still_since = time.time()
        self._fired_at: tuple[float, float] | None = None
        self._recent: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("brain.enabled", True))

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def run(self) -> None:
        while True:
            try:
                await asyncio.sleep(POLL_S)
                self._poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("悬停检测出错")
                await asyncio.sleep(1.0)

    def _poll(self) -> None:
        x, y = macos.cursor_pos()
        dx = abs(x - self._last[0]) + abs(y - self._last[1])
        now = time.time()

        if dx > STILL_PX:
            self._last = (x, y)
            self._still_since = now
            if self._fired_at and (abs(x - self._fired_at[0]) + abs(y - self._fired_at[1])) > LEAVE_PX:
                self._fired_at = None
                self.bus.publish("brain.hover_left")
            return

        if self._fired_at is not None:      # 这次静止已经报过了
            return
        if now - self._still_since < self.dwell_s:
            return
        if not self.enabled:
            return

        info = macos.element_under_cursor(x, y)
        if not info or not info.get("app"):
            self._fired_at = (x, y)          # 标记为已处理，避免每 100 ms 重查
            return

        if info["kind"] not in ("file", "dock", "finder_item") and info["app"] == macos.frontmost_bundle():
            self._fired_at = (x, y)
            return

        key = f"{info['app']}|{info.get('path') or info.get('title') or ''}"
        if now - self._recent.get(key, 0) < self.cooldown:
            self._fired_at = (x, y)
            return

        self._recent[key] = now
        self._fired_at = (x, y)
        log.info("悬停：%s (%s) %s", info["app"], info["kind"], info.get("title") or "")
        self.bus.publish("brain.hover", app=info["app"], kind=info["kind"],
                         title=info.get("title"), path=info.get("path"),
                         dwell_ms=int((now - self._still_since) * 1000))
