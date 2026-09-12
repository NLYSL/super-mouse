"""内置技能：不需要模型也能跑的"配方"。

为什么要有这一层：
  1. 断网 / API 慢时，Demo 仍然能完整走通（这是保命机制）
  2. AppleScript 比让模型逐步操作 UI 稳定得多
  3. 快路径的建议直接绑定到技能，零延迟

每个技能签名统一为 fn(ctx, **args) -> str，返回给用户看的一句话结果。
ctx 提供 progress() 回调，用来更新老鼠牌子。
"""

from __future__ import annotations

import logging
import subprocess
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ...actions import macos

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillResult:
    status: str
    message: str
    artifact_path: str = ""
    opened: bool = False


class SkillContext:
    """技能运行时上下文：进度回调 + 取消检查。"""

    def __init__(self, bus, plan_id: str, cfg):
        self.bus = bus
        self.plan_id = plan_id
        self.cfg = cfg
        self.cancelled = False

    def progress(self, message: str) -> None:
        log.info("[技能] %s", message)
        self.bus.publish("brain.action", plan_id=self.plan_id,
                         status="running", message=message)

    def check(self) -> None:
        if self.cancelled:
            raise SkillCancelled()


class SkillCancelled(Exception):
    pass


# ---------------------------------------------------------------- Mail


def mail_draft_replies(ctx: SkillContext, count: int = 3, **_) -> str:
    """读未读邮件 → 为每封创建一份草稿（只存草稿，绝不发送）。"""
    ctx.progress("正在读取未读邮件…")
    listing = macos.osa(f'''
        tell application "Mail"
            set out to ""
            set msgs to (messages of inbox whose read status is false)
            set n to count of msgs
            if n > {count} then set n to {count}
            repeat with i from 1 to n
                set m to item i of msgs
                set out to out & (sender of m) & "\t" & (subject of m) & "\\n"
            end repeat
            return out
        end tell
    ''', timeout=15)

    if listing.startswith("ERROR"):
        raise RuntimeError(f"读邮件失败：{listing[7:120]}")

    rows = [r for r in listing.split("\n") if "\t" in r]
    if not rows:
        return "收件箱里没有未读邮件"

    made = 0
    for i, row in enumerate(rows, 1):
        ctx.check()
        sender, subject = row.split("\t", 1)
        ctx.progress(f"起草回复 {i}/{len(rows)}…")
        body = (f"你好，\n\n关于「{subject.strip()}」，我已收到，"
                f"正在处理，会尽快给你答复。\n\n祝好")
        r = macos.osa(f'''
            tell application "Mail"
                set d to make new outgoing message with properties ¬
                    {{subject:"Re: {_esc(subject.strip())}", content:"{_esc(body)}", visible:false}}
                tell d to make new to recipient at end of to recipients ¬
                    with properties {{address:"{_esc(_email(sender))}"}}
                save d
            end tell
        ''', timeout=12)
        if not r.startswith("ERROR"):
            made += 1

    return SkillResult("done" if made == len(rows) else "partial" if made else "error", f"已创建 {made}/{len(rows)} 封草稿")


# ---------------------------------------------------------------- 编辑器


def vscode_open_todo(ctx: SkillContext, project: str | None = None, **_) -> str:
    """打开项目并搜集 TODO。"""
    root = Path(str(project or "~/Documents")).expanduser()
    ctx.progress(f"打开 {root.name}…")
    if not macos.open_path(str(root), "com.microsoft.VSCode"):
        if not macos.activate("com.microsoft.VSCode"):
            raise RuntimeError("无法打开 VS Code")

    ctx.check()
    ctx.progress("扫描 TODO…")
    todos: list[str] = []
    exts = {".py", ".ts", ".tsx", ".js", ".md", ".swift", ".go", ".rs", ".java"}
    skip = {"node_modules", ".git", ".venv", "venv", "dist", "build", "__pycache__"}
    try:
        for path in root.rglob("*"):
            if len(todos) >= 20:
                break
            if not path.is_file() or path.suffix not in exts:
                continue
            if any(part in skip for part in path.parts):
                continue
            try:
                for ln, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
                    if "TODO" in line or "FIXME" in line:
                        todos.append(f"{path.relative_to(root)}:{ln} {line.strip()[:70]}")
                        if len(todos) >= 20:
                            break
            except OSError:
                continue
    except OSError as e:
        raise RuntimeError(f"扫描失败：{e}")

    if not todos:
        return f"{root.name} 已打开，没有待办"
    macos.notify("Super Mouse", f"找到 {len(todos)} 条 TODO")
    log.info("TODO 列表：\n%s", "\n".join(todos))
    return f"{root.name} 已打开，{len(todos)} 条待办"


# ---------------------------------------------------------------- 备忘录


def notes_daily_report(ctx: SkillContext, **_) -> str:
    """在备忘录里新建今天的日报模板。"""
    today = datetime.now().strftime("%Y-%m-%d")
    ctx.progress("创建日报…")
    body = (f"<h1>{today} 日报</h1>"
            f"<div>今天完成：</div><div>• </div>"
            f"<div><br></div><div>遇到的问题：</div><div>• </div>"
            f"<div><br></div><div>明天计划：</div><div>• </div>")
    r = macos.osa(f'''
        tell application "Notes"
            activate
            make new note at folder "Notes" of default account ¬
                with properties {{name:"{today} 日报", body:"{_esc(body)}"}}
        end tell
    ''', timeout=12)
    if r.startswith("ERROR"):
        raise RuntimeError(f"创建失败：{r[7:120]}")
    return f"{today} 日报模板已就绪"


# ---------------------------------------------------------------- 浏览器


def browser_morning_tabs(ctx: SkillContext, urls: list[str] | None = None, **_) -> str:
    """打开一组常用标签页。"""
    urls = urls if urls is not None else ctx.cfg.section("brain.apps").get("com.google.Chrome", {}).get("urls", [])
    if not urls:
        raise ValueError("还没配置早间标签页")
    opened = 0
    for i, url in enumerate(urls, 1):
        ctx.check()
        ctx.progress(f"打开标签 {i}/{len(urls)}…")
        if macos.open_url(url):
            opened += 1
    return SkillResult("done" if opened == len(urls) else "partial" if opened else "error", f"已打开 {opened}/{len(urls)} 个标签页")


# ---------------------------------------------------------------- 文件


def summarize_file(ctx: SkillContext, path: str | None = None, **_) -> str:
    """读文件 → 让 Claude 总结 → 通知展示。断网时退化为显示文件信息。"""
    if not path:
        raise ValueError("没有指定文件")
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError("这不是一个文件")

    allowed = [Path(str(d)).expanduser().resolve()
               for d in ctx.cfg.get("brain.allowed_dirs", [])]
    try:
        resolved = p.resolve()
    except OSError:
        raise ValueError("路径无法访问")
    if allowed and not any(_under(resolved, a) for a in allowed):
        raise ValueError("这个目录不在允许范围内")

    if p.stat().st_size > 200_000:
        raise ValueError(f"{p.name} 太大了（>200 KB）")

    ctx.progress(f"读取 {p.name}…")
    try:
        text = p.read_text(errors="ignore")
    except OSError as e:
        raise RuntimeError(f"读取失败：{e}")

    ctx.check()
    ctx.progress("正在总结…")
    from .client import summarize_text
    summary = summarize_text(text, p.name)
    if summary:
        macos.notify(f"{p.name} 总结", summary[:200])
        return summary[:40]
    lines = text.count("\n") + 1
    return SkillResult("partial", f"{p.name}：{lines} 行，{len(text)} 字（离线，无法总结）")


# ---------------------------------------------------------------- RTF / 行情


def _rtf_escape(text: str) -> str:
    """把中文文本转成 RTF 里安全的纯 ASCII 表示。

    用 `\\uNNNN?` 转义所有非 ASCII 字符，这样输出文件是纯 ASCII，
    不依赖代码页（cp936 那条路在不同系统上会乱码）。
    `?` 是给不认识 Unicode 的老解析器看的降级字符。
    """
    out: list[str] = []
    for ch in text:
        o = ord(ch)
        if ch == "\n":
            out.append("\\par\n")
        elif ch in "\\{}":
            out.append("\\" + ch)
        elif o < 128:
            out.append(ch)
        else:
            # RTF 的 \u 用有符号 16 位整数
            for i in range(0, len(ch.encode("utf-16-le")), 2):
                unit = int.from_bytes(ch.encode("utf-16-le")[i:i+2], "little")
                out.append(f"\\u{unit if unit < 32768 else unit - 65536}?")
    return "".join(out)


def _make_rtf(lines: list[tuple[str, str]]) -> str:
    """把 (样式, 文本) 列表拼成 RTF。样式：title / h2 / body / dim。"""
    parts = [
        r"{\rtf1\ansi\deff0",
        r"{\fonttbl{\f0\fnil\fcharset134 PingFang SC;}}",
        r"{\colortbl;\red0\green0\blue0;\red137\green135\blue129;}",
        "\n",
    ]
    for style, text in lines:
        esc = _rtf_escape(text)
        if style == "title":
            parts.append(r"\f0\fs44\b " + esc + r"\b0\par" + "\n")
        elif style == "h2":
            parts.append(r"\f0\fs30\b " + esc + r"\b0\par" + "\n")
        elif style == "dim":
            parts.append(r"\f0\fs20\cf2 " + esc + r"\cf1\par" + "\n")
        else:
            parts.append(r"\f0\fs24 " + esc + r"\par" + "\n")
    parts.append("}")
    return "".join(parts)


# 报告默认盯的标的。新浪的 `s_` 简化接口直接给涨跌幅，不用自己算。
_DEFAULT_SYMBOLS = [
    ("s_sh000001", "上证指数"),
    ("s_sz399001", "深证成指"),
    ("s_sh000300", "沪深300"),
    ("s_sz399006", "创业板指"),
]


def _fetch_quotes(symbols: list[tuple[str, str]]) -> tuple[list[dict], str | None]:
    """抓行情。返回 (行情列表, 错误说明)。

    新浪 `hq.sinajs.cn` 免 key，但**必须带 Referer**，否则返回 403。
    简化接口 `s_` 的字段：名称,当前价,涨跌额,涨跌幅%,成交量(手),成交额(万)
    """
    import urllib.request

    codes = ",".join(c for c, _ in symbols)
    url = f"https://hq.sinajs.cn/list={codes}"
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode("gbk", errors="replace")
    except Exception as e:
        return [], f"行情接口没连上（{type(e).__name__}）"

    want = dict(symbols)
    quotes: list[dict] = []
    for line in raw.splitlines():
        if "=" not in line:
            continue
        left, _, right = line.partition("=")
        code = left.split("_str_")[-1].strip()
        body = right.strip().strip('";')
        if not body:
            continue
        f = body.split(",")
        if code not in want or len(f) < 4:
            continue
        try:
            quotes.append({
                "code": code,
                "name": (want.get(code) if want.get(code) != code else None) or f[0],
                "price": float(f[1]),
                "change": float(f[2]),
                "pct": float(f[3]),
            })
        except ValueError:
            continue

    quotes = [q for q in quotes if q["price"] > 0 and all(math.isfinite(q[k]) for k in ("price", "change", "pct"))]
    quotes = list({q["code"]: q for q in quotes}.values())
    if not quotes:
        return [], "行情接口返回了空数据"
    missing = set(want) - {q["code"] for q in quotes}
    return quotes, ("缺少行情：" + ", ".join(sorted(missing))) if missing else None


def _build_report(quotes: list[dict], err: str | None) -> list[tuple[str, str]]:
    """把行情整理成报告段落。只陈述数据算得出的事实，不编分析。"""
    now = datetime.now()
    lines: list[tuple[str, str]] = [
        ("title", "今日股票报告"),
        ("dim", f"由 Super Mouse 生成 · {now.strftime('%Y年%m月%d日 %H:%M')}"),
        ("body", ""),
    ]

    lines.append(("dim", "数据来源：新浪财经；以上为生成时间，接口未提供行情时间，不能保证为实时或当日数据。非投资建议。"))

    if err:
        lines += [
            ("h2", "行情未取到"),
            ("body", f"{err}。"),
            ("body", "以下只展示已获取的数据；缺失数据请稍后重新获取。"),
            ("body", ""),
        ]
        if not quotes:
            return lines

    lines.append(("h2", "主要指数"))
    for q in quotes:
        arrow = "▲" if q["pct"] > 0 else ("▼" if q["pct"] < 0 else "—")
        lines.append((
            "body",
            f"{q['name']}　{q['price']:.2f}　{arrow} {q['change']:+.2f}"
            f"（{q['pct']:+.2f}%）",
        ))
    lines.append(("body", ""))

    ups = [q for q in quotes if q["pct"] > 0]
    downs = [q for q in quotes if q["pct"] < 0]
    lines.append(("h2", "概览"))
    if len(ups) == len(quotes):
        lines.append(("body", f"跟踪的 {len(quotes)} 个指数全部上涨。"))
    elif len(downs) == len(quotes):
        lines.append(("body", f"跟踪的 {len(quotes)} 个指数全部下跌。"))
    else:
        lines.append((
            "body",
            f"跟踪的 {len(quotes)} 个指数中，{len(ups)} 涨 {len(downs)} 跌"
            f"{'，' + str(len(quotes) - len(ups) - len(downs)) + ' 平' if len(quotes) - len(ups) - len(downs) else ''}。",
        ))

    strongest = max(quotes, key=lambda q: q["pct"])
    weakest = min(quotes, key=lambda q: q["pct"])
    if strongest["code"] != weakest["code"]:
        lines.append((
            "body",
            f"表现最强 {strongest['name']}（{strongest['pct']:+.2f}%），"
            f"最弱 {weakest['name']}（{weakest['pct']:+.2f}%）。",
        ))

    lines += [
        ("body", ""),
        ("dim", "数据来源：新浪财经。本报告为自动汇总，不构成投资建议。"),
    ]
    return lines


def _open_pages(path: Path) -> tuple[bool, str]:
    # Pages imports DOCX. Use the returned document reference, not just `open` exit 0.
    script = f'''tell application "Pages"
        activate
        set reportDocument to open POSIX file "{_esc(str(path))}"
        return name of reportDocument
    end tell'''
    result = macos.osa(script, timeout=45)
    return bool(result and not result.startswith("ERROR")), result


def pages_stock_report(ctx: SkillContext, symbols: list | None = None,
                       output_dir: str = "~/.supermouse/reports",
                       report_title: str = "今日股票报告", notes: str = "", **_) -> SkillResult:
    """Fetch → RTF → DOCX import → verify Pages returned a document.

    A generated file alone is not success. Incomplete quotes are explicitly partial.
    """
    ctx.check()
    picks = _DEFAULT_SYMBOLS if symbols is None else [
        (s, s) if isinstance(s, str) else tuple(s) for s in symbols]
    if not picks or any(len(s) != 2 or not re.fullmatch(r"s_(sh|sz)\d{6}", s[0]) for s in picks):
        raise ValueError("标的格式应为 s_sh000001 或 [代码, 名称]；列表不能为空")
    ctx.progress("正在获取行情…")
    quotes, err = _fetch_quotes(picks)
    ctx.check()
    lines = _build_report(quotes, err)
    lines[0] = ("title", report_title)
    if notes.strip():
        lines.extend([("h2", "用户备注（非行情分析）"), ("body", notes)])
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"股票报告-{datetime.now():%Y%m%d-%H%M%S-%f}.docx"
    rtf_path = path.with_suffix(".rtf")
    rtf_path.write_text(_make_rtf(lines), encoding="ascii")
    ctx.check()
    ctx.progress("正在生成可编辑文稿…")
    try:
        converted = subprocess.run(["/usr/bin/textutil", "-convert", "docx", "-output", str(path), str(rtf_path)],
                                   capture_output=True, text=True, timeout=20)
        if converted.returncode or not path.is_file() or not path.stat().st_size:
            return SkillResult("error", "文稿转换失败：" + converted.stderr.strip()[:160], str(rtf_path))
        ctx.check()
        ctx.progress("正在打开 Pages 并检查文档…")
        opened, detail = _open_pages(path)
    except subprocess.TimeoutExpired:
        return SkillResult("error", "转换或打开超时，未确认完成", str(path if path.exists() else rtf_path))
    ctx.check()
    if not opened:
        return SkillResult("error", "报告已保存，但未确认 Pages 打开。请检查系统设置→隐私与安全性→自动化。" + detail[:120], str(path))
    return SkillResult("partial" if err else "done",
                       (f"Pages 已打开；报告数据不完整：{err}" if err else f"Pages 已打开 {len(quotes)} 个标的的报告"), str(path), True)


# ---------------------------------------------------------------- 注册表

REGISTRY = {
    "mail_draft_replies": mail_draft_replies,
    "vscode_open_todo": vscode_open_todo,
    "notes_daily_report": notes_daily_report,
    "browser_morning_tabs": browser_morning_tabs,
    "summarize_file": summarize_file,
    "pages_stock_report": pages_stock_report,
}

# 快路径：每个应用的默认建议文案（喂养之后会被 plans.json 覆盖）
DEFAULT_PLANS = {
    "com.apple.mail": {
        "title": "要我起草未读邮件的回复吗？",
        "skill": "mail_draft_replies",
        "args": {"count": 3},
        "steps": ["读取未读邮件", "逐封生成回复", "保存为草稿"],
        "needs_confirm": False,
    },
    "com.microsoft.VSCode": {
        "title": "打开项目并列出今天的 TODO？",
        "skill": "vscode_open_todo",
        "args": {},
        "steps": ["打开项目", "扫描 TODO", "汇总提醒"],
        "needs_confirm": False,
    },
    "com.apple.Notes": {
        "title": "新建今天的日报草稿？",
        "skill": "notes_daily_report",
        "args": {},
        "steps": ["创建备忘录", "填入日报模板"],
        "needs_confirm": False,
    },
    "com.google.Chrome": {
        "title": "打开你每天早上的标签页？",
        "skill": "browser_morning_tabs",
        "args": {},
        "steps": ["读取标签列表", "逐个打开"],
        "needs_confirm": False,
    },
    "com.apple.iWork.Pages": {
        "title": "写一份今日股票报告？",
        "skill": "pages_stock_report",
        "args": {},
        "steps": ["获取主要指数行情", "排版成文稿", "用 Pages 打开"],
        "needs_confirm": False,
    },
}


def _esc(s: str) -> str:
    """转义 AppleScript 字符串字面量。"""
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n").replace("\r", ""))




def _email(sender: str) -> str:
    """从 "张三 <a@b.com>" 里取出邮箱。"""
    if "<" in sender and ">" in sender:
        return sender.split("<", 1)[1].split(">", 1)[0].strip()
    return sender.strip()


def _under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
