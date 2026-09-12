"""事件总线：所有模块之间唯一的通信方式。

设计要点
- publish() 可以从任意线程调用（内部用 call_soon_threadsafe 转到事件循环）
- 按 type 前缀订阅："bci.*" / "radar.frame" / "*"
- 可选把所有事件录制成 JSONL，用 tools/replay.py 回放
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, AsyncIterator, Callable

log = logging.getLogger(__name__)

# 这些事件量太大，默认不写进录制文件（除非 --record-raw）
HIGH_VOLUME = {"bci.raw"}


class EventBus:
    def __init__(self, record_path: str | Path | None = None, record_raw: bool = False):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._queues: list[tuple[str, asyncio.Queue]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread_id = threading.get_ident()
        self._record_raw = record_raw
        self._fh = None
        self._count = 0
        if record_path:
            p = Path(record_path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            self._fh = p.open("a", encoding="utf-8")
            log.info("录制事件到 %s", p)

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """在事件循环启动后调用一次，之后才支持跨线程 publish。"""
        self._loop = loop
        self._thread_id = threading.get_ident()

    # ---------- 发布 ----------

    def publish(self, type: str, **payload: Any) -> None:
        """发布一个事件。线程安全。"""
        evt = {"type": type, "t": payload.pop("t", None) or time.time(), **payload}
        if self._loop is not None and threading.get_ident() != self._thread_id:
            self._loop.call_soon_threadsafe(self._dispatch, evt)
        else:
            self._dispatch(evt)

    def _dispatch(self, evt: dict) -> None:
        etype = evt["type"]
        self._count += 1
        if self._fh and (etype not in HIGH_VOLUME or self._record_raw):
            self._fh.write(json.dumps(evt, ensure_ascii=False) + "\n")
            if self._count % 50 == 0:
                self._fh.flush()

        for pattern, handlers in list(self._handlers.items()):
            if not _match(pattern, etype):
                continue
            for h in list(handlers):
                try:
                    r = h(evt)
                    if asyncio.iscoroutine(r):
                        asyncio.ensure_future(r)
                except Exception:
                    log.exception("事件处理器出错 pattern=%s type=%s", pattern, etype)

        for pattern, q in list(self._queues):
            if _match(pattern, etype):
                try:
                    q.put_nowait(evt)
                except asyncio.QueueFull:
                    log.warning("订阅队列已满，丢弃 %s", etype)

    # ---------- 订阅 ----------

    def subscribe(self, pattern: str, handler: Callable[[dict], Any]) -> Callable[[], None]:
        """注册处理器（可以是普通函数或 async 函数）。返回取消订阅的函数。"""
        self._handlers[pattern].append(handler)

        def unsubscribe() -> None:
            try:
                self._handlers[pattern].remove(handler)
            except ValueError:
                pass

        return unsubscribe

    async def stream(self, pattern: str, maxsize: int = 512) -> AsyncIterator[dict]:
        """async for evt in bus.stream("bci.blink"): ..."""
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        entry = (pattern, q)
        self._queues.append(entry)
        try:
            while True:
                yield await q.get()
        finally:
            try:
                self._queues.remove(entry)
            except ValueError:
                pass

    async def wait_for(self, pattern: str, timeout: float | None = None,
                       predicate: Callable[[dict], bool] | None = None) -> dict | None:
        """等待下一个匹配事件，超时返回 None。"""
        async def _wait() -> dict:
            async for evt in self.stream(pattern):
                if predicate is None or predicate(evt):
                    return evt
            raise RuntimeError("unreachable")

        try:
            return await asyncio.wait_for(_wait(), timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        if self._fh:
            self._fh.flush()
            self._fh.close()
            self._fh = None


def _match(pattern: str, etype: str) -> bool:
    if pattern == "*":
        return True
    if pattern == etype:
        return True
    return fnmatch.fnmatchcase(etype, pattern)
