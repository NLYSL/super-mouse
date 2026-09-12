"""眼势 → 动作映射。

配置形如：
    gestures:
      double_blink: { action: click }
      triple_blink: { action: keys, combo: "cmd+tab" }
      hard_blink:   { action: brain_confirm }

action 取值：
    click / right_click / double_click / keys / scene / brain_confirm / scroll / none
brain_confirm 不直接执行动作，而是发 ui.confirm_gesture 让 Smart Brain 接管；
若此刻没有待确认的建议，可以配 fallback 退化成别的动作。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 每个眼势对应的老鼠头顶图标
GESTURE_ICONS = {
    "double_blink": "🖱",
    "triple_blink": "⌘⇥",
    "hard_blink": "✅",
    "long_blink": "⏳",
}


class GestureMapper:
    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        self.pending_confirm = False     # Smart Brain 是否正在等确认
        self.suppressed = False          # 录制眼势时挂起动作，见下

    def install(self) -> None:
        self.bus.subscribe("eye.gesture", self.on_gesture)
        self.bus.subscribe("brain.suggest", lambda e: self._set_pending(True))
        self.bus.subscribe("brain.confirm", lambda e: self._set_pending(False))
        self.bus.subscribe("brain.cancel", lambda e: self._set_pending(False))
        # 绑定向导录制眼势期间必须挂起动作，否则用户一边录双眨、
        # 屏幕一边被真的点击，根本没法操作界面。
        self.bus.subscribe("ui.suppress_actions",
                           lambda e: self._set_suppressed(bool(e.get("on"))))

    def _set_pending(self, v: bool) -> None:
        self.pending_confirm = v

    def _set_suppressed(self, v: bool) -> None:
        self.suppressed = v
        log.info("眼势动作 %s", "已挂起（录制中）" if v else "已恢复")

    def on_gesture(self, evt: dict) -> None:
        gesture = evt.get("gesture")
        if self.suppressed:
            # 仍然让老鼠有反馈，但不执行动作
            self.bus.publish("mouse.state", anim="gesture",
                             icon=GESTURE_ICONS.get(gesture, "✨"), ttl_ms=400)
            return
        spec = self.cfg.get(f"eyes.gestures.{gesture}")
        if not isinstance(spec, dict):
            log.debug("眼势 %s 未映射", gesture)
            return

        self.bus.publish("mouse.state", anim="gesture",
                         icon=GESTURE_ICONS.get(gesture, "✨"), ttl_ms=600)
        self._execute(spec, gesture)

    def _execute(self, spec: dict, gesture: str) -> None:
        action = spec.get("action", "none")

        if action == "brain_confirm":
            if self.pending_confirm:
                self.bus.publish("ui.confirm_gesture", via="gesture")
                return
            fallback = spec.get("fallback")
            if isinstance(fallback, dict):
                log.debug("无待确认建议，%s 走 fallback", gesture)
                self._execute(fallback, gesture)
            return

        if action == "none":
            return
        if action == "click":
            self.bus.publish("action.click", button="left", count=1)
        elif action == "double_click":
            self.bus.publish("action.click", button="left", count=2)
        elif action == "right_click":
            self.bus.publish("action.click", button="right", count=1)
        elif action == "keys":
            combo = spec.get("combo")
            if combo:
                self.bus.publish("action.keys", combo=combo)
        elif action == "scene":
            scene = spec.get("scene")
            if scene:
                self.bus.publish("action.scene", scene=scene)
        elif action == "scroll":
            self.bus.publish("action.scroll", dy=int(spec.get("dy", -3)))
        else:
            log.warning("未知的映射动作：%s", action)
