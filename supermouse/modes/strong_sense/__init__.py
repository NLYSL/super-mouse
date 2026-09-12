"""STRONG SENSE —— 身后有人就切回工作页面。

雷达硬件尚未接入；判定逻辑（presence）与执行逻辑（scenes）已完成，
当前由模拟器热键 F16/F17 驱动，雷达插上后自动切到真实数据。
"""

from __future__ import annotations

import logging

from .presence import IntruderDetector
from .scenes import SceneSwitcher

log = logging.getLogger(__name__)


class StrongSense:
    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        self.detector = IntruderDetector(cfg, bus)
        self.switcher = SceneSwitcher(cfg, bus)

    def install(self) -> None:
        self.detector.install()
        self.switcher.install()
        source = "真实雷达" if self.cfg.get("devices.radar.enabled", False) else "模拟器（F16/F17）"
        log.info("STRONG SENSE 已就绪，数据源：%s", source)
