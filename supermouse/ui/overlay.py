"""老鼠光标 Overlay。

两个窗口：
  MouseOverlay  跟随光标的老鼠本体，**点击穿透**（绝不能挡住用户点击）
  SignWindow    老鼠举的牌子，接受点击（用来确认建议），只在有建议时出现

动画状态机由 mouse.state 事件驱动：
  {"anim": "suggest", "label": "要我起草回复吗？", "ttl_ms": 4000}
ttl 到期后回落到基础状态（idle / walk，按光标是否移动）。
"""

from __future__ import annotations

import logging
import time

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QCursor, QPainter
from PyQt6.QtWidgets import QWidget

from . import sprites

log = logging.getLogger(__name__)

# NSWindow 层级。数值越大越靠前。
#   NSStatusWindowLevel = 25（菜单栏）
#   NSScreenSaverWindowLevel = 1000
# 老鼠是光标，必须盖住一切；牌子略低一层，免得挡住老鼠自己。
LEVEL_MOUSE = 1000
LEVEL_SIGN = 999


def fix_native_window(widget, *, ignore_mouse: bool, level: int,
                      what: str) -> bool:
    """把 Qt 窗口底下的 NSWindow 调成"常驻、不抢焦点"。

    为什么必须做：`Qt.WindowType.Tool` 在 macOS 上映射成 **NSPanel**，
    而 NSPanel 的 `hidesOnDeactivate` 默认是 **true** —— 本 App 一失去焦点，
    窗口就自动隐藏。表现正是"只有把 Super Mouse 放到台前，老鼠才出现"。

    顺带修两件事：
      · collectionBehavior 默认只待在当前 Space，切桌面/别的 App 全屏时老鼠会消失
      · 窗口层级不够高会被别的窗口盖住

    NSWindow 只有 show() 之后才存在，所以调用点在 showEvent 而不是 __init__。

    返回 True = 修好了；False = 没修成（功能仍可用，只是会退回失焦隐藏的旧行为）。
    """
    try:
        import objc
        view = objc.objc_object(c_void_p=int(widget.winId()))
        win = view.window()
        if win is None:
            return False

        win.setHidesOnDeactivate_(False)      # ← 核心修复

        # CanJoinAllSpaces(1) | Stationary(16) | IgnoresCycle(64)
        #   | FullScreenAuxiliary(256)
        win.setCollectionBehavior_(1 | 16 | 64 | 256)
        win.setLevel_(level)

        if ignore_mouse:
            # 老鼠绝不能吃点击；牌子要能点，所以不设
            win.setIgnoresMouseEvents_(True)

        log.info("%s窗口已修正（失焦不隐藏 · 跨 Space · level=%d）", what, level)
        return True
    except Exception as e:
        log.warning("%s窗口修正失败，失焦时可能隐藏：%s", what, e)
        return False

CANVAS_W, CANVAS_H = 170, 140
NOSE_X, NOSE_Y = 46, 86          # 鼻尖在画布里的位置

# 这些状态是"持续"的，不会自动回落到 idle
STICKY = {"thinking", "suggest", "working", "alert", "hide", "peek", "sleepy"}
FOLLOW_MS = 8
FRAME_MS = 80


class MouseOverlay(QWidget):
    def __init__(self, cfg, bus):
        super().__init__(None)
        self.cfg = cfg
        self.bus = bus
        self.scale = float(cfg.get("mouse.scale", 1.0))
        hotspot = cfg.get("mouse.hotspot") or [16, 10]
        # 系统光标没隐藏时把老鼠往右下挪一点，免得和箭头叠在一起
        self.offset = (float(hotspot[0]), float(hotspot[1])) \
            if not cfg.get("mouse.hide_system_cursor", False) else (0.0, 0.0)

        self.anim = "idle"
        self.icon: str | None = None
        self.frame = 0
        self.facing = 1
        self._anim_until = 0.0
        self._last_pos = QCursor.pos()
        self._last_move = 0.0

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.resize(CANVAS_W, CANVAS_H)

        # 必须在 show() 之后才有 NSWindow，见 showEvent()
        self._native_fixed = False

        self._follow = QTimer(self)
        self._follow.timeout.connect(self._tick)
        self._follow.start(FOLLOW_MS)

        self._animate = QTimer(self)
        self._animate.timeout.connect(self._next_frame)
        self._animate.start(FRAME_MS)

        bus.subscribe("mouse.state", self.on_mouse_state)

    # ---------- 原生窗口修正 ----------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._native_fixed:
            self._native_fixed = fix_native_window(
                self, ignore_mouse=True, level=LEVEL_MOUSE, what="老鼠")

    # ---------- 事件 ----------

    def on_mouse_state(self, evt: dict) -> None:
        anim = evt.get("anim")
        if not anim:
            return
        ttl = evt.get("ttl_ms")
        self.anim = anim
        self.icon = evt.get("icon")
        if ttl:
            self._anim_until = time.time() + float(ttl) / 1000.0
        elif anim in STICKY:
            self._anim_until = 0.0            # 一直保持
        else:
            self._anim_until = time.time() + 0.4

    # ---------- 循环 ----------

    def _tick(self) -> None:
        pos = QCursor.pos()
        dx = pos.x() - self._last_pos.x()
        dy = pos.y() - self._last_pos.y()
        moved = abs(dx) + abs(dy)

        if moved > 1:
            self._last_move = time.time()
            if abs(dx) > 2:
                self.facing = 1 if dx > 0 else -1
        self._last_pos = pos

        self.move(int(pos.x() - NOSE_X + self.offset[0]),
                  int(pos.y() - NOSE_Y + self.offset[1]))

        # 基础状态回落
        if self._anim_until and time.time() > self._anim_until:
            self._anim_until = 0.0
            self.anim = self._base_anim()
            self.icon = None
        elif self.anim in ("idle", "walk"):
            self.anim = self._base_anim()

    def _base_anim(self) -> str:
        return "walk" if time.time() - self._last_move < 0.12 else "idle"

    def _next_frame(self) -> None:
        self.frame += 1
        self.update()

    # ---------- 绘制 ----------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.translate(NOSE_X, NOSE_Y)
        sprites.draw_mouse(p, self.anim, self.frame, facing=self.facing, scale=self.scale)
        if self.icon:
            p.resetTransform()
            sprites.draw_icon_bubble(p, self.icon, NOSE_X - 4, NOSE_Y - 58)
        p.end()


class SignWindow(QWidget):
    """老鼠举的牌子。独立窗口，接受点击 → 发 ui.click_suggest。"""

    def __init__(self, cfg, bus):
        super().__init__(None)
        self.bus = bus
        self.text = ""
        self.plan_id: str | None = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resize(300, 52)
        self._native_fixed = False
        self.hide()

        bus.subscribe("brain.suggest", self._on_suggest)
        bus.subscribe("brain.cancel", lambda e: self.dismiss())
        bus.subscribe("brain.confirm", lambda e: self.dismiss())
        bus.subscribe("brain.action", self._on_action)

        # 空格 = 确认牌子：只在"举着可点击的牌子"时响应；
        # 没牌子（或牌子只是进度提示）时空格照常落到前台 App。
        # 只观察不拦截 —— 空格仍会传给当前应用。
        try:
            from pynput import keyboard

            def on_press(key) -> None:
                if key is keyboard.Key.space and self.plan_id:
                    self.bus.publish("ui.click_suggest", plan_id=self.plan_id)

            self._space_listener = keyboard.Listener(on_press=on_press)
            self._space_listener.daemon = True
            self._space_listener.start()
        except Exception as e:
            log.warning("空格确认监听不可用（点牌子仍可确认）：%s", e)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._native_fixed:
            # 注意 ignore_mouse=False：牌子必须能被点击（要发 ui.click_suggest）
            self._native_fixed = fix_native_window(
                self, ignore_mouse=False, level=LEVEL_SIGN, what="牌子")

    def _on_suggest(self, evt: dict) -> None:
        self.plan_id = evt.get("plan_id")
        self.show_text(evt.get("title", ""))

    def _on_action(self, evt: dict) -> None:
        msg = evt.get("message")
        status = evt.get("status")
        if status == "running" and msg:
            self.show_text(msg, clickable=False)
        elif status in ("done", "partial", "error"):
            if msg:
                self.show_text(msg, clickable=False)
                QTimer.singleShot(3000, self.dismiss)
            else:
                self.dismiss()

    def show_text(self, text: str, clickable: bool = True) -> None:
        self.text = text
        self.plan_id = self.plan_id if clickable else None
        pos = QCursor.pos()
        self.move(int(pos.x() - 40), int(pos.y() - 108))
        self.update()
        if not self.isVisible():
            self.show()
            self.raise_()

    def dismiss(self) -> None:
        self.plan_id = None
        self.hide()

    def mousePressEvent(self, event) -> None:
        if self.plan_id:
            self.bus.publish("ui.click_suggest", plan_id=self.plan_id)
        event.accept()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        rect = sprites.draw_sign(p, self.text, 6, 6, max_width=self.width() - 24)
        p.end()
        # 让窗口宽度贴合内容
        want = int(rect.width() + 20)
        if abs(want - self.width()) > 8:
            self.resize(want, self.height())
