"""WebSocket 广播：把总线事件推给 tools/dashboard.html。

Demo 时把面板投到第二块屏幕，评委能看到"脑电 → 眨眼 → 动作"的全链路，
这是"硬件真的接上了"最有说服力的证据。

同时也是切换到 Swift Overlay 的预留接口（订阅 ws://127.0.0.1:8765 即可）。
"""

from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8765


class WSBroadcaster:
    def __init__(self, bus, host: str = HOST, port: int = PORT):
        self.bus = bus
        self.host = host
        self.port = port
        self.clients: set = set()
        self._server = None

    async def run(self) -> None:
        try:
            import websockets
        except ImportError:
            log.warning("websockets 未安装，调试面板不可用")
            return

        self.bus.subscribe("*", self._on_event)
        try:
            self._server = await websockets.serve(self._handler, self.host, self.port)
        except OSError as e:
            log.warning("WebSocket 端口 %d 占用，调试面板不可用：%s", self.port, e)
            return
        log.info("调试面板：在浏览器打开 tools/dashboard.html（ws://%s:%d）", self.host, self.port)
        try:
            await asyncio.Future()          # 一直跑
        except asyncio.CancelledError:
            self._server.close()
            raise

    async def _handler(self, ws) -> None:
        self.clients.add(ws)
        log.info("调试面板已连接（当前 %d 个）", len(self.clients))
        try:
            async for _ in ws:              # 忽略客户端消息
                pass
        except Exception:
            pass
        finally:
            self.clients.discard(ws)

    def _on_event(self, evt: dict) -> None:
        if not self.clients:
            return
        try:
            payload = json.dumps(evt, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return
        for ws in list(self.clients):
            try:
                asyncio.create_task(self._send(ws, payload))
            except RuntimeError:
                return

    async def _send(self, ws, payload: str) -> None:
        try:
            await ws.send(payload)
        except Exception:
            self.clients.discard(ws)
