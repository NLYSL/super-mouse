"""控制面板：每个模式一个开关，实时状态一目了然。

为什么开关能立刻生效：各模式都是**每个事件实时读配置**的
（`gestures.py` 的 `enabled` 是 property，`hover.py` 同理），
所以这里 `cfg.set()` 之后无需重启、无需通知，下一个事件就按新值走。

三条设计决定：
  · 老鼠光标是一切的核心，**不给开关**（关了整个产品就没了）
  · SENSE 雷达硬件尚未接通（见 docs/06-雷达问题交接.md），开关**锁住不让开** ——
    宁可明确说"还不能用"，也不要给一个拨了没反应的开关
  · 面板要反映真实事件流，不做静态 UI
"""

from __future__ import annotations

import logging
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

log = logging.getLogger(__name__)

# 配色与 tools/dashboard.html 一致（dataviz 暗色表面）
BG       = "#0d0d0d"
CARD     = "#1a1a19"
INK      = "#ffffff"
MUTED    = "#898781"
BORDER   = "rgba(255,255,255,0.10)"
GOOD     = "#0ca30c"
WARNING  = "#fab219"
CRITICAL = "#d03b3b"
BLUE     = "#3987e5"


class ToggleRow(QFrame):
    """一个模式一行：名字 + 说明 + 开关 + 实时状态。"""

    def __init__(self, key: str, title: str, subtitle: str, cfg, bus,
                 locked_reason: str | None = None):
        super().__init__()
        self.key = key
        self.cfg = cfg
        self.bus = bus
        self.locked_reason = locked_reason

        self.setObjectName("row")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(13, 10, 13, 10)
        outer.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(8)

        name = QLabel(title)
        f = QFont()
        f.setPointSize(13)
        f.setWeight(QFont.Weight.DemiBold)
        name.setFont(f)
        name.setStyleSheet(f"color: {INK};")
        top.addWidget(name)
        top.addStretch(1)

        self.state_lbl = QLabel("")
        self.state_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        top.addWidget(self.state_lbl)

        self.box = QCheckBox()
        self.box.setFixedSize(48, 24)
        self.box.setCursor(Qt.CursorShape.PointingHandCursor)
        self.box.setStyleSheet(f"""
            QCheckBox {{ spacing: 0; }}
            QCheckBox::indicator {{
                width: 44px; height: 22px; border-radius: 11px;
                background: #2c2c2a; border: 1px solid {BORDER};
            }}
            QCheckBox::indicator:checked {{
                background: {GOOD}; border: 1px solid {GOOD};
            }}
            QCheckBox::indicator:disabled {{
                background: #232322; border: 1px dashed #4a4a46;
            }}
        """)
        top.addWidget(self.box)
        outer.addLayout(top)

        sub = QLabel(locked_reason or subtitle)
        sub.setWordWrap(True)
        sub.setStyleSheet(
            f"color: {WARNING if locked_reason else MUTED}; font-size: 11px;")
        outer.addWidget(sub)

        on = False if locked_reason else bool(cfg.get(f"{key}.enabled", True))
        self.box.setChecked(on)
        if locked_reason:
            self.box.setEnabled(False)
            self.box.setToolTip(locked_reason)
        else:
            self.box.stateChanged.connect(self._on_toggle)
        self._paint_edge(on)

    def _paint_edge(self, on: bool) -> None:
        color = GOOD if on else ("#4a4a46" if self.box.isEnabled() else "#3a2f1c")
        self.setStyleSheet(f"""
            QFrame#row {{
                background: {CARD};
                border: 1px solid {BORDER};
                border-left: 3px solid {color};
                border-radius: 8px;
            }}
        """)

    def _on_toggle(self, _state: int) -> None:
        on = self.box.isChecked()
        self.cfg.set(f"{self.key}.enabled", on)
        try:
            self.cfg.save()
        except Exception:
            log.exception("保存配置失败")
        self._paint_edge(on)
        self.bus.publish("config.changed", key=f"{self.key}.enabled", value=on)
        log.info("%s.enabled → %s", self.key, on)

    def set_status(self, text: str, color: str = MUTED) -> None:
        self.state_lbl.setText(text)
        self.state_lbl.setStyleSheet(f"color: {color}; font-size: 11px;")


class ControlPanel(QWidget):
    # 跨线程信号：全局热键在 pynput 线程触发，经信号安全转到 Qt 主线程
    bind_requested = pyqtSignal()
    def __init__(self, cfg, bus, app=None):
        super().__init__()
        self.cfg = cfg
        self.bus = bus
        self.app = app

        self.setWindowTitle("Super Mouse")
        self.setMinimumWidth(440)
        self.setStyleSheet(f"background: {BG};")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # ---- 标题栏
        head = QHBoxLayout()
        t = QLabel("SUPER MOUSE")
        tf = QFont()
        tf.setPointSize(15)
        tf.setWeight(QFont.Weight.Bold)
        t.setFont(tf)
        t.setStyleSheet(f"color: {INK}; letter-spacing: 1px;")
        head.addWidget(t)
        head.addStretch(1)
        self.conn = QLabel("—")
        self.conn.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        head.addWidget(self.conn)
        root.addLayout(head)

        core = QLabel("🐭  老鼠光标常开 —— 它是这一切的核心")
        core.setStyleSheet(f"color: {BLUE}; font-size: 11.5px;")
        root.addWidget(core)

        # ---- 模式开关
        self.rows: dict[str, ToggleRow] = {}

        self.rows["eyes"] = ToggleRow(
            "eyes", "👀  SHARP EYES",
            "眨眼识别：双眨点击 · 三眨切换应用 · 重眨确认",
            cfg, bus)
        self.rows["brain"] = ToggleRow(
            "brain", "🧠  SUPER BRAIN",
            "光标停在 Dock 图标上 0.7 秒，老鼠举牌问要不要替你干活",
            cfg, bus)
        self.rows["sense"] = ToggleRow(
            "sense", "📡  STRONG SENSE",
            "雷达盯着探测区：目标数达到 2（你 + 来人）→ 切回工作页面",
            cfg, bus)

        root.addWidget(self.rows["eyes"])

        # 眼势的校准与绑定：紧跟在 SHARP EYES 下面
        eyes_tools = QFrame()
        eyes_tools.setObjectName("tools")
        eyes_tools.setStyleSheet(f"""
            QFrame#tools {{ background: #141413; border: 1px solid {BORDER};
                            border-radius: 8px; }}
        """)
        et = QVBoxLayout(eyes_tools)
        et.setContentsMargins(13, 9, 13, 10)
        et.setSpacing(7)

        self.bind_list = QLabel("")
        self.bind_list.setWordWrap(True)
        self.bind_list.setStyleSheet("color: #a8a79e; font-size: 11.5px;")
        et.addWidget(self.bind_list)

        row = QHBoxLayout()
        row.setSpacing(8)
        cal_btn = QPushButton("校准眼势")
        cal_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cal_btn.setStyleSheet(f"""
            QPushButton {{ background: {BLUE}; color: white; border: none;
                           border-radius: 6px; padding: 7px 14px; font-size: 12px; }}
            QPushButton:hover {{ background: #2f74c8; }}
        """)
        cal_btn.clicked.connect(self._open_calibrate)
        row.addWidget(cal_btn)

        bind_btn = QPushButton("绑定热键")
        bind_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        bind_btn.setStyleSheet(f"""
            QPushButton {{ background: #2c2c2a; color: {INK}; border: none;
                           border-radius: 6px; padding: 7px 14px; font-size: 12px; }}
            QPushButton:hover {{ background: #3a3a37; }}
        """)
        bind_btn.clicked.connect(self._open_bind)
        row.addWidget(bind_btn)
        row.addStretch(1)
        et.addLayout(row)
        root.addWidget(eyes_tools)

        root.addWidget(self.rows["brain"])
        brain_btn = QPushButton("Brain 任务设置 / 测试")
        brain_btn.setStyleSheet(f"color: {BLUE}; padding: 8px;")
        brain_btn.clicked.connect(self._open_brain)
        root.addWidget(brain_btn)
        root.addWidget(self.rows["sense"])

        # STRONG SENSE 工具：绑定工作页面 + 实时目标数。
        # 全屏 App 住在自己的 Space 上，open -b 激活会直接切回那个 Space，
        # 所以"绑定全屏工作页" = 绑定那个 App 本身。
        sense_tools = QFrame()
        sense_tools.setObjectName("tools")
        sense_tools.setStyleSheet(f"""
            QFrame#tools {{ background: #141413; border: 1px solid {BORDER};
                            border-radius: 8px; }}
        """)
        st = QVBoxLayout(sense_tools)
        st.setContentsMargins(13, 9, 13, 10)
        st.setSpacing(7)

        self.work_bind_lbl = QLabel("")
        self.work_bind_lbl.setWordWrap(True)
        self.work_bind_lbl.setStyleSheet("color: #a8a79e; font-size: 11.5px;")
        st.addWidget(self.work_bind_lbl)

        srow = QHBoxLayout()
        srow.setSpacing(8)
        self.bind_work_btn = QPushButton("把当前 App 设为工作页面")
        self.bind_work_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bind_work_btn.setToolTip("先切到你要用的 App（全屏也可以），再点这个按钮。"
                                      "\n有人走近时，屏幕会切回它。")
        self.bind_work_btn.setStyleSheet(f"""
            QPushButton {{ background: {BLUE}; color: white; border: none;
                           border-radius: 6px; padding: 7px 14px; font-size: 12px; }}
            QPushButton:hover {{ background: #2f74c8; }}
        """)
        self.bind_work_btn.clicked.connect(self._bind_work_page)
        srow.addWidget(self.bind_work_btn)

        hk_btn = QPushButton("换绑定热键")
        hk_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        hk_btn.setToolTip("当前键盘可能没有 F13+ —— 在这里按一个自己的组合键")
        hk_btn.setStyleSheet(f"""
            QPushButton {{ background: #2c2c2a; color: {INK}; border: none;
                           border-radius: 6px; padding: 7px 14px; font-size: 12px; }}
            QPushButton:hover {{ background: #3a3a37; }}
        """)
        hk_btn.clicked.connect(self._change_bind_hotkey)
        srow.addWidget(hk_btn)
        srow.addStretch(1)
        st.addLayout(srow)
        root.addWidget(sense_tools)

        # ---- 急停（不是配置项，直接掐掉动作执行）
        stop_row = QFrame()
        stop_row.setObjectName("row")
        stop_row.setStyleSheet(f"""
            QFrame#row {{ background: {CARD}; border: 1px solid {BORDER};
                          border-left: 3px solid {CRITICAL}; border-radius: 8px; }}
        """)
        sl = QHBoxLayout(stop_row)
        sl.setContentsMargins(13, 10, 13, 10)
        lab = QVBoxLayout()
        lab.setSpacing(2)
        n = QLabel("🛑  急停")
        nf = QFont()
        nf.setPointSize(13)
        nf.setWeight(QFont.Weight.DemiBold)
        n.setFont(nf)
        n.setStyleSheet(f"color: {INK};")
        lab.addWidget(n)
        d = QLabel("立刻停止所有自动操作（也可连按两次 Esc）")
        d.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        lab.addWidget(d)
        sl.addLayout(lab)
        sl.addStretch(1)
        self.stop_btn = QPushButton("全部停止")
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.setStyleSheet(f"""
            QPushButton {{ background: {CRITICAL}; color: white; border: none;
                           border-radius: 6px; padding: 7px 14px; font-size: 12px; }}
            QPushButton:hover {{ background: #b83232; }}
        """)
        self.stop_btn.clicked.connect(self._on_stop)
        sl.addWidget(self.stop_btn)
        root.addWidget(stop_row)

        # ---- 底部
        self.stats = QLabel("等待事件…")
        self.stats.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        root.addWidget(self.stats)

        hint = QLabel(
            "没戴脑机时的模拟热键  F13 双眨 · F14 三眨 · F15 重眨 · F18 单眨\n"
            "Esc Esc 急停 · 关闭本窗口不会退出程序")
        hint.setStyleSheet("color: #6a6a66; font-size: 10.5px;")
        root.addWidget(hint)

        # ---- 事件订阅：面板必须反映真实状态
        self._n_blink = 0
        self._n_gesture = 0
        self._last_gesture = ""
        self._last_gesture_t = 0.0
        self._bci_ok = False
        self._quality: int | None = None
        self._n_events = 0
        self._brain_note = ""
        self._brain_note_t = 0.0
        self._radar_targets = -1          # -1 = 从未收到雷达帧
        self._radar_t = 0.0
        self._intruder_until = 0.0

        bus.subscribe("*", self._on_any)

        self._refresh_binds()          # 打开面板就能看到当前绑了什么
        self._refresh_work_bind()

        # 全局热键绑定工作页面：全屏 App 独占 Space，面板够不着，
        # 所以"绑定当前 App"必须能在全屏 App 里完成 —— 按键即绑。
        self.bind_requested.connect(self._bind_work_page)
        self._install_bind_hotkey()
        self._refresh_work_bind()      # 热键名装好后刷新一次标签

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(400)

    # ---------- 校准与绑定 ----------

    def _open_calibrate(self) -> None:
        from .bind import CalibrateDialog
        dlg = CalibrateDialog(self.cfg, self.bus, self)
        dlg.exec()
        self._refresh_binds()

    def _open_brain(self):
        from .brain_settings import BrainSettings
        if not getattr(self, "_brain_dialog", None):
            self._brain_dialog = BrainSettings(self.cfg, self.bus, self)
        self._brain_dialog.show()
        self._brain_dialog.raise_()

    def _open_bind(self) -> None:
        from .bind import BindDialog
        dlg = BindDialog(self.cfg, self.bus, self)
        dlg.exec()
        self._refresh_binds()

    def _refresh_binds(self) -> None:
        """把当前眼势映射显示出来 —— 用户得能看到自己绑了什么。"""
        from .bind import GESTURE_NAMES, describe
        specs = self.cfg.section("eyes.gestures") or {}
        parts = []
        for key in ("double_blink", "triple_blink", "hard_blink", "long_blink"):
            spec = specs.get(key)
            if not spec or spec.get("action") in (None, "none"):
                continue
            parts.append(f"{GESTURE_NAMES.get(key, key)} → {describe(spec)}")
        # 判据只能看 abs_threshold / intent_peak —— 它们默认是 null，只由校准写回。
        # hard_threshold 有默认值 0.80，拿它判断会永远显示"已校准"。
        calibrated = (self.cfg.get("eyes.calibration.abs_threshold") is not None
                      and self.cfg.get("eyes.calibration.intent_peak") is not None)
        tail = "　·　已校准" if calibrated else "　·　未校准（建议先校准）"
        self.bind_list.setText(("　|　".join(parts) if parts else "还没有绑定任何眼势") + tail)

    # ---------- 事件 ----------

    def _on_stop(self) -> None:
        self.bus.publish("sys.stop_all")
        self.stop_btn.setText("已停止")
        QTimer.singleShot(1200, lambda: self.stop_btn.setText("全部停止"))

    def _on_any(self, evt: dict) -> None:
        t = evt.get("type", "")
        self._n_events += 1
        if t == "bci.connected":
            self._bci_ok = True
        elif t == "bci.disconnected":
            self._bci_ok = False
        elif t == "bci.quality":
            self._quality = evt.get("poor_signal")
        elif t == "bci.blink":
            self._n_blink += 1
        elif t == "eye.gesture":
            self._n_gesture += 1
            self._last_gesture = evt.get("gesture", "")
            self._last_gesture_t = time.time()
        elif t == "brain.hover":
            self._brain_note = f"识别 {evt.get('app') or evt.get('title') or ''}"[:28]
            self._brain_note_t = time.time()
        elif t == "brain.suggest":
            self._brain_note = f"{evt.get('source', '建议')} · 等确认"
            self._brain_note_t = time.time()
        elif t == "brain.action":
            st = evt.get("status")
            self._brain_note = {"running": "执行中", "done": "完成", "partial": "部分完成",
                                "error": "出错"}.get(st, str(st))
            self._brain_note_t = time.time()
        elif t == "radar.frame":
            tg = evt.get("targets")
            if tg is not None:
                self._radar_targets = len(tg)
                self._radar_t = time.time()
        elif t == "sense.intruder":
            self._intruder_until = time.time() + 3.0

    def _refresh(self) -> None:
        if self._bci_ok:
            q = self._quality
            if q == 0:
                self.conn.setText("脑机已连接 · 信号良好")
                self.conn.setStyleSheet(f"color: {GOOD}; font-size: 11px;")
            elif q is not None and q > 50:
                self.conn.setText(f"脑机已连接 · 信号差 ({q})，请调整电极")
                self.conn.setStyleSheet(f"color: {CRITICAL}; font-size: 11px;")
            else:
                self.conn.setText(f"脑机已连接 · 质量 {q if q is not None else '?'}")
                self.conn.setStyleSheet(f"color: {WARNING}; font-size: 11px;")
        else:
            self.conn.setText("脑机未连接 · 用热键模拟")
            self.conn.setStyleSheet(f"color: {MUTED}; font-size: 11px;")

        age = time.time() - self._last_gesture_t
        if self._last_gesture and age < 2.5:
            names = {"double_blink": "双眨 → 点击", "triple_blink": "三眨 → 切换",
                     "hard_blink": "重眨 → 确认", "long_blink": "长眨"}
            self.rows["eyes"].set_status(
                names.get(self._last_gesture, self._last_gesture), GOOD)
        else:
            self.rows["eyes"].set_status(f"{self._n_blink} 次眨眼")

        if self._brain_note and time.time() - self._brain_note_t < 4.0:
            self.rows["brain"].set_status(self._brain_note, BLUE)
        else:
            self.rows["brain"].set_status("")

        # STRONG SENSE：实时目标数 —— 二人实测时直接看这里有没有变成 2
        if time.time() - self._radar_t > 2.0 or self._radar_targets < 0:
            self.rows["sense"].set_status("无雷达信号", MUTED)
        elif time.time() < self._intruder_until:
            self.rows["sense"].set_status("来人！切屏", CRITICAL)
        elif self._radar_targets >= 2:
            self.rows["sense"].set_status(f"目标 {self._radar_targets}", CRITICAL)
        else:
            self.rows["sense"].set_status(f"目标 {self._radar_targets}（你）", GOOD)

        self.stats.setText(
            f"事件 {self._n_events} · 眨眼 {self._n_blink} · 眼势 {self._n_gesture}")

    # ---------- 工作页面绑定 ----------

    @staticmethod
    def _hotkey_spec(combo: str) -> str | None:
        """配置里的组合（如 "ctrl+alt+b" / "f8"）→ pynput 热键串。

        pynput 语法：修饰键和功能键要 <尖括号>，单字符裸写。
        返回 None 表示组合里有不认识的键。
        """
        from pynput import keyboard
        parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
        if not parts:
            return None
        spec = []
        for p in parts:
            if len(p) == 1 and p.isalnum():
                spec.append(p)                       # 主键是单字符，如 b
            elif hasattr(keyboard.Key, p):
                spec.append(f"<{p}>")                # 修饰键 / f8 / space …
            else:
                return None
        return "+".join(spec)

    def _install_bind_hotkey(self) -> None:
        """注册全局热键（配置 sense.bind_work_hotkey，可用组合键，默认 ⌃⌥B）。

        pynput 回调在自己的线程里跑，emit 信号跨线程自动排队到 Qt 主线程。
        """
        combo = str(self.cfg.get("sense.bind_work_hotkey", "ctrl+alt+b")).lower()
        try:
            from pynput import keyboard
            spec = self._hotkey_spec(combo)
            if not spec:
                log.warning("热键组合 %s 无法解析，工作页面热键未注册", combo)
                self._bind_hotkey_name = None
                return
            self._hotkey_listener = keyboard.GlobalHotKeys(
                {spec: self.bind_requested.emit})
            self._hotkey_listener.daemon = True
            self._hotkey_listener.start()
            self._bind_hotkey_name = combo
            log.info("工作页面热键已注册：%s", combo)
        except Exception as e:
            # 注册不了（无输入监控权限等）面板按钮仍然可用
            self._bind_hotkey_name = None
            log.warning("工作页面热键注册失败（面板按钮仍可用）：%s", e)

    def _restart_bind_hotkey(self) -> None:
        if getattr(self, "_hotkey_listener", None):
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
            self._hotkey_listener = None
        self._install_bind_hotkey()

    def _change_bind_hotkey(self) -> None:
        """弹出捕获框，用户按一个新组合 → 存配置并立即生效。"""
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout
        from .bind import Catcher

        # 捕获期间先停掉全局监听，免得按到旧热键触发一次绑定
        if getattr(self, "_hotkey_listener", None):
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass

        dlg = QDialog(self)
        dlg.setWindowTitle("设置绑定热键")
        dlg.setMinimumWidth(360)
        dlg.setStyleSheet("background: #0d0d0d;")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 14, 16, 14)
        tip = QLabel("按下新的热键组合（推荐 ⌃⌥B 这类三键组合，避免和常用快捷键冲突）")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #a8a79e; font-size: 11.5px;")
        lay.addWidget(tip)
        catcher = Catcher("在这里按下组合键")
        lay.addWidget(catcher)

        def on_capture(combo: str, pretty: str) -> None:
            self.cfg.set("sense.bind_work_hotkey", combo)
            try:
                self.cfg.save()
            except Exception:
                log.exception("保存热键失败")
            self._install_bind_hotkey()
            self._refresh_work_bind()
            dlg.accept()

        catcher.captured.connect(on_capture)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.setStyleSheet(f"""
            QPushButton {{ background: #2c2c2a; color: {INK}; border: none;
                           border-radius: 6px; padding: 7px 14px; font-size: 12px; }}
        """)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        dlg.exec()
        # 无论确认还是取消，恢复全局监听（可能已换成新键）
        self._restart_bind_hotkey()

    def _bind_work_page(self) -> None:
        """把当前最前面的 App 绑成"有人走近时切回"的工作页面。

        全屏 App 住在自己的 Space 上，open -b 激活会直接回到那个 Space，
        所以绑定全屏工作页 = 绑定该 App，不需要额外处理。
        """
        from ..actions import macos
        # frontmost_bundle 返回 pyobjc 的 NSString 代理，不转 str 的话
        # yaml 序列化会崩（实测：RepresenterError）
        bid = str(macos.frontmost_bundle() or "")
        if not bid:
            self.work_bind_lbl.setText("没识别到前台 App，再试一次")
            return

        scene = [dict(s) for s in (self.cfg.get("sense.work_scene") or [])]
        kept = [s for s in scene if s.get("do") != "activate"]
        if not any(s.get("do") == "hide_apps" for s in kept):
            kept.insert(0, {"do": "hide_apps", "apps": "fun_apps"})
        kept.append({"do": "activate", "app": bid})
        if not any(s.get("do") == "mute" for s in kept):
            kept.append({"do": "mute"})
        self.cfg.set("sense.work_scene", kept)
        try:
            self.cfg.save()
        except Exception:
            log.exception("保存工作页面绑定失败")
            self.work_bind_lbl.setText("保存失败，看日志")
            return
        self.bus.publish("config.changed", key="sense.work_scene")
        self._refresh_work_bind()
        self.bind_work_btn.setText("已绑定 ✓")
        QTimer.singleShot(1500, lambda: self.bind_work_btn.setText("把当前 App 设为工作页面"))
        # 热键入口按下时面板多半不可见（全屏 App 独占 Space）——
        # 用老鼠牌子反馈，它浮在所有 Space 和全屏窗口之上。
        name = self._app_display_name(bid)
        self.bus.publish("mouse.state", anim="happy",
                         label=f"工作页面 → {name}", ttl_ms=2500)
        log.info("工作页面已绑定 → %s", bid)

    def _refresh_work_bind(self) -> None:
        """显示当前绑定的工作页面。"""
        from .bind import pretty_combo
        scene = self.cfg.get("sense.work_scene") or []
        bid = next((s.get("app") for s in scene if s.get("do") == "activate"), None)
        hk = getattr(self, "_bind_hotkey_name", None)
        hk_note = f"　按 {pretty_combo(hk)} 绑定当前 App（全屏里也能按）" if hk else ""
        if not bid:
            self.work_bind_lbl.setText(
                f"还没绑定工作页面 —— 切到目标 App 点按钮或按热键{hk_note}")
            return
        name = self._app_display_name(bid)
        self.work_bind_lbl.setText(f"触发时切回：{name}{hk_note}")

    @staticmethod
    def _app_display_name(bundle_id: str) -> str:
        try:
            from AppKit import NSWorkspace
            for app in NSWorkspace.sharedWorkspace().runningApplications():
                if app.bundleIdentifier() == bundle_id:
                    return app.localizedName() or bundle_id
        except Exception:
            pass
        return bundle_id.rsplit(".", 1)[-1]

    # ---------- 窗口 ----------

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show_front()

    def show_front(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        # 关窗口不退程序 —— 后台功能要继续跑
        event.ignore()
        self.hide()
