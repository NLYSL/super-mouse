"""SHARP EYES —— 眼势模式。

把 bci.blink / bci.quality 变成 eye.gesture，再按映射表变成 action.*。
"""

from __future__ import annotations

import logging

from .calibration import Calibrator
from .gestures import GestureRecognizer
from .mapping import GestureMapper

log = logging.getLogger(__name__)


class SharpEyes:
    def __init__(self, cfg, bus, bci_client=None):
        self.cfg = cfg
        self.bus = bus
        self.recognizer = GestureRecognizer(cfg, bus)
        self.mapper = GestureMapper(cfg, bus)
        self.calibrator = Calibrator(cfg, bus, self.recognizer, bci_client)

    def install(self) -> None:
        self.bus.subscribe("bci.blink", self.recognizer.on_blink)
        self.bus.subscribe("bci.quality", self.recognizer.on_quality)
        self.bus.subscribe("eye.calibrated", lambda e: self.recognizer.reload())
        self.mapper.install()
        self.bus.subscribe("ui.calibrate", lambda e: self.calibrator.start())
        log.info("SHARP EYES 已就绪（enabled=%s）", self.cfg.get("eyes.enabled", True))
