"""脑机串口客户端。

实测：脑机通过 USB 接收器（VID 0x1a86 / PID 0x5722 "USBDV"）暴露为 CDC 虚拟串口，
波特率不影响数据（CDC 直通），但 pyserial 仍需要一个值。

在独立线程里阻塞读串口，解析后通过 bus.publish 跨线程投递到事件循环。
输出事件：
  bci.connected   {port, protocol, fs}
  bci.disconnected {reason}
  bci.quality     {poor_signal}
  bci.raw         {fs, samples[]}         每 CHUNK 个样本一次
  bci.blink       {strength, duration_ms, source}
  bci.attention   {value}
  bci.meditation  {value}
  bci.eeg_power   {bands}
"""

from __future__ import annotations

import logging
import threading
import time

import serial

from ..serial_ports import is_bci_port
from .blink_detector import BlinkDetector
from .thinkgear import ThinkGearParser

log = logging.getLogger(__name__)

# 每收集这么多原始样本处理一次（250 Hz 下 ≈ 128 ms 延迟，眨眼检测足够）
CHUNK = 32

def find_port() -> str | None:
    """只自动匹配实测脑机身份；不把同 VID 的 CH340K/原生 USB 当脑机。

    换成其他脑机接收器时显式设置 devices.bci.port，不退回宽泛 glob。
    """
    from serial.tools import list_ports

    candidates = [p.device for p in list_ports.comports() if is_bci_port(p)]
    if len(candidates) != 1:
        return None
    return candidates[0].replace("/dev/cu.", "/dev/tty.")


class BCISerialClient:
    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        self.fs = int(cfg.get("devices.bci.fs", 250))
        self.baud = int(cfg.get("devices.bci.baud", 57600))
        configured = cfg.get("devices.bci.port", "auto")
        self.port = None if configured in (None, "auto") else str(configured)

        self.parser = ThinkGearParser()
        self.detector = BlinkDetector(
            fs=self.fs,
            abs_threshold=cfg.get("eyes.calibration.abs_threshold"),
            intent_peak=cfg.get("eyes.calibration.intent_peak"),
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_quality: int | None = None
        self._buf: list[int] = []

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="bci-serial", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def recalibrate(self, abs_threshold: float, intent_peak: float) -> None:
        self.detector.calibrate(abs_threshold, intent_peak)

    # ---------- 读取线程 ----------

    def _run(self) -> None:
        while not self._stop.is_set():
            port = self.port or find_port()
            if not port:
                log.warning("没找到脑机串口，2 s 后重试（可用 --sim 跳过硬件）")
                self._stop.wait(2.0)
                continue
            try:
                self._session(port)
            except serial.SerialException as e:
                self.bus.publish("bci.disconnected", reason=str(e))
                log.warning("脑机串口断开：%s，2 s 后重连", e)
                self._stop.wait(2.0)
            except Exception:
                log.exception("脑机读取线程异常")
                self._stop.wait(2.0)

    def _session(self, port: str) -> None:
        with serial.Serial(port, self.baud, timeout=0.05) as ser:
            time.sleep(0.2)
            ser.reset_input_buffer()
            self.bus.publish("bci.connected", port=port, protocol="thinkgear", fs=self.fs)
            log.info("脑机已连接 %s @ %d (fs=%d Hz)", port, self.baud, self.fs)

            idle_since = time.time()
            while not self._stop.is_set():
                n = ser.in_waiting
                data = ser.read(n if n else 1)
                if not data:
                    if time.time() - idle_since > 5.0:
                        raise serial.SerialException("5 s 内无数据，可能是设备断电或接收器掉线")
                    continue
                idle_since = time.time()
                self._consume(data)

    def _consume(self, data: bytes) -> None:
        for name, value in self.parser.feed(data):
            if name == "raw":
                self._buf.append(int(value))
                if len(self._buf) >= CHUNK:
                    self._flush_raw()
            elif name == "poor_signal":
                if value != self._last_quality:
                    self._last_quality = int(value)
                    self.bus.publish("bci.quality", poor_signal=int(value))
            elif name == "blink":
                # 该固件实测不发这个码；万一发了，作为设备侧眨眼一并上报
                self.bus.publish("bci.blink", strength=min(int(value) / 255.0, 1.0),
                                 duration_ms=None, source="device")
            elif name == "attention":
                self.bus.publish("bci.attention", value=int(value))
            elif name == "meditation":
                self.bus.publish("bci.meditation", value=int(value))
            elif name == "eeg_power":
                self.bus.publish("bci.eeg_power", bands=value)

    def _flush_raw(self) -> None:
        samples, self._buf = self._buf, []
        blinks = self.detector.feed(samples)
        now = time.time()
        self.bus.publish("bci.raw", fs=self.fs, samples=samples,
                         filtered=[round(v, 1) for v in self.detector.filtered])
        for b in blinks:
            # 眨眼是按 CHUNK（≈128 ms）成批检测的，若用发布时刻当眨眼时刻，
            # 同一批里的两次眨眼会拿到相同时间戳，而双眨判据窗口是 120–450 ms
            # ——那样双眨永远判不出来。用 age_ms 回推真实发生时刻。
            self.bus.publish("bci.blink", t=now - b.age_ms / 1000.0,
                             strength=round(b.strength, 3),
                             duration_ms=round(b.duration_ms, 1),
                             peak=round(b.peak, 1), source="raw")
