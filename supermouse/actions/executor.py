"""动作执行器：订阅 action.* 事件 → 调用 macos.py。

把"决定做什么"（模式层）和"怎么做"（macOS API）分开，
这样模式层可以在没有 pyobjc 的机器上单测。
"""

from __future__ import annotations

import asyncio
import logging

from . import macos

log = logging.getLogger(__name__)


class ActionExecutor:
    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        self.stopped = False

    def install(self) -> None:
        self.bus.subscribe("action.*", self._on_action)
        self.bus.subscribe("sys.stop_all", self._on_stop)

    def _on_stop(self, evt: dict) -> None:
        self.stopped = True
        macos.notify("Super Mouse", "已停止所有动作")
        log.warning("全局急停")
        # 0.5 s 后解除，避免永久锁死
        try:
            asyncio.get_running_loop().call_later(0.5, self._clear_stop)
        except RuntimeError:
            self.stopped = False

    def _clear_stop(self) -> None:
        self.stopped = False

    def _on_action(self, evt: dict) -> None:
        kind = evt["type"].split(".", 1)[1]
        if kind == "done":
            return
        if self.stopped:
            log.info("急停中，忽略 action.%s", kind)
            return
        try:
            self._dispatch(kind, evt)
            self.bus.publish("action.done", ref=kind, ok=True)
        except Exception as e:
            log.exception("执行 action.%s 失败", kind)
            self.bus.publish("action.done", ref=kind, ok=False, error=str(e))

    def _dispatch(self, kind: str, evt: dict) -> None:
        if kind == "click":
            macos.click(button=evt.get("button", "left"), count=int(evt.get("count", 1)))
        elif kind == "keys":
            macos.press_keys(evt["combo"])
        elif kind == "type":
            macos.type_text(evt["text"])
        elif kind == "scroll":
            macos.scroll(int(evt.get("dy", 0)), int(evt.get("dx", 0)))
        elif kind == "activate":
            macos.activate(evt["app"])
        elif kind == "hide_app":
            macos.hide_app(evt["app"])
        elif kind == "mute":
            macos.mute(bool(evt.get("on", True)))
        elif kind == "open_url":
            macos.open_url(evt["url"])
        elif kind == "notify":
            macos.notify(evt.get("title", "Super Mouse"), evt.get("message", ""))
        elif kind == "scene":
            self._run_scene(evt["scene"])
        else:
            log.warning("未知动作：action.%s", kind)

    def _run_scene(self, name: str) -> None:
        """执行配置里的动作序列，比如 sense.work_scene。"""
        steps = self.cfg.get(f"sense.{name}_scene") or self.cfg.get(f"scenes.{name}") or []
        if not steps:
            log.warning("场景 %s 未配置", name)
            return
        for step in steps:
            do = step.get("do")
            if do == "hide_apps":
                which = step.get("apps")
                apps = self.cfg.get(f"sense.{which}", []) if isinstance(which, str) else (which or [])
                running = macos.running_bundles()
                for app in apps:
                    if app in running:
                        macos.hide_app(app)
            elif do == "activate":
                macos.activate(step["app"])
            elif do == "mute":
                macos.mute(bool(step.get("on", True)))
            elif do == "space":
                macos.switch_space(int(step["index"]))
            elif do == "open_url":
                macos.open_url(step["url"])
            elif do == "keys":
                macos.press_keys(step["combo"])
            elif do == "hide_others":
                macos.hide_others()
            else:
                log.warning("场景 %s 里有未知步骤：%s", name, do)
