"""LLM 后端封装：Anthropic Claude 或 DeepSeek（OpenAI 兼容协议）。

后端选择（优先级）：
  1. App 启动时 SmartBrain.install() 调 configure(cfg)
  2. 环境变量：DEEPSEEK_API_KEY → deepseek；ANTHROPIC_API_KEY → anthropic
  3. 直接读 ~/.supermouse/config.yaml 的 brain 段（独立工具也能用）

所有函数**失败时返回 None 而不抛异常** —— 断网 / 换后端是演示的常见风险，
上层必须能无缝退化到内置技能（快路径）。

DeepSeek 是 OpenAI 兼容协议（api.deepseek.com），无 thinking / cache_control /
beta tool_runner —— 这些只在 Anthropic 分支使用；自由工具任务在 DeepSeek 下
由 agent.py 挡掉并提示改用内置技能。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"          # Anthropic 默认（agent.py 等仍在引用）
DS_MODEL = "deepseek-flash"      # DeepSeek 默认（实测在模型列表里）

_provider: str | None = None     # None = 未 configure，按环境/配置推断
_ds_api_key: str | None = None
_ds_base_url: str = "https://api.deepseek.com"
_ds_model: str = DS_MODEL

_client = None                    # anthropic 客户端
_ds_client = None                 # openai 客户端
_client_lock = threading.Lock()
_unavailable_reason: str | None = None

USER_CONFIG = Path.home() / ".supermouse" / "config.yaml"


class Suggestion(BaseModel):
    """老鼠举牌的建议。"""

    title: str = Field(description="牌子文案，≤ 20 字的中文问句")
    skill: str | None = Field(default=None, description="内置技能名，没有合适的填 null")
    args: dict = Field(default_factory=dict, description="技能参数")
    steps: list[str] = Field(default_factory=list, description="执行步骤，3-5 条")
    needs_confirm: bool = Field(default=False, description="是否包含不可逆动作")


# ---------------------------------------------------------------- 后端选择


def configure(cfg) -> None:
    """App 启动时由 SmartBrain 调用，把 brain 段的配置喂进来。"""
    global _provider, _ds_api_key, _ds_base_url, _ds_model
    provider = str(cfg.get("brain.provider", "") or "").strip().lower()
    if provider in ("anthropic", "deepseek"):
        _provider = provider
    key = cfg.get("brain.api_key")
    if key:
        _ds_api_key = str(key)
    url = cfg.get("brain.base_url")
    if url:
        _ds_base_url = str(url)
    m = cfg.get("brain.ds_model")
    if m:
        _ds_model = str(m)


def _yaml_brain() -> dict:
    try:
        import yaml
        with USER_CONFIG.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        b = data.get("brain") or {}
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def provider() -> str:
    """当前生效的后端：'anthropic' | 'deepseek'。"""
    global _provider, _ds_api_key
    if _provider:
        return _provider
    # 未 configure：环境变量 → 用户配置文件 → 默认 anthropic
    if os.environ.get("DEEPSEEK_API_KEY"):
        _ds_api_key = _ds_api_key or os.environ["DEEPSEEK_API_KEY"]
        _provider = "deepseek"
        return _provider
    b = _yaml_brain()
    p = str(b.get("provider", "")).lower()
    if p in ("anthropic", "deepseek"):
        _provider = p
        if b.get("api_key"):
            _ds_api_key = _ds_api_key or str(b["api_key"])
        if b.get("base_url"):
            globals()["_ds_base_url"] = str(b["base_url"])
        if b.get("ds_model"):
            globals()["_ds_model"] = str(b["ds_model"])
        return _provider
    _provider = "anthropic"
    return _provider


def model_name() -> str:
    return _ds_model if provider() == "deepseek" else MODEL


def get_client():
    """按后端懒加载客户端。没有凭据时返回 None。"""
    global _client, _ds_client, _unavailable_reason
    if provider() == "deepseek":
        if _ds_client is not None:
            return _ds_client
        with _client_lock:
            if _ds_client is not None:
                return _ds_client
            key = _ds_api_key or os.environ.get("DEEPSEEK_API_KEY")
            if not key:
                b = _yaml_brain()
                key = str(b.get("api_key") or "") or None
            if not key:
                _unavailable_reason = "DeepSeek 模式但未配置 brain.api_key / DEEPSEEK_API_KEY"
                log.warning(_unavailable_reason)
                return None
            try:
                from openai import OpenAI
                _ds_client = OpenAI(api_key=key, base_url=_ds_base_url, max_retries=1)
            except Exception as e:
                _unavailable_reason = f"DeepSeek 客户端初始化失败：{e}"
                log.warning(_unavailable_reason)
                return None
        return _ds_client

    # anthropic
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        try:
            import anthropic
        except ImportError as e:
            _unavailable_reason = f"anthropic SDK 未安装：{e}"
            log.warning(_unavailable_reason)
            return None
        try:
            _client = anthropic.Anthropic(max_retries=1)
        except Exception as e:
            _unavailable_reason = f"客户端初始化失败：{e}"
            log.warning(_unavailable_reason)
            return None
    return _client


def available() -> bool:
    return get_client() is not None


def unavailable_reason() -> str | None:
    if provider() == "deepseek":
        if get_client() is not None:
            return None
        return _unavailable_reason or "DeepSeek 不可用"
    if _client is not None:
        return None
    if _unavailable_reason:
        return _unavailable_reason
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return "未设置 ANTHROPIC_API_KEY（额度耗尽可切 brain.provider=deepseek）"
    return None


# ---------------------------------------------------------------- Prompt

SUGGEST_SYSTEM = """你是 Super Mouse——一只住在用户光标里的老鼠助手。用户把光标停在了某个应用或文件上，\
你要根据 <profile> 里对用户工作的理解和当前屏幕截图，提出【一个】此刻最有价值、可以立刻代劳的行动。

要求：
- title 是一句 ≤ 20 字的中文问句，口吻像可爱的小助手，例如"要我起草这 3 封邮件的回复吗？"
- 优先选择 profile 中用户对该应用的明确期望；没有则根据屏幕内容推断
- skill 只能填这些内置技能之一，或 null：pages_stock_report / mail_draft_replies /
  vscode_open_todo / notes_daily_report / browser_morning_tabs / summarize_file
- 不确定的信息不要编造（比如看不到邮件数量就不要写数字）
- 任何发送、删除、支付、发布类动作 needs_confirm 必须为 true"""

AGENT_SYSTEM = """你是 Super Mouse 的执行大脑，通过工具在用户的 macOS 上完成一个已被用户确认的小任务。

原则：
1. 先看后做：不确定屏幕状态时先用 see_screen 确认，再操作。
2. 最小动作：能用 AppleScript 就不模拟按键，能一步完成就不分两步。
3. 只到草稿：邮件/消息只创建草稿，绝不发送；文件只新建不删除；确需不可逆动作先 ask_confirm。
4. 每完成一步调用 notify 告诉用户进度（≤ 15 字）。
5. 遇到登录框、密码框、验证码：立即停止并用 notify 说明，绝不输入凭据。
6. 结束时用一句 ≤ 30 字的中文总结你做了什么。"""

PROFILE_SYSTEM = """你在为一个叫 Super Mouse 的桌面助手整理用户画像。用户提供了一些关于自己工作的材料，\
请生成一份 Markdown 格式的 profile.md（≤ 3000 字），包含：

## 身份与角色
## 当前项目
## 各应用的使用习惯与期望（每个应用一节，写明"用户希望在这个应用里被代劳什么"）
## 明确禁止事项

只输出 Markdown，不要额外说明。材料里没提到的不要编造。"""

_SUMMARY_SYSTEM = "你是一个简洁的中文摘要助手。用 3 句话以内总结用户给的文档，直接给结论。"

_SUGGEST_JSON_SPEC = ('输出 JSON：{"title": str, "skill": str|null, "args": {}, '
                      '"steps": [str], "needs_confirm": bool}。只输出 JSON，不要别的文字。')


# ---------------------------------------------------------------- 调用


def suggest_from_screen(profile_md: str, hover: dict, screenshot_png: bytes | None,
                        timeout: float = 10.0) -> Suggestion | None:
    """慢路径：结合截图与画像生成建议。失败返回 None（上层退化到快路径）。"""
    if provider() == "deepseek":
        return _ds_suggest(profile_md, hover, screenshot_png, timeout)
    client = get_client()
    if client is None:
        return None

    content: list[dict] = []
    if screenshot_png:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": base64.standard_b64encode(screenshot_png).decode()},
        })
    content.append({
        "type": "text",
        "text": (f"用户光标悬停在：{hover}\n"
                 f"给出一个最有价值的建议。"),
    })

    try:
        resp = client.with_options(timeout=timeout).messages.parse(
            model=MODEL,
            max_tokens=2048,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},        # 悬停建议要快
            system=[
                {"type": "text", "text": SUGGEST_SYSTEM},
                {"type": "text", "text": f"<profile>\n{profile_md}\n</profile>",
                 "cache_control": {"type": "ephemeral"}},   # profile 稳定 → 缓存
            ],
            messages=[{"role": "user", "content": content}],
            output_format=Suggestion,
        )
    except Exception as e:
        log.warning("建议生成失败（退化到快路径）：%s", e)
        return None

    if getattr(resp, "stop_reason", None) == "refusal":
        log.warning("模型拒绝了这个请求")
        return None
    return resp.parsed_output


def _ds_suggest(profile_md, hover, screenshot_png, timeout) -> Suggestion | None:
    client = get_client()
    if client is None:
        return None
    system = f"{SUGGEST_SYSTEM}\n<profile>\n{profile_md}\n</profile>\n{_SUGGEST_JSON_SPEC}"
    user = f"用户光标悬停在：{json.dumps(hover, ensure_ascii=False)}\n给出一个最有价值的建议。"

    def call(with_image: bool):
        content: list = [{"type": "text", "text": user}]
        if with_image and screenshot_png:
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/png;base64,"
                                       + base64.standard_b64encode(screenshot_png).decode()}})
        return client.with_options(timeout=timeout).chat.completions.create(
            model=_ds_model, max_tokens=1024, temperature=0.3,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": content}],
        )

    try:
        try:
            resp = call(True)
        except Exception:               # 模型可能不支持视觉 → 纯文本重试
            resp = call(False)
        raw = (resp.choices[0].message.content or "").strip()
        data = json.loads(raw)
    except Exception as e:
        log.warning("DeepSeek 建议生成失败（退化到快路径）：%s", e)
        return None

    try:
        return Suggestion(
            title=str(data.get("title") or "")[:30] or None,
            skill=data.get("skill") or None,
            args=data.get("args") or {},
            steps=[str(s) for s in (data.get("steps") or [])][:5],
            needs_confirm=bool(data.get("needs_confirm")),
        )
    except Exception:
        return None


def vision_answer(png: bytes, question: str, timeout: float = 20.0) -> str:
    """看屏幕回答问题。给 Agent 的 see_screen 工具用，所以必须返回字符串。"""
    if provider() == "deepseek":
        client = get_client()
        if client is None:
            return "无法看屏幕：DeepSeek 不可用"
        try:
            resp = client.with_options(timeout=timeout).chat.completions.create(
                model=_ds_model, max_tokens=1024,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64,"
                                  + base64.standard_b64encode(png).decode()}},
                ]}],
            )
            return (resp.choices[0].message.content or "").strip() or "没看出来"
        except Exception as e:
            return f"看屏幕失败（当前后端可能不支持视觉）：{e}"

    client = get_client()
    if client is None:
        return "无法看屏幕：Claude 不可用"
    try:
        resp = client.with_options(timeout=timeout).messages.create(
            model=MODEL,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.standard_b64encode(png).decode()}},
                {"type": "text", "text": question},
            ]}],
        )
    except Exception as e:
        return f"看屏幕失败：{e}"
    return _text_of(resp) or "没看出来"


def summarize_text(text: str, name: str = "文档", timeout: float = 30.0) -> str | None:
    """总结一段文本。断网返回 None，调用方退化成显示文件信息。"""
    client = get_client()
    if client is None:
        return None
    try:
        if provider() == "deepseek":
            resp = client.with_options(timeout=timeout).chat.completions.create(
                model=_ds_model, max_tokens=1024, temperature=0.3,
                messages=[{"role": "system", "content": _SUMMARY_SYSTEM},
                          {"role": "user",
                           "content": f'<doc name="{name}">\n{text[:60000]}\n</doc>'}],
            )
            return (resp.choices[0].message.content or "").strip() or None

        resp = client.with_options(timeout=timeout).messages.create(
            model=MODEL,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=_SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": f'<doc name="{name}">\n{text[:60000]}\n</doc>'}],
        )
    except Exception as e:
        log.warning("总结失败：%s", e)
        return None
    return _text_of(resp)


def build_profile(raw_text: str, timeout: float = 120.0) -> str | None:
    """喂养：把原始材料整理成 profile.md。"""
    client = get_client()
    if client is None:
        return None
    try:
        if provider() == "deepseek":
            resp = client.with_options(timeout=timeout).chat.completions.create(
                model=_ds_model, max_tokens=8000, temperature=0.3,
                messages=[{"role": "system", "content": PROFILE_SYSTEM},
                          {"role": "user",
                           "content": f"<materials>\n{raw_text}\n</materials>\n请生成 profile.md。"}],
            )
            return (resp.choices[0].message.content or "").strip() or None

        with client.with_options(timeout=timeout).messages.stream(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=PROFILE_SYSTEM,
            messages=[{"role": "user",
                       "content": f"<materials>\n{raw_text}\n</materials>\n请生成 profile.md。"}],
        ) as stream:
            msg = stream.get_final_message()
    except Exception as e:
        log.warning("生成画像失败：%s", e)
        return None
    return _text_of(msg)


def _text_of(msg) -> str | None:
    parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
    out = "".join(parts).strip()
    return out or None
