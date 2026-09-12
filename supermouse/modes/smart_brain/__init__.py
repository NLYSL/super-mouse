"""SMART BRAIN —— 懂你工作的老鼠。

流程：
    brain.hover  悬停识别
      ├─ 快路径：plans.json / DEFAULT_PLANS 里有该应用的计划 → 立刻举牌（0 延迟）
      └─ 慢路径：截图 + profile → Claude 生成建议（≤ 2 s，失败退回快路径）
    brain.suggest 老鼠举牌
    用户重眨 / 点牌子 → brain.confirm
    brain.action  执行（内置技能优先，纯 AppleScript，断网也能跑）
    mouse.state(happy) 完成

执行放在线程池里，绝不阻塞 Qt 主线程（否则老鼠会僵住）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from ...actions import macos
from . import client, skills
from .hover import HoverWatcher
from .plans import resolve

log = logging.getLogger(__name__)


class SmartBrain:
    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        self.hover = HoverWatcher(cfg, bus)

        self.profile_md = ""
        self.plans: dict[str, dict] = dict(skills.DEFAULT_PLANS)
        self.pending: dict | None = None          # 当前举着的牌子
        self.running: asyncio.Task | None = None
        self.ctx: skills.SkillContext | None = None
        self._suggest_task: asyncio.Task | None = None
        self._worker = None

    # ---------- 生命周期 ----------

    def install(self) -> None:
        client.configure(self.cfg)      # 后端选择（anthropic / deepseek）
        self._load_knowledge()
        self.bus.subscribe("brain.hover", self._on_hover)
        self.bus.subscribe("brain.cancel", self._on_cancel)
        self.bus.subscribe("ui.confirm_gesture", self._on_confirm)
        self.bus.subscribe("ui.click_suggest", self._on_confirm)
        self.bus.subscribe("sys.stop_all", self._on_stop)
        self.bus.subscribe("sense.intruder", self._on_stop)   # 有人来了先停手
        self.bus.subscribe("brain.settings_changed", self._settings_changed)
        self.bus.subscribe("config.changed", self._config_changed)
        self.bus.subscribe("brain.test", self._test)
        self.bus.subscribe("brain.hover_left", self._hover_left)
        self.hover.start()

        mode = "在线" if client.available() else f"离线（{client.unavailable_reason()}）"
        log.info("SMART BRAIN 已就绪，%s，已加载 %d 个应用计划", mode, len(self.plans))

    def stop(self) -> None:
        self.hover.stop()
        self._on_stop({})

    def _load_knowledge(self) -> None:
        d = self.cfg.path_of("brain.knowledge_dir", "~/.supermouse/brain")
        if not d:
            return
        d.mkdir(parents=True, exist_ok=True)

        pm = d / "profile.md"
        if pm.exists():
            self.profile_md = pm.read_text(encoding="utf-8", errors="ignore")
            log.info("已加载画像 %d 字", len(self.profile_md))

        self.plans = resolve(self.cfg)

    def _hover_left(self, evt):
        # Keep the card until TTL so the pointer can travel from Dock to it.
        if self._suggest_task and not self._suggest_task.done():
            self._suggest_task.cancel()

    def _settings_changed(self, evt):
        self._on_cancel({})
        self.bus.publish("brain.cancel")
        self._load_knowledge()
        self.hover._recent.clear()
        self.hover._fired_at = None

    def _config_changed(self, evt):
        if evt.get("key") == "brain.enabled" and not self.cfg.get("brain.enabled", True):
            self._on_stop(evt)
            self.bus.publish("brain.cancel")

    def _busy(self):
        return ((self.running is not None and not self.running.done()) or
                (self._worker is not None and not self._worker.done()))

    def _test(self, evt):
        if not self.cfg.get("brain.enabled", True) or self._busy():
            self.bus.publish("brain.action", status="error", message="请先开启 Brain，并等待当前任务结束")
            return
        self._hover_left({})
        self.plans = resolve(self.cfg)
        plan = self.plans.get(evt.get("app"))
        if not plan:
            self.bus.publish("brain.action", status="error", message="未找到任务配置")
            return
        self._offer(plan, {"app": evt["app"], "kind": "manual"})
        self._on_confirm({"via": "test"})

    # ---------- 悬停 → 建议 ----------

    def _on_hover(self, evt: dict) -> None:
        if not self.cfg.get("brain.enabled", True) or self._busy():
            return                                # 正在干活，不打扰
        self._on_cancel({})
        self.bus.publish("brain.cancel")
        self.plans = resolve(self.cfg)
        app = evt.get("app")
        kind = evt.get("kind")

        plan = self.plans.get(app) if app else None
        if plan:
            self._offer(plan, evt)
            return

        # Finder 桌面图标/替身：按名字解析目标 App，命中预设（Pages 演示场景）
        if kind == "finder_item" and evt.get("title"):
            bundle = macos.resolve_finder_target(str(evt["title"]))
            if bundle and bundle in self.plans:
                self._offer(self.plans[bundle], evt)
                return

        # 文件悬停：先看路径能否映射到有预设的应用（.app / .pages / 替身）
        if kind == "file" and evt.get("path"):
            bundle = macos.bundle_for_path(evt["path"])
            if not bundle:
                # 替身文件本身没有后缀 —— 按文件名解析它指向哪个应用
                from pathlib import Path as _P
                bundle = macos.resolve_finder_target(_P(str(evt["path"])).name)
            if bundle and bundle in self.plans:
                self._offer(self.plans[bundle], evt)
                return
            self._offer({
                "title": "要我总结这个文档吗？",
                "skill": "summarize_file",
                "args": {"path": evt["path"]},
                "steps": ["读取文件", "生成摘要", "通知展示"],
                "needs_confirm": False,
            }, evt)
            return

        if self.cfg.get("brain.slow_path", True) and client.available():
            self._suggest_task = asyncio.create_task(self._slow_path(evt))

    async def _slow_path(self, hover: dict) -> None:
        """截图 + 画像 → Claude 生成建议。"""
        self.bus.publish("mouse.state", anim="thinking", ttl_ms=8000)
        loop = asyncio.get_running_loop()
        try:
            png = await loop.run_in_executor(None, macos.screenshot_bytes, 1280)
            sug = await loop.run_in_executor(
                None, client.suggest_from_screen, self.profile_md, hover, png, 8.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("慢路径出错")
            sug = None

        if sug is None:
            self.bus.publish("mouse.state", anim="idle", ttl_ms=100)
            return
        self._offer({
            "title": sug.title,
            "skill": sug.skill,
            "args": sug.args or {},
            "steps": sug.steps or [],
            "needs_confirm": sug.needs_confirm,
        }, hover)

    def _offer(self, plan: dict, hover: dict) -> None:
        plan_id = uuid.uuid4().hex[:8]
        self.pending = {**plan, "plan_id": plan_id, "hover": hover, "t": time.time()}
        self.bus.publish("mouse.state", anim="suggest", ttl_ms=None)
        self.bus.publish("brain.suggest", plan_id=plan_id, app=hover.get("app"),
                         title=plan["title"], steps=plan.get("steps", []),
                         source=plan.get("source", "AI 建议"), skill=plan.get("skill"))

        ttl = float(self.cfg.get("brain.suggest_ttl_ms", 4000)) / 1000.0
        asyncio.get_running_loop().call_later(ttl, self._expire, plan_id)

    def _expire(self, plan_id: str) -> None:
        if self.pending and self.pending["plan_id"] == plan_id:
            self.pending = None
            self.bus.publish("brain.cancel", plan_id=plan_id)
            self.bus.publish("mouse.state", anim="idle", ttl_ms=100)

    def _on_cancel(self, evt: dict) -> None:
        if self._suggest_task and not self._suggest_task.done():
            self._suggest_task.cancel()
        pid = evt.get("plan_id")
        if self.pending and (pid is None or self.pending["plan_id"] == pid):
            self.pending = None

    # ---------- 确认 → 执行 ----------

    def _on_confirm(self, evt: dict) -> None:
        if not self.cfg.get("brain.enabled", True) or self._busy():
            return
        plan = self.pending
        if plan is None:
            return
        if plan.get("confirmation") == "click" and evt.get("type") == "ui.confirm_gesture":
            return
        pid = evt.get("plan_id")
        if pid and pid != plan["plan_id"]:
            return
        self.pending = None

        # Strong Sense 的"回去继续？"复用了同一套牌子机制
        if plan["plan_id"] == "__restore__":
            self.bus.publish("brain.confirm", plan_id="__restore__", via=evt.get("via", "click"))
            self.bus.publish("ui.restore")
            return

        self.bus.publish("brain.confirm", plan_id=plan["plan_id"],
                         via=evt.get("via", "click"))
        if self.running and not self.running.done():
            log.info("上一个任务还在跑，忽略")
            return
        self.running = asyncio.create_task(self._execute(plan))

    async def _execute(self, plan: dict) -> None:
        pid = plan["plan_id"]
        ctx = skills.SkillContext(self.bus, pid, self.cfg)
        self.ctx = ctx
        self.bus.publish("mouse.state", anim="working", ttl_ms=None)
        loop = asyncio.get_running_loop()

        try:
            fn = skills.REGISTRY.get(plan.get("skill") or "")
            if fn is None:
                if plan.get("skill") not in (None, "", "agent"):
                    raise ValueError(f"未知技能：{plan.get('skill')}")
                result = await self._run_agent(plan)
            else:
                self._worker = loop.run_in_executor(None, lambda: fn(ctx, **(plan.get("args") or {})))
                self._worker.add_done_callback(self._consume_worker_exception)
                result = await asyncio.shield(self._worker)
            ctx.check()
        except asyncio.CancelledError:
            ctx.cancelled = True
            self.bus.publish("brain.action", plan_id=pid, status="error", message="已中止")
            self.bus.publish("mouse.state", anim="idle", ttl_ms=200)
            raise
        except skills.SkillCancelled:
            self.bus.publish("brain.action", plan_id=pid, status="error", message="已中止")
            self.bus.publish("mouse.state", anim="idle", ttl_ms=200)
            return
        except Exception as e:
            log.exception("执行失败")
            self.bus.publish("brain.action", plan_id=pid, status="error", message=f"出错了：{e}"[:300])
            self.bus.publish("mouse.state", anim="idle", ttl_ms=2000)
            return

        if not isinstance(result, skills.SkillResult):
            result = skills.SkillResult("done", str(result))
        self.bus.publish("brain.action", plan_id=pid, status=result.status, message=result.message,
                         artifact_path=result.artifact_path, opened=result.opened)
        self.bus.publish("mouse.state", anim="happy" if result.status == "done" else "idle",
                         label=result.message, ttl_ms=3000)
        log.info("任务 %s：%s", result.status, result.message)

    async def _run_agent(self, plan: dict) -> str:
        """没有内置技能时，让 Claude 用工具自由组合。"""
        if not client.available():
            raise RuntimeError("AI 自定义任务不可用：" + client.unavailable_reason())
        from .agent import run_plan
        loop = asyncio.get_running_loop()
        self._worker = loop.run_in_executor(None, run_plan, self.profile_md, plan, self.ctx)
        self._worker.add_done_callback(self._consume_worker_exception)
        return await asyncio.shield(self._worker)

    @staticmethod
    def _consume_worker_exception(worker):
        # A cancelled asyncio wrapper cannot stop its thread. Retrieve any late
        # exception while keeping the worker busy until it actually finishes.
        if not worker.cancelled():
            worker.exception()

    # ---------- 急停 ----------

    def _on_stop(self, evt: dict) -> None:
        if self.ctx:
            self.ctx.cancelled = True
        if self._suggest_task and not self._suggest_task.done():
            self._suggest_task.cancel()
        if self.running and not self.running.done():
            self.running.cancel()
        self.pending = None
