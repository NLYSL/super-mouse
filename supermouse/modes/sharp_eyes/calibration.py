"""校准向导：60 秒把"自然眨眼"和"用力眨眼"分开。

四个阶段：
  1. 信号就绪   等 poor_signal 降下来
  2. 自然 20 s  正常看屏幕 → 得到自然眨眼峰值分布
  3. 意图 15 s  用力眨 10 次 → 得到意图眨眼峰值分布
  4. 节奏 15 s  快速双眨 5 次 → 得到双眨间隔分布

输出写回配置：
  eyes.calibration.abs_threshold   检测器的绝对幅度阈值（自然 P95 与意图 P25 之间）
  eyes.calibration.intent_peak     用力眨眼的典型峰值，用于把 peak 归一化成 0-1 的 strength
  eyes.calibration.hard_threshold  归一化后区分重眨的阈值
  eyes.calibration.double_gap_ms   双眨间隔窗口

校准期间 recognizer.paused = True，避免边校准边触发点击。
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

log = logging.getLogger(__name__)


class Calibrator:
    def __init__(self, cfg, bus, recognizer, bci_client=None):
        self.cfg = cfg
        self.bus = bus
        self.recognizer = recognizer
        self.bci_client = bci_client
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            log.info("校准已在进行中")
            return
        self._task = asyncio.create_task(self.run())

    def cancel(self) -> None:
        if self._task:
            self._task.cancel()

    async def run(self) -> dict | None:
        self.recognizer.paused = True
        try:
            return await self._run()
        except asyncio.CancelledError:
            self._say("校准已取消", anim="idle")
            raise
        except Exception:
            log.exception("校准失败")
            self._say("校准出错了", anim="idle")
            return None
        finally:
            self.recognizer.paused = False

    async def _run(self) -> dict | None:
        # 阶段 1：等信号
        self._say("戴好设备，等信号…", anim="thinking", ttl_ms=30000)
        if not await self._wait_quality(timeout=30.0):
            self._say("信号一直很差，检查电极", anim="idle")
            return None

        # 阶段 2：自然眨眼
        self._say("放松，正常看屏幕 20 秒", anim="idle", ttl_ms=20000)
        natural = await self._collect_peaks(20.0)

        # 阶段 3：用力眨眼
        self._say("现在用力眨眼 10 次", anim="thinking", ttl_ms=18000)
        intent = await self._collect_peaks(18.0)

        # 阶段 4：双眨节奏
        self._say("快速双眨 5 次", anim="thinking", ttl_ms=16000)
        gaps = await self._collect_gaps(16.0)

        if len(natural) < 3 or len(intent) < 3:
            self._say(f"数据不够（自然{len(natural)}/用力{len(intent)}）", anim="idle")
            log.warning("校准样本不足：natural=%d intent=%d", len(natural), len(intent))
            return None

        result = self._compute(natural, intent, gaps)
        self._write_back(result)
        overlap = result["overlap"]
        if overlap > 0.3:
            self._say(f"分布重叠 {overlap:.0%}，建议重贴电极", anim="idle", ttl_ms=5000)
        else:
            self._say("校准好了！", anim="happy", ttl_ms=2500)
        self.bus.publish("eye.calibrated", thresholds=result)
        return result

    # ---------- 采集 ----------

    async def _wait_quality(self, timeout: float) -> bool:
        """等 poor_signal ≤ 门限并稳定 2 s。没有质量事件时也放行（有些固件不发）。"""
        gate = int(self.cfg.get("eyes.quality_gate", 50))
        deadline = time.time() + timeout
        good_since: float | None = None
        got_any = False

        async for evt in self.bus.stream("bci.quality"):
            got_any = True
            if int(evt.get("poor_signal", 200)) <= gate:
                good_since = good_since or time.time()
                if time.time() - good_since >= 2.0:
                    return True
            else:
                good_since = None
            if time.time() > deadline:
                break
        return not got_any

    async def _collect_peaks(self, seconds: float) -> list[float]:
        peaks: list[float] = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            evt = await self.bus.wait_for("bci.blink", timeout=max(deadline - time.time(), 0.01))
            if evt is None:
                break
            peak = evt.get("peak")
            if peak is not None:
                peaks.append(float(peak))
        return peaks

    async def _collect_gaps(self, seconds: float) -> list[float]:
        times: list[float] = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            evt = await self.bus.wait_for("bci.blink", timeout=max(deadline - time.time(), 0.01))
            if evt is None:
                break
            times.append(float(evt.get("t") or time.time()))
        gaps = [(b - a) * 1000 for a, b in zip(times, times[1:]) if 0.05 < (b - a) < 0.9]
        return gaps

    # ---------- 计算 ----------

    def _compute(self, natural: list[float], intent: list[float],
                 gaps: list[float]) -> dict:
        nat = np.array(natural, dtype=float)
        itn = np.array(intent, dtype=float)

        nat_p95 = float(np.percentile(nat, 95))
        itn_p25 = float(np.percentile(itn, 25))
        itn_p90 = float(np.percentile(itn, 90))

        # 检测阈值取"自然 P50 的 1.5 倍"和"意图 P25 的 0.5 倍"里更保守的那个，
        # 目的是既能抓到普通眨眼（老鼠要镜像），又不把脑电噪声当眨眼
        nat_p50 = float(np.median(nat))
        abs_threshold = max(nat_p50 * 0.6, min(nat_p50 * 1.2, itn_p25 * 0.45))

        intent_peak = itn_p90
        # 重眨阈值：归一化到 intent_peak 之后，落在自然 P95 和意图 P25 中间
        hard = ((nat_p95 + itn_p25) / 2) / intent_peak if intent_peak > 0 else 0.55
        hard = float(min(max(hard, 0.35), 0.9))

        # 分布重叠度：自然里有多少超过了重眨门限
        overlap = float((nat >= hard * intent_peak).mean())

        if len(gaps) >= 3:
            g = np.array(gaps)
            lo = float(max(np.percentile(g, 5) * 0.8, 100))
            hi = float(min(np.percentile(g, 95) * 1.2, 550))
            if hi - lo < 120:
                hi = lo + 120
        else:
            lo, hi = 120.0, 450.0

        return {
            "abs_threshold": round(abs_threshold, 2),
            "intent_peak": round(intent_peak, 2),
            "hard_threshold": round(hard, 3),
            "double_gap_ms": [round(lo), round(hi)],
            "natural_p95": round(nat_p95, 2),
            "intent_p25": round(itn_p25, 2),
            "overlap": round(overlap, 3),
            "samples": {"natural": len(natural), "intent": len(intent), "gaps": len(gaps)},
        }

    def _write_back(self, r: dict) -> None:
        for key in ("abs_threshold", "intent_peak", "hard_threshold", "double_gap_ms"):
            self.cfg.set(f"eyes.calibration.{key}", r[key])
        self.cfg.save()
        if self.bci_client is not None:
            self.bci_client.recalibrate(r["abs_threshold"], r["intent_peak"])
        log.info("校准结果：%s", r)

    def _say(self, text: str, anim: str = "idle", ttl_ms: int = 3000) -> None:
        self.bus.publish("mouse.state", anim=anim, label=text, ttl_ms=ttl_ms)
        log.info("[校准] %s", text)
