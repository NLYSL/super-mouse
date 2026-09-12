"""执行 Agent：用 Claude Tool Runner 自由组合工具完成任务。

只在**没有对应内置技能**时才走到这里（内置技能更快更稳，断网也能跑）。

安全边界（docs/04 §7）：
  - 应用白名单：只能操作配置里出现过的应用 + 少数系统应用
  - 目录白名单：只能读 brain.allowed_dirs 内的文件
  - 密码框保护：焦点在 AXSecureTextField 时拒绝打字
  - 只到草稿：发送/删除类动作必须先 ask_confirm
  - 工具调用上限 25 次、单任务 90 s，防止失控循环

@beta_tool 装饰的函数必须是模块级的，所以运行时上下文（进度回调、取消标志）
放在模块级变量里；SmartBrain 保证同一时刻只有一个任务在跑。
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from anthropic import beta_tool

from ...actions import macos
from . import client
from .plans import resolve
from .skills import SkillResult, SkillCancelled, SkillContext

log = logging.getLogger(__name__)

MAX_TOOL_CALLS = 25
TASK_TIMEOUT_S = 90.0

BASE_ALLOWED_APPS = {
    "com.apple.mail", "com.apple.Notes", "com.apple.finder", "com.apple.Safari",
    "com.apple.iWork.Pages", "com.apple.TextEdit", "com.apple.reminders", "com.apple.iCal",
    "com.microsoft.VSCode", "com.google.Chrome", "com.todesktop.230313mzl4w4u92",
}

BASE_ALLOWED_SCRIPT_APPS = {
    "Pages", "System Events", "Mail", "Notes", "Finder", "Safari", "TextEdit",
    "Reminders", "Calendar", "Google Chrome", "Music",
}

DANGEROUS_KEYS = {"cmd+q", "cmd+w", "cmd+delete", "cmd+shift+delete"}


class _Runtime:
    """当前任务的运行时状态。"""

    def __init__(self) -> None:
        self.ctx: SkillContext | None = None
        self.calls = 0
        self.started = 0.0
        self.allowed_apps: set[str] = set(BASE_ALLOWED_APPS)
        self.allowed_dirs: list[Path] = []
        self.lock = threading.Lock()

    def reset(self, ctx: SkillContext) -> None:
        self.ctx = ctx
        self.calls = 0
        self.started = time.time()
        cfg = ctx.cfg
        self.allowed_apps = set(BASE_ALLOWED_APPS) | set(resolve(cfg))
        self.allowed_dirs = []
        for d in cfg.get("brain.allowed_dirs", []) or []:
            try:
                self.allowed_dirs.append(Path(str(d)).expanduser().resolve())
            except OSError:
                continue


_RT = _Runtime()


def _guard() -> str | None:
    """每个工具入口都要过一遍：取消 / 超时 / 次数上限。返回错误字符串或 None。"""
    with _RT.lock:
        _RT.calls += 1
        calls = _RT.calls
    if _RT.ctx is not None and _RT.ctx.cancelled:
        return "已被用户中止，请停止并总结。"
    if calls > MAX_TOOL_CALLS:
        return f"工具调用已达上限（{MAX_TOOL_CALLS}），请立刻总结现状并结束。"
    if _RT.started and time.time() - _RT.started > TASK_TIMEOUT_S:
        return "任务已超时，请立刻总结现状并结束。"
    return None


def _progress(msg: str) -> None:
    if _RT.ctx is not None:
        _RT.ctx.progress(msg)


# ---------------------------------------------------------------- 工具


@beta_tool
def open_app(bundle_id: str) -> str:
    """打开或激活一个 macOS 应用。

    Args:
        bundle_id: 应用的 bundle identifier，例如 com.apple.mail。
    """
    if (err := _guard()):
        return err
    if bundle_id not in _RT.allowed_apps:
        return f"拒绝：{bundle_id} 不在允许列表里"
    _progress(f"打开 {bundle_id.split('.')[-1]}…")
    return f"已激活 {bundle_id}" if macos.activate(bundle_id) else f"打开 {bundle_id} 失败"


@beta_tool
def open_url(url: str) -> str:
    """在默认浏览器里打开一个网址。

    Args:
        url: 完整网址，必须以 http:// 或 https:// 开头。
    """
    if (err := _guard()):
        return err
    if not url.startswith(("http://", "https://")):
        return "拒绝：只允许 http/https 网址"
    _progress("打开网页…")
    return f"已打开 {url}" if macos.open_url(url) else "打开失败"


@beta_tool
def run_applescript(script: str) -> str:
    """执行一段 AppleScript 来控制应用。这是操作 Mail、Notes 等应用最可靠的方式。

    Args:
        script: AppleScript 源码。脚本里 tell application "X" 的 X 必须是允许的应用。
    """
    if (err := _guard()):
        return err
    import re

    for name in re.findall(r'tell\s+application\s+"([^"]+)"', script):
        if name not in BASE_ALLOWED_SCRIPT_APPS:
            return f"拒绝：不允许控制应用 {name}"
    low = script.lower()
    for word in ("send", "delete", "empty trash", "quit"):
        if word in low:
            return (f"拒绝：脚本里含有 {word!r} 这类不可逆动作。"
                    f"如确实需要，请先调用 ask_confirm 征得用户同意，"
                    f"或改成只创建草稿（save 而不是 send）。")
    _progress("操作应用…")
    out = macos.osa(script, timeout=12)
    return out[:2000] if out else "执行完成，无返回值"


@beta_tool
def press_keys(combo: str) -> str:
    """按下一组快捷键。

    Args:
        combo: 组合键，如 "cmd+n"、"cmd+shift+p"、"escape"。
    """
    if (err := _guard()):
        return err
    if combo.strip().lower() in DANGEROUS_KEYS:
        return f"拒绝：{combo} 可能导致数据丢失"
    macos.press_keys(combo)
    return f"已按下 {combo}"


@beta_tool
def type_text(text: str) -> str:
    """在当前光标位置输入文本（通过剪贴板粘贴，支持中文）。

    Args:
        text: 要输入的文本。
    """
    if (err := _guard()):
        return err
    if macos.is_secure_field_focused():
        return "拒绝：焦点在密码框里，绝不输入内容"
    if len(text) > 5000:
        return "拒绝：文本过长（>5000 字）"
    _progress("输入内容…")
    macos.type_text(text)
    return f"已输入 {len(text)} 个字符"


@beta_tool
def read_file(path: str) -> str:
    """读取一个文本文件的内容。

    Args:
        path: 文件路径，必须在允许的目录内。
    """
    if (err := _guard()):
        return err
    p = Path(path).expanduser()
    ok, reason = _path_allowed(p)
    if not ok:
        return f"拒绝：{reason}"
    if not p.is_file():
        return "这不是一个文件"
    if p.stat().st_size > 200_000:
        return "文件太大（>200 KB）"
    try:
        return p.read_text(errors="ignore")[:60000]
    except OSError as e:
        return f"读取失败：{e}"


@beta_tool
def list_dir(path: str) -> str:
    """列出一个目录下的文件和子目录。

    Args:
        path: 目录路径，必须在允许的目录内。
    """
    if (err := _guard()):
        return err
    p = Path(path).expanduser()
    ok, reason = _path_allowed(p)
    if not ok:
        return f"拒绝：{reason}"
    if not p.is_dir():
        return "这不是一个目录"
    try:
        entries = sorted(p.iterdir())[:120]
    except OSError as e:
        return f"列举失败：{e}"
    return "\n".join(f"{'[目录] ' if e.is_dir() else ''}{e.name}" for e in entries) or "空目录"


@beta_tool
def see_screen(question: str) -> str:
    """截取当前屏幕并回答关于屏幕内容的问题。当你需要知道界面上有什么时使用。

    Args:
        question: 想从屏幕上了解的内容，例如"列出收件箱里未读邮件的发件人和主题"。
    """
    if (err := _guard()):
        return err
    _progress("看一眼屏幕…")
    try:
        png = macos.screenshot_bytes(max_width=1280)
    except Exception as e:
        return f"截图失败：{e}"
    return client.vision_answer(png, question)


@beta_tool
def notify(message: str) -> str:
    """把当前进度显示在老鼠举的牌子上，让用户知道你在做什么。

    Args:
        message: 一句 15 字以内的中文进度说明。
    """
    if _RT.ctx is not None and _RT.ctx.cancelled:
        return "已被用户中止"
    _progress(message)
    return "已显示给用户"


@beta_tool
def ask_confirm(question: str) -> str:
    """在执行不可逆动作（发送、删除、发布、支付）前询问用户。用户重眨或点牌子表示同意。

    Args:
        question: 要问用户的问题，例如"确认发送这 3 封邮件吗？"。
    """
    if (err := _guard()):
        return err
    ctx = _RT.ctx
    if ctx is None:
        return "用户拒绝（没有交互上下文）"

    approved = threading.Event()
    bus = ctx.bus

    def on_confirm(_evt: dict) -> None:
        approved.set()

    unsub_a = bus.subscribe("ui.confirm_gesture", on_confirm)
    unsub_b = bus.subscribe("ui.click_suggest", on_confirm)
    bus.publish("brain.suggest", plan_id=ctx.plan_id, title=question, steps=[])
    try:
        got = approved.wait(timeout=15.0)
    finally:
        unsub_a()
        unsub_b()
    if ctx.cancelled:
        return "已被用户中止"
    return "用户同意了" if got else "用户没有回应（15 秒超时），视为拒绝，不要执行该动作"


ALL_TOOLS = [
    open_app, open_url, run_applescript, press_keys, type_text,
    read_file, list_dir, see_screen, notify, ask_confirm,
]


def _path_allowed(p: Path) -> tuple[bool, str]:
    if not _RT.allowed_dirs:
        return False, "没有配置允许的目录"
    try:
        resolved = p.resolve()
    except OSError:
        return False, "路径无法解析"
    for base in _RT.allowed_dirs:
        try:
            resolved.relative_to(base)
            return True, ""
        except ValueError:
            continue
    return False, f"{p} 不在允许的目录内"


# ---------------------------------------------------------------- 主循环


def run_plan(profile_md: str, plan: dict, ctx: SkillContext) -> str:
    """在线程池里同步执行一个计划。返回给用户看的一句话结果。"""
    # 自由工具循环依赖 Anthropic 的 beta tool_runner；DeepSeek 后端挡掉，
    # 提示改绑内置技能（面板 brain 设置里可选）。
    if client.provider() != "anthropic":
        ctx.progress("该任务需要 Claude 后端")
        return "自由 AI 任务暂不可用，请把该应用绑定到内置技能"
    c = client.get_client()
    if c is None:
        raise RuntimeError("这个任务需要可用的在线模型")

    _RT.reset(ctx)
    task = (f"执行以下计划：{plan.get('title')}\n"
            f"用户要求：{plan.get('instruction') or '无补充'}\n"
            f"步骤：{plan.get('steps') or '（自行决定）'}\n"
            f"相关上下文：{plan.get('hover') or {}}\n"
            f"完成后用一句话（≤ 30 字）总结结果。")

    system = [{"type": "text", "text": client.AGENT_SYSTEM}]
    if profile_md:
        system.append({"type": "text", "text": f"<profile>\n{profile_md}\n</profile>",
                       "cache_control": {"type": "ephemeral"}})

    try:
        runner = c.beta.messages.tool_runner(
            model=client.MODEL,
            max_tokens=8192,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=system,
            tools=ALL_TOOLS,
            messages=[{"role": "user", "content": task}],
        )
    except Exception as e:
        log.warning("创建 tool_runner 失败：%s", e)
        raise RuntimeError(f"启动失败：{e}") from e

    final = None
    try:
        for message in runner:
            final = message
            if ctx.cancelled:
                break
            for block in message.content:
                if getattr(block, "type", None) == "tool_use":
                    log.info("Agent 调用工具 %s %s", block.name, block.input)
            if _RT.calls > MAX_TOOL_CALLS or time.time() - _RT.started > TASK_TIMEOUT_S:
                log.warning("Agent 触及上限，停止循环")
                return SkillResult("partial", "AI 执行达到时间或工具次数上限，未确认完成")
    except SkillCancelled:
        raise SkillCancelled()
    except Exception as e:
        log.exception("Agent 循环出错")
        raise RuntimeError(f"执行出错：{e}") from e

    if final is None:
        raise RuntimeError("没有得到结果")
    if getattr(final, "stop_reason", None) == "refusal":
        raise RuntimeError("模型拒绝执行此请求")
    text = "".join(b.text for b in final.content if getattr(b, "type", None) == "text").strip()
    return SkillResult("partial", "AI 执行结果（需人工核验）：" + (text or "没有完成说明")[:200])
