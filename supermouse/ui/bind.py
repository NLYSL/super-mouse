"""眼势校准 + 热键绑定向导。

绑定流程按用户描述设计：**先做眼势，再按热键**。

    1. 点「绑定眼势」
    2. 对着屏幕做一个眼势（比如用力眨两下）—— 系统认出是哪个眼势
    3. 按下想绑的热键（比如 ⌃V）—— 系统记下按键组合
    4. 保存 → 写进 config.yaml 的 eyes.gestures

两个必须处理的坑：

**录制期间要挂起动作。** 否则你正在录「双眨」，而双眨本身绑的是点击，
屏幕会被真的点一下 —— 界面根本没法操作。所以进入录制就发
`ui.suppress_actions on=True`，退出时恢复。

**Qt 在 macOS 上会交换 Ctrl 和 Cmd。** Qt 为了跨平台一致，默认把
`ControlModifier` 映射到 ⌘、`MetaModifier` 映射到 ⌃（Qt 文档 "Qt for macOS
- Signals and Slots"）。直接照搬会让用户按 ⌃V 却存成 `cmd+v`。
这里显式换回来，并且把捕获结果原样显示，用户能立刻看出对不对。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QKeyEvent
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..actions.macos import KEYCODES, MODIFIERS

log = logging.getLogger(__name__)

GESTURE_NAMES = {
    "double_blink": "双眨",
    "triple_blink": "三眨",
    "hard_blink": "重眨（用力眨一下）",
    "long_blink": "长眨（闭眼停一会）",
}

ACTION_NAMES = {
    "click": "鼠标点击",
    "keys": "按键组合",
    "brain_confirm": "确认 SUPER BRAIN 的建议",
    "scene": "执行场景",
    "none": "不做任何事",
}

# 配色与面板一致
CARD     = "#1a1a19"
INK      = "#ffffff"
MUTED    = "#898781"
BORDER   = "rgba(255,255,255,0.10)"
GOOD     = "#0ca30c"
WARNING  = "#fab219"
CRITICAL = "#d03b3b"
BLUE     = "#3987e5"

# Qt 键码 → macos.KEYCODES 里的名字
_QT_KEYS: dict[int, str] = {
    Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Space: "space",
    Qt.Key.Key_Return: "return",
    Qt.Key.Key_Enter: "return",
    Qt.Key.Key_Escape: "escape",
    Qt.Key.Key_Backspace: "delete",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Right: "right",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Down: "down",
    Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "page_up",
    Qt.Key.Key_PageDown: "page_down",
    Qt.Key.Key_Minus: "minus",
    Qt.Key.Key_Equal: "equal",
    Qt.Key.Key_Comma: "comma",
    Qt.Key.Key_Period: "period",
    Qt.Key.Key_Slash: "slash",
    Qt.Key.Key_Semicolon: "semicolon",
    Qt.Key.Key_Apostrophe: "quote",
    Qt.Key.Key_BracketLeft: "left_bracket",
    Qt.Key.Key_BracketRight: "right_bracket",
    Qt.Key.Key_Backslash: "backslash",
    Qt.Key.Key_QuoteLeft: "grave",
}
for _i in range(1, 13):
    _QT_KEYS[getattr(Qt.Key, f"Key_F{_i}")] = f"f{_i}"

# 只作为修饰键出现、不能单独当主键
_MOD_ONLY = {
    Qt.Key.Key_Control, Qt.Key.Key_Meta, Qt.Key.Key_Alt,
    Qt.Key.Key_Shift, Qt.Key.Key_AltGr, Qt.Key.Key_CapsLock,
}

_SYMBOLS = {"cmd": "⌘", "ctrl": "⌃", "alt": "⌥", "shift": "⇧", "fn": "fn"}


def pretty_combo(combo: str) -> str:
    """'ctrl+v' → '⌃V'，给界面显示用。"""
    if not combo:
        return ""
    parts = combo.split("+")
    out = "".join(_SYMBOLS.get(p, "") for p in parts[:-1])
    return out + parts[-1].upper()


def describe(spec: dict | None) -> str:
    """眼势绑定 → 一句人话。"""
    if not spec:
        return "未绑定"
    act = spec.get("action")
    if act == "keys":
        return pretty_combo(spec.get("combo", "")) or "按键"
    return ACTION_NAMES.get(act, str(act or "未绑定"))


def combo_from_event(event: QKeyEvent) -> tuple[str, str] | None:
    """QKeyEvent → (配置用的 combo 字符串, 给人看的符号串)。

    返回 None 表示这次按键不能作为热键（只按了修饰键，或主键不支持）。
    """
    key = event.key()
    if key in _MOD_ONLY:
        return None

    m = event.modifiers()
    mods: list[str] = []
    # macOS 上 Qt 交换了 Ctrl/Cmd —— 这里换回物理键
    if m & Qt.KeyboardModifier.MetaModifier:
        mods.append("ctrl")
    if m & Qt.KeyboardModifier.ControlModifier:
        mods.append("cmd")
    if m & Qt.KeyboardModifier.AltModifier:
        mods.append("alt")
    if m & Qt.KeyboardModifier.ShiftModifier:
        mods.append("shift")

    if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
        base = chr(key).lower()
    elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
        base = chr(key)
    else:
        base = _QT_KEYS.get(key, "")

    if not base or base not in KEYCODES:
        return None

    for mod in mods:
        if mod not in MODIFIERS:
            return None

    combo = "+".join(mods + [base])
    pretty = "".join(_SYMBOLS.get(x, x) for x in mods) + base.upper()
    return combo, pretty


class Catcher(QFrame):
    """一块能捕获键盘的区域。"""

    captured = pyqtSignal(str, str)      # combo, pretty

    def __init__(self, placeholder: str):
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(64)
        self.setStyleSheet(f"""
            QFrame {{ background: #111110; border: 1px dashed #4a4a46;
                      border-radius: 8px; }}
        """)
        lay = QVBoxLayout(self)
        self.lbl = QLabel(placeholder)
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont()
        f.setPointSize(17)
        f.setWeight(QFont.Weight.DemiBold)
        self.lbl.setFont(f)
        self.lbl.setStyleSheet(f"color: {MUTED}; border: none;")
        lay.addWidget(self.lbl)
        self.placeholder = placeholder

    def keyPressEvent(self, event: QKeyEvent) -> None:
        got = combo_from_event(event)
        if got is None:
            # 只按了修饰键 → 实时显示已按下的部分，给用户反馈
            m = event.modifiers()
            parts = []
            if m & Qt.KeyboardModifier.MetaModifier:
                parts.append("⌃")
            if m & Qt.KeyboardModifier.ControlModifier:
                parts.append("⌘")
            if m & Qt.KeyboardModifier.AltModifier:
                parts.append("⌥")
            if m & Qt.KeyboardModifier.ShiftModifier:
                parts.append("⇧")
            if parts:
                self.lbl.setText("".join(parts) + " …")
                self.lbl.setStyleSheet(f"color: {WARNING}; border: none;")
            else:
                self.lbl.setText("这个键不支持，换一个")
                self.lbl.setStyleSheet(f"color: {CRITICAL}; border: none;")
            return
        combo, pretty = got
        self.lbl.setText(pretty)
        self.lbl.setStyleSheet(f"color: {GOOD}; border: none;")
        self.setStyleSheet(f"""
            QFrame {{ background: #111110; border: 1px solid {GOOD};
                      border-radius: 8px; }}
        """)
        self.captured.emit(combo, pretty)

    def reset(self) -> None:
        self.lbl.setText(self.placeholder)
        self.lbl.setStyleSheet(f"color: {MUTED}; border: none;")
        self.setStyleSheet(f"""
            QFrame {{ background: #111110; border: 1px dashed #4a4a46;
                      border-radius: 8px; }}
        """)


class BindDialog(QDialog):
    """两步绑定：先做眼势，再按热键。"""

    def __init__(self, cfg, bus, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.bus = bus
        self.gesture: str | None = None
        self.combo: str | None = None
        self.pretty: str = ""

        self.setWindowTitle("绑定眼势")
        self.setMinimumWidth(430)
        self.setStyleSheet("background: #0d0d0d;")

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # ---- 第一步
        root.addWidget(self._step_label("第一步　做一个眼势"))
        self.g_box = QFrame()
        self.g_box.setMinimumHeight(72)
        self.g_box.setStyleSheet(
            "QFrame { background:#111110; border:1px dashed #4a4a46; border-radius:8px; }")
        gl = QVBoxLayout(self.g_box)
        self.g_lbl = QLabel("等你眨眼…")
        self.g_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gf = QFont()
        gf.setPointSize(17)
        gf.setWeight(QFont.Weight.DemiBold)
        self.g_lbl.setFont(gf)
        self.g_lbl.setStyleSheet(f"color: {MUTED}; border: none;")
        gl.addWidget(self.g_lbl)
        self.g_hint = QLabel("双眨 / 三眨 / 用力眨一下都可以")
        self.g_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.g_hint.setStyleSheet(f"color: #6a6a66; font-size: 11px; border: none;")
        gl.addWidget(self.g_hint)
        root.addWidget(self.g_box)

        # ---- 第二步
        root.addWidget(self._step_label("第二步　按下要绑定的热键"))
        self.catcher = Catcher("点这里，然后按快捷键")
        self.catcher.captured.connect(self._on_combo)
        root.addWidget(self.catcher)

        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet(f"color: {WARNING}; font-size: 11px;")
        root.addWidget(self.warn)

        # ---- 按钮
        # 除了按键，还得能绑这两个动作 —— 否则用户绑不出「双眨点击」
        # 和「重眨确认 SUPER BRAIN 的建议」，那是产品的核心交互。
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.other = QPushButton("绑成点击")
        self.other.setToolTip("不用按键，这个眼势直接当鼠标左键点击")
        self.other.setStyleSheet(self._btn_css("#2c2c2a", INK))
        self.other.clicked.connect(lambda: self._pick_action("click"))
        btns.addWidget(self.other)

        self.confirm_btn = QPushButton("绑成 BRAIN 确认")
        self.confirm_btn.setToolTip("老鼠举牌问你要不要干活时，用这个眼势确认")
        self.confirm_btn.setStyleSheet(self._btn_css("#2c2c2a", INK))
        self.confirm_btn.clicked.connect(lambda: self._pick_action("brain_confirm"))
        btns.addWidget(self.confirm_btn)

        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setStyleSheet(self._btn_css("#2c2c2a", INK))
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.save = QPushButton("保存")
        self.save.setEnabled(False)
        self.save.setStyleSheet(self._btn_css(GOOD, "white"))
        self.save.clicked.connect(self._on_save)
        btns.addWidget(self.save)
        root.addLayout(btns)

        # 录制期间挂起眼势动作，否则双眨会真的点下去
        self.bus.publish("ui.suppress_actions", on=True)
        self._unsub = self.bus.subscribe("eye.gesture", self._on_gesture)
        self._unsub_blink = self.bus.subscribe("bci.blink", self._on_blink)

        # 眼势关着的话不会有事件，得提前说
        if not self.cfg.get("eyes.enabled", True):
            self.warn.setText("SHARP EYES 当前是关闭的 —— 请先在面板上打开，否则收不到眼势。")

        self._blinks = 0
        QTimer.singleShot(60, self.catcher.setFocus)

    # ---------- 内部 ----------

    def _step_label(self, text: str) -> QLabel:
        lab = QLabel(text)
        f = QFont()
        f.setPointSize(11)
        f.setWeight(QFont.Weight.DemiBold)
        lab.setFont(f)
        lab.setStyleSheet(f"color: {BLUE}; letter-spacing: .5px;")
        return lab

    def _btn_css(self, bg: str, fg: str) -> str:
        return f"""
            QPushButton {{ background: {bg}; color: {fg}; border: none;
                           border-radius: 6px; padding: 8px 16px; font-size: 12px; }}
            QPushButton:disabled {{ background: #232322; color: #5a5a56; }}
        """

    def _on_blink(self, evt: dict) -> None:
        self._blinks += 1
        if not self.gesture:
            self.g_hint.setText(f"已收到 {self._blinks} 次眨眼，继续…")

    def _on_gesture(self, evt: dict) -> None:
        g = evt.get("gesture")
        if not g:
            return
        self.gesture = g
        self.g_lbl.setText(GESTURE_NAMES.get(g, g))
        self.g_lbl.setStyleSheet(f"color: {GOOD}; border: none;")
        self.g_box.setStyleSheet(
            f"QFrame {{ background:#111110; border:1px solid {GOOD}; border-radius:8px; }}")

        cur = self.cfg.get(f"eyes.gestures.{g}") or {}
        act = cur.get("action")
        if act and act != "none":
            desc = ACTION_NAMES.get(act, act)
            if act == "keys" and cur.get("combo"):
                desc += f"（{cur['combo']}）"
            self.g_hint.setText(f"这个眼势现在绑的是：{desc} —— 保存会覆盖它")
        else:
            self.g_hint.setText("这个眼势还没绑东西")
        self._refresh_save()
        self.catcher.setFocus()

    def _on_combo(self, combo: str, pretty: str) -> None:
        self.combo = combo
        self.pretty = pretty
        self._refresh_save()

    def _pick_action(self, action: str) -> None:
        """绑成非按键动作（点击等）。"""
        if not self.gesture:
            self.warn.setText("先做一个眼势。")
            return
        self._write(action, None)

    def _refresh_save(self) -> None:
        self.save.setEnabled(bool(self.gesture and self.combo))

    def _on_save(self) -> None:
        if not (self.gesture and self.combo):
            return
        self._write("keys", self.combo)

    def _write(self, action: str, combo: str | None) -> None:
        spec: dict = {"action": action}
        if combo:
            spec["combo"] = combo
        self.cfg.set(f"eyes.gestures.{self.gesture}", spec)
        try:
            self.cfg.save()
        except Exception:
            log.exception("保存眼势绑定失败")
            self.warn.setText("保存失败，看日志。")
            return
        log.info("眼势绑定 %s → %s %s", self.gesture, action, combo or "")
        self.bus.publish("config.changed", key=f"eyes.gestures.{self.gesture}")
        self.accept()

    def done(self, r: int) -> None:
        # 无论确定还是取消，都要恢复动作执行
        try:
            self.bus.publish("ui.suppress_actions", on=False)
            if callable(self._unsub):
                self._unsub()
            if callable(self._unsub_blink):
                self._unsub_blink()
        except Exception:
            log.debug("解除订阅失败", exc_info=True)
        super().done(r)


class CalibrateDialog(QDialog):
    """校准进度：整个流程由 Calibrator 驱动，这里只显示它的播报。"""

    def __init__(self, cfg, bus, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.bus = bus
        self.setWindowTitle("校准眼势")
        self.setMinimumWidth(420)
        self.setStyleSheet("background: #0d0d0d;")

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        t = QLabel("校准眼势")
        tf = QFont()
        tf.setPointSize(14)
        tf.setWeight(QFont.Weight.Bold)
        t.setFont(tf)
        t.setStyleSheet(f"color: {INK};")
        root.addWidget(t)

        d = QLabel(
            "大约 60 秒，分四步：先自然眨眼，再用力眨，然后练双眨。\n"
            "目的是把「自然眨眼」和「有意眨眼」分开 —— 否则自然眨眼会误触发动作。")
        d.setWordWrap(True)
        d.setStyleSheet(f"color: {MUTED}; font-size: 11.5px;")
        root.addWidget(d)

        self.stage = QFrame()
        self.stage.setMinimumHeight(84)
        self.stage.setStyleSheet(
            "QFrame { background:#111110; border:1px solid rgba(255,255,255,0.10);"
            " border-radius:8px; }")
        sl = QVBoxLayout(self.stage)
        self.msg = QLabel("准备开始…")
        self.msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mf = QFont()
        mf.setPointSize(15)
        mf.setWeight(QFont.Weight.DemiBold)
        self.msg.setFont(mf)
        self.msg.setStyleSheet(f"color: {INK}; border: none;")
        self.msg.setWordWrap(True)
        sl.addWidget(self.msg)
        self.count = QLabel("")
        self.count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count.setStyleSheet(f"color: {MUTED}; font-size: 11px; border: none;")
        sl.addWidget(self.count)
        root.addWidget(self.stage)

        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setStyleSheet(f"color: {GOOD}; font-size: 11.5px;")
        root.addWidget(self.result)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.close_btn = QPushButton("取消")
        self.close_btn.setStyleSheet(f"""
            QPushButton {{ background:#2c2c2a; color:{INK}; border:none;
                           border-radius:6px; padding:8px 16px; font-size:12px; }}
        """)
        self.close_btn.clicked.connect(self.reject)
        btns.addWidget(self.close_btn)
        self.start_btn = QPushButton("开始校准")
        self.start_btn.setStyleSheet(f"""
            QPushButton {{ background:{BLUE}; color:white; border:none;
                           border-radius:6px; padding:8px 16px; font-size:12px; }}
            QPushButton:disabled {{ background:#232322; color:#5a5a56; }}
        """)
        self.start_btn.clicked.connect(self._start)
        btns.addWidget(self.start_btn)
        root.addLayout(btns)

        self._blinks = 0
        self._unsubs = [
            self.bus.subscribe("mouse.state", self._on_state),
            self.bus.subscribe("bci.blink", self._on_blink),
            self.bus.subscribe("eye.calibrated", self._on_done),
        ]

    def _start(self) -> None:
        self._blinks = 0
        self.start_btn.setEnabled(False)
        self.msg.setText("开始了，看提示做…")
        self.bus.publish("ui.calibrate")

    def _on_blink(self, evt: dict) -> None:
        self._blinks += 1
        self.count.setText(f"已记录 {self._blinks} 次眨眼")

    def _on_state(self, evt: dict) -> None:
        label = evt.get("label")
        if label:                      # Calibrator 用 mouse.state 的 label 播报阶段
            self.msg.setText(str(label))

    def _on_done(self, evt: dict) -> None:
        th = evt.get("thresholds") or {}
        hard = th.get("hard_threshold")
        gap = th.get("double_gap_ms")
        parts = ["校准完成，阈值已保存。"]
        if hard is not None:
            parts.append(f"重眨阈值 {hard:.2f}")
        if gap:
            parts.append(f"双眨间隔 {gap[0]}–{gap[1]} ms")
        self.result.setText("　·　".join(parts))
        self.msg.setText("完成 ✅")
        self.start_btn.setEnabled(True)
        self.start_btn.setText("再校准一次")
        self.close_btn.setText("关闭")

    def done(self, r: int) -> None:
        for u in self._unsubs:
            try:
                if callable(u):
                    u()
            except Exception:
                pass
        super().done(r)
