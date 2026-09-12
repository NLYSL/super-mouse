"""眼势识别状态机。

规则：缓冲眨眼，**静默 450 ms 后判定** —— 因为"重眨"和"双眨的第一下"在发生瞬间
无法区分，必须等一等看有没有第二下。long_blink 例外，在眨眼结束时立即判定。

  n >= 3                        → triple_blink
  n == 2 且间隔在 double_gap_ms → double_blink
  n == 1 且 strength >= 阈值    → hard_blink
  n == 1 普通眨眼               → 忽略（自然眨眼每分钟 15-20 次，绝不能触发动作）

门控：poor_signal 超过 quality_gate 时丢弃所有眨眼；eyes.enabled 为假时只镜像不识别。
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)


class GestureRecognizer:
    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        calib = cfg.section("eyes.calibration")
        gap = calib.get("double_gap_ms") or [120, 450]
        self.gap = (gap[0] / 1000.0, gap[1] / 1000.0)
        self.hard_threshold = float(calib.get("hard_threshold") or 0.80)
        self.long_ms = float(calib.get("long_ms") or 400)
        self.refractory = float(cfg.get("eyes.refractory_ms", 500)) / 1000.0
        self.quality_gate = int(cfg.get("eyes.quality_gate", 50))

        self.buf: list[tuple[float, float]] = []   # (t, strength)
        self.last_gesture_t = 0.0
        self.poor_signal = 0
        self._timer: asyncio.TimerHandle | None = None
        self.paused = False                        # 校准期间暂停识别

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("eyes.enabled", True)) and not self.paused

    def reload(self) -> None:
        """校准写回配置后调用，刷新阈值。"""
        calib = self.cfg.section("eyes.calibration")
        gap = calib.get("double_gap_ms") or [120, 450]
        self.gap = (gap[0] / 1000.0, gap[1] / 1000.0)
        self.hard_threshold = float(calib.get("hard_threshold") or 0.80)

    # ---------- 事件入口 ----------

    def on_quality(self, evt: dict) -> None:
        self.poor_signal = int(evt.get("poor_signal", 0))
        if self.poor_signal > self.quality_gate:
            self._cancel_timer()
            self.buf.clear()

    def on_blink(self, evt: dict) -> None:
        t = float(evt.get("t") or time.time())
        strength = float(evt.get("strength") or 0.0)
        duration = evt.get("duration_ms")

        # 老鼠始终跟着眨眼——即使信号差或功能关闭，这是"它在读我的眼睛"的可见证明
        self.bus.publish("mouse.state", anim="blink_mirror", ttl_ms=220)

        if self.poor_signal > self.quality_gate:
            return
        if not self.enabled:
            return

        if duration is not None and float(duration) >= self.long_ms:
            self._cancel_timer()
            self.buf.clear()
            self._emit("long_blink", t)
            return

        # 距离上一下太久 → 是新的一组
        if self.buf and (t - self.buf[-1][0]) > self.gap[1] + 0.3:
            self.buf.clear()

        self.buf.append((t, strength))
        self._arm_timer()

    # ---------- 判定 ----------

    def _arm_timer(self) -> None:
        self._cancel_timer()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._decide()      # 没有事件循环（单元测试）→ 立即判定
            return
        self._timer = loop.call_later(self.gap[1], self._decide)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _decide(self) -> None:
        self._timer = None
        if not self.buf:
            return
        buf, self.buf = self.buf, []
        n = len(buf)
        t = buf[-1][0]

        gesture: str | None = None
        if n >= 3:
            gesture = "triple_blink"
        elif n == 2:
            if self.gap[0] <= (buf[1][0] - buf[0][0]) <= self.gap[1]:
                gesture = "double_blink"
        elif n == 1 and buf[0][1] >= self.hard_threshold:
            gesture = "hard_blink"

        if gesture:
            self._emit(gesture, t)

    def _emit(self, gesture: str, t: float) -> None:
        if t - self.last_gesture_t < self.refractory:
            log.debug("眼势 %s 在不应期内，忽略", gesture)
            return
        self.last_gesture_t = t
        log.info("眼势：%s", gesture)
        self.bus.publish("eye.gesture", gesture=gesture)
