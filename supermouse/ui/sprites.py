"""用 QPainter 直接画老鼠。

美术的 sprite sheet 还没到，先用几何图形画一只能表达全部状态的老鼠——
关键是眼睛能眨、耳朵能动、表情能变，Demo 的"它在读我的眼睛"才立得住。
后续换成 PNG sprite 只需要替换 draw_mouse()，状态机不用改。

坐标系：以老鼠鼻尖为原点 (0,0)，向右为 +x，向下为 +y。
画完之后调用方按 hotspot 平移，让鼻尖对准光标位置。
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen

# 配色
FUR = QColor(140, 140, 150)
FUR_DARK = QColor(105, 105, 118)
BELLY = QColor(228, 220, 226)
EAR_IN = QColor(238, 176, 190)
NOSE = QColor(236, 130, 150)
EYE = QColor(28, 28, 34)
EYE_SHINE = QColor(255, 255, 255)
TAIL = QColor(198, 160, 168)
HAT = QColor(252, 196, 60)
HAT_DARK = QColor(214, 156, 30)

# 每个状态的持续帧行为：(眼睛开合 0-1, 耳朵抬起, 身体倾斜)
_CLOSED = 0.06


def draw_mouse(p: QPainter, anim: str, frame: int, facing: int = 1,
               scale: float = 1.0, icon: str | None = None) -> None:
    """在当前 painter 上画老鼠。frame 用于呼吸/奔跑等周期动画。"""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.scale(scale * facing, scale)

    t = frame / 12.0                          # 秒
    eye_open = 1.0
    ear_lift = 0.0
    bob = math.sin(t * 3.0) * 0.6             # 呼吸
    lean = 0.0
    hidden = 0.0                              # 钻洞进度 0-1

    if anim == "walk":
        bob = math.sin(t * 14.0) * 1.6
        lean = 4.0
    elif anim == "blink_mirror":
        eye_open = _CLOSED
    elif anim == "gesture":
        eye_open = 1.0
        ear_lift = 2.0
    elif anim == "thinking":
        ear_lift = -1.0
        eye_open = 0.55 + 0.2 * math.sin(t * 4)
    elif anim == "suggest":
        ear_lift = 2.5
        bob = math.sin(t * 6.0) * 1.2
    elif anim == "working":
        bob = abs(math.sin(t * 10.0)) * 2.2
    elif anim == "happy":
        eye_open = 0.35
        bob = abs(math.sin(t * 8.0)) * 3.0
    elif anim == "alert":
        ear_lift = 4.0
        eye_open = 1.0
        bob = 0.0
    elif anim == "hide":
        hidden = min(1.0, (frame % 24) / 12.0)
    elif anim == "peek":
        hidden = 0.55
        ear_lift = 2.0
    elif anim == "sleepy":
        eye_open = _CLOSED
        bob = math.sin(t * 1.5) * 1.0
    elif anim == "click":
        lean = 6.0

    if hidden > 0:
        _draw_hole(p, hidden)
        if hidden >= 0.95:
            p.restore()
            return
        p.translate(0, 14 * hidden)

    p.translate(0, bob)

    # 尾巴
    tail = QPainterPath()
    tail.moveTo(22, 4)
    wag = math.sin(t * (10.0 if anim in ("walk", "happy") else 2.2)) * 5
    tail.cubicTo(34, 2 + wag, 40, -8 + wag, 33, -14 + wag)
    p.setPen(QPen(TAIL, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(tail)

    # 身体
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(FUR))
    body = QRectF(6, -9 + lean * 0.1, 20, 17)
    p.drawEllipse(body)
    p.setBrush(QBrush(BELLY))
    p.drawEllipse(QRectF(9, -2, 14, 9))

    # 后腿
    p.setBrush(QBrush(FUR_DARK))
    leg_phase = math.sin(t * 14.0) * 2 if anim == "walk" else 0
    p.drawEllipse(QRectF(15 + leg_phase, 4, 6, 5))

    # 耳朵
    ear_y = -12 - ear_lift
    p.setBrush(QBrush(FUR))
    p.drawEllipse(QRectF(6.5, ear_y, 9, 9))
    p.setBrush(QBrush(EAR_IN))
    p.drawEllipse(QRectF(8.5, ear_y + 2, 5, 5))

    # 头
    p.setBrush(QBrush(FUR))
    head = QPainterPath()
    head.moveTo(0, 0)                          # 鼻尖 = 原点
    head.cubicTo(3, -8, 8, -12, 14, -10)
    head.cubicTo(19, -8, 19, 2, 13, 4)
    head.cubicTo(7, 6, 2, 3, 0, 0)
    p.drawPath(head)

    # 鼻子
    p.setBrush(QBrush(NOSE))
    p.drawEllipse(QRectF(-1.4, -2.2, 3.4, 3.0))

    # 胡须
    p.setPen(QPen(QColor(255, 255, 255, 170), 0.9))
    for dy in (-1.5, 0.2, 1.8):
        p.drawLine(QPointF(0.5, -0.6), QPointF(-6.5, dy - 2))
    p.setPen(Qt.PenStyle.NoPen)

    # 眼睛
    eye_h = max(0.8, 5.0 * eye_open)
    p.setBrush(QBrush(EYE))
    eye_rect = QRectF(6.5, -6.5 - eye_h / 2 + 2.5, 4.2, eye_h)
    p.drawEllipse(eye_rect)
    if eye_open > 0.5:
        p.setBrush(QBrush(EYE_SHINE))
        p.drawEllipse(QRectF(eye_rect.x() + 2.2, eye_rect.y() + 0.8, 1.4, 1.4))

    if anim == "happy":                        # 腮红
        p.setBrush(QBrush(QColor(245, 150, 165, 130)))
        p.drawEllipse(QRectF(4, -0.5, 4, 2.6))

    if anim == "working":                       # 安全帽
        p.setBrush(QBrush(HAT))
        p.drawChord(QRectF(3, -17, 15, 14), 0, 180 * 16)
        p.setBrush(QBrush(HAT_DARK))
        p.drawRect(QRectF(1.5, -10.5, 18, 2.2))

    p.restore()


def _draw_hole(p: QPainter, progress: float) -> None:
    """老鼠钻的洞。progress 0→1 表示越钻越深。"""
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(20, 18, 26, int(220 * min(progress * 2, 1.0)))))
    w = 30 + 6 * progress
    p.drawEllipse(QRectF(16 - w / 2, 2, w, w * 0.42))
    p.restore()


def draw_sign(p: QPainter, text: str, x: float, y: float,
              max_width: float = 260.0) -> QRectF:
    """老鼠举的小牌子。返回牌子矩形（供命中测试）。"""
    from PyQt6.QtGui import QFont, QFontMetrics

    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    font = QFont()
    font.setPointSizeF(11.0)
    font.setBold(True)
    p.setFont(font)
    fm = QFontMetrics(font)

    pad_x, pad_y = 10.0, 6.0
    tw = min(fm.horizontalAdvance(text), max_width - 2 * pad_x)
    rect = QRectF(x, y, tw + 2 * pad_x, fm.height() + 2 * pad_y)

    # 牌子杆
    p.setPen(QPen(QColor(150, 110, 70), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(rect.center().x(), rect.bottom()),
               QPointF(rect.center().x() - 2, rect.bottom() + 10))

    # 牌面
    p.setPen(QPen(QColor(255, 255, 255, 220), 1.4))
    p.setBrush(QBrush(QColor(38, 36, 48, 238)))
    p.drawRoundedRect(rect, 7, 7)
    p.setPen(QPen(QColor(248, 248, 252)))
    p.drawText(rect.adjusted(pad_x, pad_y, -pad_x, -pad_y),
               int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
               fm.elidedText(text, Qt.TextElideMode.ElideRight, int(max_width - 2 * pad_x)))
    p.restore()
    return rect


def draw_icon_bubble(p: QPainter, icon: str, x: float, y: float) -> None:
    """头顶浮出的小图标（眼势反馈）。"""
    from PyQt6.QtGui import QFont

    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(38, 36, 48, 232)))
    p.drawRoundedRect(QRectF(x, y, 30, 24), 6, 6)
    f = QFont()
    f.setPointSizeF(12.0)
    p.setFont(f)
    p.setPen(QPen(QColor(255, 255, 255)))
    p.drawText(QRectF(x, y, 30, 24), int(Qt.AlignmentFlag.AlignCenter), icon)
    p.restore()
