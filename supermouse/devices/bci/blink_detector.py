"""从原始 EEG 检测眨眼。

原理：前额电极离眼睛很近，眨眼产生的眼电（EOG）伪迹幅度远大于脑电本身，
表现为低频大幅慢波。用力眨眼幅度更大、**时间明显更长**。

真机实测（2026-09-12，24 秒带标注录音，fs=246 Hz，poor_signal=0）：

    静息底噪      中位数 7.3，σ 6.4          → 合理阈值 47–58
    自然眨眼      时长 92–128 ms，峰值 68–141
    用力眨眼      时长 184–212 ms，峰值 165–219
    双眨（3 组）  间隔 176 / 200 / 188 ms
                  第一下 ~100 ms，第二下 ~70 ms
                  第二下峰值稳定为第一下的 0.70 倍
    滤波后波形    每次眨眼呈 "− + −" 三段，**负向那一下先到**

四个由实测数据推出的设计决定：

1. 阈值用**门控式底噪估计**：只把"不在眨眼中"的样本喂给估计器。
   朴素的滚动窗 median+MAD 会被眨眼自己污染——实测在密集双眨段阈值
   从 38 飙到 348，直接漏检全部；门控法全程稳定在 38–47。
   （百分位方案 P25/P40/P50/P75 也都试过，全部漂移 9–10 倍，不可用。）

2. **相对幅度门**：真眨眼的峰值是阈值的 2.6–3.8 倍，噪声毛刺只有 1.2–1.9 倍。
   同时与"最近几次眨眼的峰值中位数"比较，太小的直接丢弃。这是 MNE
   `find_eog_events` 用 (max−min)/4 作阈值的流式等价物。

3. 极性**不从第一段波动学**。每次眨眼滤波后是三段交替、负向先到，
   按"第一段"定极性会学反。默认 +1（前额-耳参考的标准眼电方向），
   只在长期证据压倒性相反时翻转；校准会把结论写回配置。

4. 眨眼**必须带样本级时刻**。检测是按 32 样本分块做的（~130 ms），
   若用"事件发布时刻"当眨眼时刻，同一块内的两次眨眼会拿到相同时间戳，
   而双眨判据窗口是 120–450 ms——那样双眨永远判不出来。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

# 强度里幅度与时长的权重。**时长主导**——真机端到端回放证明这是关键：
#   时长  自然/双眨 68–132 ms   用力 184–284 ms   → 干净间隔（1.39 倍）
#   幅度  自然/双眨 98–165      用力 120–219      → 完全重叠，没有区分力
# 掺入幅度会让"自然最大"反超"用力最小"，重眨判据直接失效（实测 12 次误触发）。
# 保留 0.1 的幅度权重只为抵御低幅长漂移（电极移动）被误判成用力眨眼。
W_PEAK = 0.1
W_DUR = 0.9

# 未校准时的"用力眨眼"时长基准（ms）。实测用力眨眼 184–284 ms。
# 必须有默认值：否则强度公式退化成纯幅度，每次自然眨眼都会被判成重眨。
DEFAULT_INTENT_DUR_MS = 200.0

# 新眨眼峰值至少要达到"最近眨眼峰值中位数"的这个比例。
# 实测双眨第二下是第一下的 0.70 倍（峰值比阈值 1.67–2.03 倍），
# 而滤波振铃只有 1.25–1.33 倍。0.45 落在这条窄缝里。
REL_GATE = 0.45
# 只要有 1 次眨眼做参考就启用相对门。用 2 会留下一个空档：
# 第一次眨眼后的衰减尾巴（实测峰值 17.6、时长 468 ms）会趁机被当成第二次眨眼。
REL_MIN_SAMPLES = 1


@dataclass
class Blink:
    strength: float      # 0–1，相对"用力眨眼"的归一化强度（幅度+时长）
    duration_ms: float
    peak: float          # 滤波后的峰值幅度（校准用）
    sample: int = 0      # 眨眼起点的全局样本序号
    age_ms: float = 0.0  # 起点距本批数据末尾多久，调用方用它还原真实时刻


class BlinkDetector:
    def __init__(
        self,
        fs: int = 250,
        band: tuple[float, float] = (0.5, 8.0),
        k: float = 6.0,
        refractory_ms: int = 100,
        min_ms: int = 50,
        max_ms: int = 800,
        prominence: float = 1.5,
        abs_threshold: float | None = None,
        intent_peak: float | None = None,
        intent_dur_ms: float | None = None,
        polarity: int = 1,
    ):
        self.fs = fs
        self.sos = butter(2, band, btype="band", fs=fs, output="sos")
        self.zi = sosfilt_zi(self.sos) * 0.0
        self.k = k
        self.abs_threshold = abs_threshold
        self.intent_peak = intent_peak
        # 未校准也要有时长基准，否则强度退化成纯幅度 → 自然眨眼被误判为重眨
        self.intent_dur_ms = intent_dur_ms or DEFAULT_INTENT_DUR_MS
        self.prominence = prominence
        self.refractory = int(fs * refractory_ms / 1000)
        self.min_len = int(fs * min_ms / 1000)
        self.max_len = int(fs * max_ms / 1000)

        self.polarity = polarity if polarity in (1, -1) else 1
        self._pol_score = 0.0

        # 门控底噪估计：只收"不在眨眼中"的样本
        self._quiet: deque[float] = deque(maxlen=fs * 8)
        self._boot: list[float] = []
        self._cooldown = 0
        self._thr = float("inf")
        self._recent_peaks: deque[float] = deque(maxlen=8)

        self.n = 0                 # 已处理样本总数
        self.in_blink = False
        self.start = 0
        self.peak = 0.0
        self.sign = 0
        self.last_blink_start = -(10**9)
        self.filtered: list[float] = []   # 最近一批的滤波结果，给 dashboard 用

    # ---------- 阈值 ----------

    def threshold(self) -> float:
        return self.abs_threshold if self.abs_threshold is not None else self._thr

    def calibrate(self, abs_threshold: float, intent_peak: float,
                  intent_dur_ms: float | None = None,
                  polarity: int | None = None) -> None:
        self.abs_threshold = abs_threshold
        self.intent_peak = intent_peak
        if intent_dur_ms:
            self.intent_dur_ms = intent_dur_ms
        if polarity in (1, -1):
            self.polarity = polarity
            self._pol_score = 0.0

    def _update_floor(self) -> None:
        if self.abs_threshold is not None or len(self._quiet) < self.fs // 2:
            return
        q = np.fromiter(self._quiet, float, len(self._quiet))
        med = float(np.median(q))
        mad = float(np.median(np.abs(q - med))) + 1e-9
        self._thr = med + self.k * 1.4826 * mad

    # ---------- 主循环 ----------

    def feed(self, samples) -> list[Blink]:
        """喂入一批原始样本，返回本批检测到的眨眼。"""
        if len(samples) == 0:
            return []
        y, self.zi = sosfilt(self.sos, np.asarray(samples, dtype=float), zi=self.zi)
        self.filtered = y.tolist()

        out: list[Blink] = []
        for v in y:
            self.n += 1
            fv = float(v)
            a = abs(fv)
            thr = self.threshold()

            if not self.in_blink:
                if a > thr:
                    self.in_blink = True
                    self.start = self.n
                    self.peak = a
                    self.sign = 1 if fv > 0 else -1
                else:
                    # 门控：眨眼结束后再等 250 ms，避开滤波振铃尾巴，
                    # 只有真正安静的样本才用来估底噪
                    if self._cooldown > 0:
                        self._cooldown -= 1
                    else:
                        self._quiet.append(a)
            else:
                self.peak = max(self.peak, a)
                if a < thr * 0.5 or self.n - self.start > self.max_len:
                    length = self.n - self.start
                    start, peak, sign = self.start, self.peak, self.sign
                    self.in_blink = False
                    self._cooldown = int(self.fs * 0.25)
                    blink = self._judge(start, length, peak, sign, thr)
                    if blink is not None:
                        self.last_blink_start = start
                        self._recent_peaks.append(peak)
                        out.append(blink)

            # 引导阶段：头 1 秒无条件当底噪，之后周期性更新
            if self.n <= self.fs:
                self._boot.append(a)
                if self.n == self.fs:
                    b = np.array(self._boot)
                    med = float(np.median(b))
                    mad = float(np.median(np.abs(b - med))) + 1e-9
                    if self.abs_threshold is None:
                        self._thr = med + self.k * 1.4826 * mad
                    self._quiet.extend(self._boot)
                    self._boot = []
            elif self.n % 25 == 0:
                self._update_floor()

        # 补上样本级时刻：调用方据此还原每次眨眼的真实发生时间
        end = self.n
        for b in out:
            b.age_ms = (end - b.sample) * 1000.0 / self.fs
        return out

    def _judge(self, start: int, length: int, peak: float, sign: int,
               thr: float) -> Blink | None:
        """一段越阈波动是真眨眼吗？"""
        if not (self.min_len <= length <= self.max_len):
            return None

        # 突出度：真眨眼峰值是阈值的 2.6–3.8 倍，噪声毛刺只有 1.2–1.9 倍
        if peak < thr * self.prominence:
            return None

        # 相对幅度门（MNE 用 (max−min)/4 的流式等价物）
        if len(self._recent_peaks) >= REL_MIN_SAMPLES:
            ref = float(np.median(np.fromiter(self._recent_peaks, float,
                                              len(self._recent_peaks))))
            if peak < ref * REL_GATE:
                return None

        # 极性：只认主极性那一半（另一半是滤波振铃）
        self._pol_score += sign * peak
        if sign != self.polarity:
            if self._pol_score * self.polarity < -10 * peak:
                self.polarity = sign
                self._pol_score = sign * peak
            else:
                return None

        if start - self.last_blink_start <= self.refractory:
            return None

        return Blink(
            strength=self._strength(peak, length, thr),
            duration_ms=length * 1000.0 / self.fs,
            peak=peak,
            sample=start,
        )

    def _strength(self, peak: float, length: int, thr: float) -> float:
        """归一化强度，用来区分"用力眨眼"。以时长为主、幅度为辅（见 W_DUR 注释）。"""
        dur_ms = length * 1000.0 / self.fs
        ref_peak = self.intent_peak or (thr * 3)
        if ref_peak <= 0:
            return 0.0
        peak_score = peak / ref_peak
        dur_score = dur_ms / self.intent_dur_ms
        return float(min(W_PEAK * peak_score + W_DUR * dur_score, 1.0))
