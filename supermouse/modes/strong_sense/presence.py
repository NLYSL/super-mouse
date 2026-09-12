"""radar.frame → sense.intruder / sense.clear，事件字段保持兼容。

以目标数为主判据：用户坐在探测区内 = 1 个目标，来人 = 第 2 个（阈值 2）。
两个实测修过的坑：
  · 来人代表不再套 zone 下限 —— 本人 0.5m、来人站近处 0.6–0.8m 时会被挡掉；
  · min_frames 按滑窗（window_s 内累计）而非连续 —— 走动目标会被雷达
    间歇性丢失，连续计数永远凑不齐。
距离门只保留远端上限排除远处目标；目标槽位不是身份 ID。
"""

from __future__ import annotations

import logging
import math
import time

log = logging.getLogger(__name__)


class IntruderDetector:
    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        zone = cfg.get("sense.zone_m") or [0.8, 4.0]
        self.zone = (float(zone[0]), float(zone[1]))
        self.target_count = int(cfg.get("sense.intruder_target_count", 2))
        if not 1 <= self.target_count <= 3:
            raise ValueError("sense.intruder_target_count 必须为 1–3")
        self.min_frames = max(1, int(cfg.get("sense.min_frames", 3)))
        self.window_s = float(cfg.get("sense.window_s", 2.0))
        self.max_frame_gap = float(cfg.get("sense.max_frame_gap_s", 0.5))
        self.approach = float(cfg.get("sense.approach_speed_mps", 0.3))
        self.cooldown = float(cfg.get("sense.cooldown_s", 20))
        self.clear_s = float(cfg.get("sense.clear_s", 10))
        # 只用于旧的单目标协议；多目标模式不排除本人后再重复数两个人。
        self.facing = str(cfg.get("devices.radar.facing", "forward"))
        self.energy_gate = int(cfg.get("sense.energy_gate", 30))
        self.user_dist_m = float(cfg.get("sense.user_dist_m", 1.2))

        self.recent: list[float] = []          # 窗口内"人数达标"帧的时间戳
        self.last_trigger = -math.inf
        self.active = False
        self.last_frame: float | None = None
        self.absent_since: float | None = None
        self.track: list[tuple[float, float]] = []

    def install(self) -> None:
        self.bus.subscribe("radar.frame", self.on_frame)
        self.bus.subscribe("radar.disconnected", self._reset_evidence)
        self.bus.subscribe("sense.clear", self._on_clear)

    def _on_clear(self, evt: dict) -> None:
        self.active = False
        self._reset_evidence()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("sense.enabled", True))

    def _reset_evidence(self, evt=None) -> None:
        self.recent.clear()
        self.track.clear()
        self.last_frame = None
        self.absent_since = None
        # 断线不等于无人，不发送 sense.clear，也不允许下一帧沿用旧确认计数。

    def on_frame(self, evt: dict) -> None:
        if not self.enabled:
            self._reset_evidence()
            return
        t = time.time() if evt.get("t") is None else float(evt["t"])
        if self.last_frame is not None and (
                t < self.last_frame or t - self.last_frame > self.max_frame_gap):
            self._reset_evidence()
        self.last_frame = t
        d, approaching = self._candidate(evt)

        if d is not None:
            self.absent_since = None
            # 滑窗计数而非"连续"计数：走动的人会被雷达间歇性丢目标，
            # 一闪断就清零的话，走近过程永远凑不齐 min_frames（实测踩过）。
            self.recent = [tt for tt in self.recent if t - tt <= self.window_s] + [t]
            # 旧协议没有目标速度，仅此时才估算距离变化；多目标不跨槽位算速度。
            if evt.get("targets") is None:
                self.track = [(tt, dd) for tt, dd in self.track if t - tt <= 1.0] + [(t, d)]
                if len(self.track) >= 3:
                    span = t - self.track[0][0]
                    if span > 1e-3:
                        approaching = (self.track[0][1] - d) / span > self.approach
            if (not self.active and len(self.recent) >= self.min_frames
                    and t - self.last_trigger >= self.cooldown):
                self.last_trigger = t
                self.active = True
                log.info("检测到来人：目标阈值=%d，%.2f m（接近=%s，%d帧/%.1fs）",
                         self.target_count, d, approaching, len(self.recent), self.window_s)
                self.bus.publish("sense.intruder", distance_m=round(d, 2), approaching=approaching)
        else:
            # 证据随滑窗自然过期（连续缺席超过 window_s 后 recent 清空），
            # 这里只维护缺席起点；断续丢帧不清空证据。
            self.recent = [tt for tt in self.recent if t - tt <= self.window_s]
            self.track.clear()
            if self.absent_since is None:
                self.absent_since = t
            # 第二人离开、只剩本人也算清空；必须有连续帧证据，串口静默不算。
            if self.active and t - self.absent_since >= self.clear_s:
                self.active = False
                log.info("来人已离开")
                self.bus.publish("sense.clear")

    def _candidate(self, f: dict) -> tuple[float | None, bool]:
        targets = f.get("targets")
        if targets is not None:
            candidates = []
            for target in targets:
                d = float(target["dist_mm"]) / 1000.0
                # 下限不用于人数统计：本人可能坐在 0.8 m 内。
                if math.isfinite(d) and 0 < d <= self.zone[1]:
                    candidates.append((d, float(target.get("speed_cms", 0))))
            candidates.sort(key=lambda item: item[0])
            if len(candidates) < self.target_count:
                return None, False
            # 近处 target_count-1 人作为基线；基线之后最近的就是来人代表。
            # 不再对代表套 zone 下限：本人坐在 0.5m 时，来人站到旁边往往只有
            # 0.6–0.8m，0.8m 的下限会把"走到近处站定"这个最典型场景全部挡掉（实测踩过）。
            # 代表天然排在基线之后，无需再用距离下限区分本人。
            d, speed = candidates[self.target_count - 1]
            return d, speed < -self.approach * 100

        # 单目标协议没有人数信息，不能把 moving/static 当成两个人。
        if self.target_count != 1:
            return None, False
        if f.get("state") in ("moving", "both"):
            if int(f.get("moving_energy", 0)) < self.energy_gate:
                return None, False
            d = float(f.get("moving_cm", 0)) / 100.0
            if self.facing == "forward" and d < self.user_dist_m:
                return None, False
            if self.zone[0] <= d <= self.zone[1]:
                return d, False
        return None, False
