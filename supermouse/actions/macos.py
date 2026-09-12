"""macOS 系统集成：输入合成、应用控制、截图、光标下元素识别。

需要的权限（系统设置 → 隐私与安全性）：
  辅助功能   → 合成点击/按键、读取光标下元素
  屏幕录制   → 截图给 Claude 看
  自动化     → AppleScript 控制 System Events 及各应用
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
import time
from pathlib import Path

import Quartz
from AppKit import NSBundle, NSRunningApplication, NSWorkspace
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCopyElementAtPosition,
    AXUIElementCreateSystemWide,
    AXUIElementGetPid,
)

log = logging.getLogger(__name__)

_SYSTEM_AX = AXUIElementCreateSystemWide()

# ---------------------------------------------------------------- 光标与点击


def cursor_pos() -> tuple[float, float]:
    """全局坐标，左上角原点（与 AX / Qt 一致）。"""
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return float(loc.x), float(loc.y)


def click(x: float | None = None, y: float | None = None,
          button: str = "left", count: int = 1) -> None:
    if x is None or y is None:
        x, y = cursor_pos()
    if button == "right":
        down, up, btn = (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp,
                         Quartz.kCGMouseButtonRight)
    else:
        down, up, btn = (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
                         Quartz.kCGMouseButtonLeft)
    for i in range(1, count + 1):
        for kind in (down, up):
            ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), btn)
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, i)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        if count > 1:
            time.sleep(0.04)


def scroll(dy: int, dx: int = 0) -> None:
    ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, dy, dx)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


# ---------------------------------------------------------------- 键盘

# 常用键的虚拟键码（macOS Carbon keycode）
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "grave": 50, "minus": 27, "equal": 24, "slash": 44, "period": 47, "comma": 43,
}

MODIFIERS = {
    "cmd": Quartz.kCGEventFlagMaskCommand,
    "command": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "option": Quartz.kCGEventFlagMaskAlternate,
    "ctrl": Quartz.kCGEventFlagMaskControl,
    "control": Quartz.kCGEventFlagMaskControl,
    "fn": Quartz.kCGEventFlagMaskSecondaryFn,
}


def press_keys(combo: str) -> None:
    """合成组合键，如 "cmd+tab"、"cmd+shift+n"、"escape"。"""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return
    flags = 0
    key = None
    for p in parts:
        if p in MODIFIERS:
            flags |= MODIFIERS[p]
        else:
            key = p
    if key is None or key not in KEYCODES:
        log.warning("未知按键：%s", combo)
        return
    code = KEYCODES[key]
    for is_down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, is_down)
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.01)


def type_text(text: str) -> None:
    """通过剪贴板粘贴（比逐键快，且支持中文）。会覆盖剪贴板内容。"""
    p = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
    if p.returncode == 0:
        time.sleep(0.05)
        press_keys("cmd+v")


# ---------------------------------------------------------------- 应用控制


def osa(script: str, timeout: float = 10.0) -> str:
    """执行 AppleScript，返回 stdout。失败返回以 "ERROR:" 开头的字符串。"""
    try:
        p = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "ERROR: AppleScript 超时"
    if p.returncode != 0:
        return f"ERROR: {p.stderr.strip()}"
    return p.stdout.strip()


def activate(bundle_id: str) -> bool:
    return subprocess.run(["open", "-b", bundle_id], capture_output=True).returncode == 0


def open_url(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    return subprocess.run(["open", url], capture_output=True).returncode == 0


def open_path(path: str, app_bundle: str | None = None) -> bool:
    cmd = ["open"] + (["-b", app_bundle] if app_bundle else []) + [path]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def hide_app(bundle_id: str) -> None:
    osa(f'tell application "System Events" to set visible of '
        f'(every process whose bundle identifier is "{bundle_id}") to false')


def hide_others() -> None:
    press_keys("cmd+alt+h")


def frontmost_bundle() -> str | None:
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.bundleIdentifier() if app else None


def running_bundles() -> set[str]:
    out = set()
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        bid = app.bundleIdentifier()
        if bid:
            out.add(bid)
    return out


def mute(on: bool = True) -> None:
    osa(f"set volume output muted {'true' if on else 'false'}")


def switch_space(index: int) -> None:
    """需要在 系统设置 → 键盘快捷键 → 调度中心 勾选"切换到桌面 N"。"""
    press_keys(f"ctrl+{index}")


def notify(title: str, message: str) -> None:
    osa(f'display notification "{message}" with title "{title}"')


# ---------------------------------------------------------------- 截图


def screenshot_bytes(max_width: int = 1280) -> bytes:
    """截取主屏并缩放，返回 PNG 字节。需要屏幕录制权限。"""
    import tempfile
    from PIL import Image

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    try:
        subprocess.run(["screencapture", "-x", "-C", path], capture_output=True, timeout=8)
        img = Image.open(path)
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    finally:
        try:
            import os
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------- 辅助功能：光标下元素


def _ax(el, attr):
    try:
        err, val = AXUIElementCopyAttributeValue(el, attr, None)
        return None if err else val
    except Exception:
        return None


def element_under_cursor(x: float | None = None, y: float | None = None) -> dict | None:
    """识别光标下方是什么：Dock 图标 / 窗口 / 文件。

    返回 {kind, app, title, path, role} 或 None。
    kind ∈ dock | window | file | unknown
    """
    if x is None or y is None:
        x, y = cursor_pos()
    try:
        err, el = AXUIElementCopyElementAtPosition(_SYSTEM_AX, x, y, None)
    except Exception as e:
        log.debug("AX 查询失败（可能缺少辅助功能权限）：%s", e)
        return None
    if err or el is None:
        return None

    role = _ax(el, "AXRole")
    title = _ax(el, "AXTitle")
    info: dict = {"role": str(role) if role else None,
                  "title": str(title) if title else None,
                  "kind": "unknown", "app": None, "path": None}

    try:
        _, pid = AXUIElementGetPid(el, None)
        owner = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        owner_bundle = owner.bundleIdentifier() if owner else None
    except Exception:
        owner_bundle = None

    if role == "AXDockItem":
        info["kind"] = "dock"
        url = _ax(el, "AXURL")
        if url is not None:
            try:
                path = url.path()
                info["path"] = path
                bundle = NSBundle.bundleWithPath_(path)
                info["app"] = bundle.bundleIdentifier() if bundle else None
            except Exception:
                pass
        if not info["app"] and title:
            info["app"] = _bundle_by_name(str(title))
        return info

    # Finder / 桌面上的文件或图标（不限角色：桌面图标的 AX 角色各版本不一致）
    if owner_bundle == "com.apple.finder":
        url = _ax(el, "AXURL")
        if url is not None:
            try:
                info["kind"] = "file"
                info["path"] = url.path()
                info["app"] = owner_bundle
                return info
            except Exception:
                pass
        # 桌面图标常没有 AXURL；名字可能在 AXTitle 或 AXDescription 里
        name = info["title"] or _ax(el, "AXDescription")
        if name:
            info["kind"] = "finder_item"
            info["title"] = str(name)
            return info

    info["kind"] = "window"
    info["app"] = owner_bundle
    if not info["title"]:
        win = _ax(el, "AXWindow")
        if win is not None:
            wt = _ax(win, "AXTitle")
            info["title"] = str(wt) if wt else None
    return info


def _as_esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def resolve_finder_target(name: str) -> str | None:
    """把 Finder 桌面上名为 name 的项目（含替身）解析成目标 App 的 bundle id。

    演示场景：桌面放着「Pages 文稿」替身 → 指向 /Applications/Pages.app。
    """
    r = osa(f'''
        tell application "Finder"
            try
                set theItem to (item "{_as_esc(name)}" of desktop)
                set c to class of theItem as text
                if c is "alias file" then
                    set orig to original item of theItem
                    if (class of orig as text) is "application file" then
                        return name of orig
                    end if
                else if c is "application file" then
                    return name of theItem
                end if
                return ""
            on error
                return ""
            end try
        end tell
    ''', timeout=6)
    if r and not r.startswith("ERROR"):
        return _bundle_by_name(r.strip())
    # 图标的 AX 描述常带元数据尾巴（"Pages 文稿，780 字节"）→ 掐掉再试
    if name and ("，" in name or "," in name):
        short = re.split("[，,]", name, 1)[0].strip()
        if short != name:
            r2 = osa(f'''
                tell application "Finder"
                    try
                        set theItem to (item "{_as_esc(short)}" of desktop)
                        if (class of theItem as text) is "alias file" then
                            return name of (original item of theItem)
                        else if (class of theItem as text) is "application file" then
                            return name of theItem
                        end if
                        return ""
                    on error
                        return ""
                    end try
                end tell
            ''', timeout=6)
            if r2 and not r2.startswith("ERROR"):
                return _bundle_by_name(r2.strip())
    return None


def bundle_for_path(path: str) -> str | None:
    """路径 → bundle id。.app 直接解；.pages 文档归 Pages。"""
    p = Path(str(path)).expanduser()
    if p.suffix == ".app":
        b = NSBundle.bundleWithPath_(str(p))
        return b.bundleIdentifier() if b else None
    if p.suffix == ".pages":
        return "com.apple.iWork.Pages"
    return None


def _bundle_by_name(name: str) -> str | None:
    for suffix in (".app", ""):
        for base in ("/System/Applications", "/Applications", "/System/Applications/Utilities"):
            path = f"{base}/{name}{suffix}"
            bundle = NSBundle.bundleWithPath_(path)
            if bundle:
                bid = bundle.bundleIdentifier()
                if bid:
                    return bid
    return None


def is_secure_field_focused() -> bool:
    """焦点是否在密码框——Agent 绝不能往里打字。"""
    try:
        app = _ax(_SYSTEM_AX, "AXFocusedApplication")
        if app is None:
            return False
        el = _ax(app, "AXFocusedUIElement")
        if el is None:
            return False
        return str(_ax(el, "AXRole")) == "AXSecureTextField"
    except Exception:
        return False
