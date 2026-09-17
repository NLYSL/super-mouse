# 🐭 SUPER MOUSE — Bring Your Cursor to Life

> A little mouse lives in your cursor. It sees your eyes, understands your work, and watches your back.

**SUPER MOUSE** is a macOS desktop companion that replaces the system cursor with an animated cartoon mouse, then gives it three superpowers through **a brain–computer interface, a millimeter-wave radar, and a large language model**. You stop operating the cursor — the cursor starts working for you.

| | |
|---|---|
| 🏆 Hackathon project · single Python process · all three modes verified on real hardware | |
| ⚙️ macOS · Python 3.11+ · PyQt6 + asyncio event bus · 171 unit tests | |
| 🔗 https://github.com/NLYSL/super-mouse | |

---

## The Three Superpowers

### 👀 SHARP EYES — Blinks Are Commands

A three-electrode forehead BCI reads your electro-oculographic signal at 250 Hz and separates **involuntary blinks** from **intentional ones**:

- **60-second calibration** — three phases (relaxed blinking → forceful blinking → double-blink rhythm) capture your personal characteristics; thresholds are computed automatically and written to your profile
- **Gesture vocabulary** — double blink, triple blink, hard blink, and long blink can each be bound to any keyboard shortcut
- **Bind wizard designed for the flow you'd expect**: first perform the gesture, then press the key you want it to trigger
- **Nothing is pre-bound out of the box** — the device will never click anything on your behalf before you explicitly opt in

**Engineering notes.** Every parameter came from labeled recordings of a real user, not guesses: natural blinks last 68–132 ms while forceful blinks last 184–284 ms — the two distributions separate cleanly on duration alone. A band-pass filter with a gated noise-floor estimator plus polarity verification yields zero false triggers across 14 natural blinks. When the adaptive threshold was replaced by a naive rolling-median estimator, a threshold drift from 38 → 348 was observed during dense blinking; the gated estimator stays within 38–47 throughout.

### 🧠 SUPER BRAIN — Hover to Delegate

Rest your cursor on an app icon for 0.7 s and the mouse holds up a little sign asking whether it should do the work for you. **Press Space** to confirm, and it gets to work:

- **Pages stock report** (our showcase demo): move the cursor onto the "Pages" alias on the desktop → the mouse asks *"Write today's stock report?"* → press Space → it fetches live quotes for four major indices, typesets a document, and opens it in Pages. **No AI API required at any point** — this works fully offline
- Built-in skills: draft Mail replies (saved as drafts, never sent), create a Notes daily report, open your morning Chrome tabs, summarize any document
- **Brain settings panel**: bind built-in skills to any app, or write custom AI instructions per app
- Dual backend: DeepSeek (default) and Claude, switchable via one config line; every AI path degrades gracefully to an offline skill

**Interaction design.** All confirmation channels are equivalent — Space, clicking the sign, or a hard blink travel through the exact same event chain, so any of them can substitute for another mid-demo.

### 📡 STRONG SENSE — Someone Behind You? Instant Screen Switch

An HLK-LD2454 24 GHz millimeter-wave radar tracks targets in its detection zone at 16 fps with full coordinates for up to 3 targets:

- **Head-count based detection**: you sitting at the desk = 1 target; **the count reaching 2 = someone approaching**
- When triggered: fun apps are hidden → the screen switches back to your **bound fullscreen work page** → audio mutes → the mouse dives into its hole
- When the person leaves: the mouse peeks out and asks *"Go back?"* — press Space to restore everything
- Bind your work page with **⌃⌥B** from inside any fullscreen app (the mouse holds up a sign to confirm); the hotkey itself is rebindable
- The control panel shows the live target count, so the moment "Target 1 (you)" turns red "Target 2" is visible to the audience

**Engineering notes.** A single-person sitting baseline was measured at 163 consecutive frames with zero flicker. Walking targets, however, are intermittently dropped by the radar — so the trigger counts qualifying frames over a 2-second sliding window instead of requiring consecutive frames. This is what makes "someone walks up and stops right beside you" (the most realistic boss scenario, 0.6–0.8 m from the radar) fire reliably; the naive consecutive-frame counter and a 0.8 m minimum-distance gate both failed exactly this case during real testing.

---

## Why One Mouse Instead of Three Features

All sensing channels converge into one body language: the mouse blinks when you blink (the most direct visible proof that your EEG is being read), wears a hard hat while working, and dives into a hole when someone approaches. **It is not a dashboard — it is a reactive creature.** Judges and users never need to read documentation; watching its face tells them what the system is thinking.

Architecturally, everything is an event: the BCI, radar, and AI communicate only through a unified event bus, which means any leg can be replaced by a hotkey simulator (`--sim` runs the full experience with no hardware) or replayed from a recording — the demo has no single point of failure.

---

## Hardware

| Device | Role | Integration |
|---|---|---|
| 3-electrode forehead BCI | SHARP EYES | USB receiver → serial, plug and play (ThinkGear protocol, ~250 Hz raw wave) |
| HLK-LD2454 mmWave radar | STRONG SENSE | LCSC ESP32-S3R8N8 (on-board MicroPython bridge) reads the radar → USB serial; WCH CH34x driver required |
| Mac | All logic | Zero configuration once drivers are installed |

The radar chain depends on the WCH CH34x driver (`brew install --cask wch-ch34x-usb-serial-driver`, then reboot). Wiring: radar TX→G10, RX→G11, 5V/GND matched. The board runs a MicroPython bridge (archived at `firmware/ld2454_micropython/main.py`) — **do not reflash it**.

## Quick Start

```bash
git clone https://github.com/NLYSL/super-mouse && cd super-mouse
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

python tools/api_check.py --quick   # self-check: deps / permissions / hardware / config
python -m supermouse --debug        # launch (auto-discovers the BCI and radar)
```

The mouse cursor appears and follows your pointer; the control panel opens automatically. Open `tools/dashboard.html` in a browser for a live view of the EEG waveform, radar target count, and event stream.

Hardware-free demo: `python -m supermouse --sim --debug` (hotkeys simulate blinks and radar events).

### Permissions (first run)

System Settings → Privacy & Security — add and enable your terminal app for:

- **Accessibility** — gesture clicks, hover detection, screen switching
- **Screen Recording** — screenshot-based AI suggestions
- **Input Monitoring** — global hotkeys (work-page binding, etc.)

Then **fully quit and reopen the terminal**. Approve every automation prompt on first use.

---

## Documentation

| Doc | Contents |
|---|---|
| [01 · PRD](docs/01-产品需求文档-PRD.md) | Product definition, per-mode interaction, gesture vocabulary, acceptance criteria |
| [02 · Architecture](docs/02-系统架构与技术方案.md) | Architecture diagram, event protocol, config spec, macOS integration notes |
| [03 · Hardware](docs/03-硬件接入指南.md) | BCI protocol parsing, blink-detection derivation, radar wiring and tuning |
| [04 · AI Design](docs/04-Smart-Brain-AI设计.md) | Hover detection, skill system, dual backend, safety boundaries |
| [05 · Demo Script](docs/05-开发计划与Demo脚本.md) | 3-minute demo script and pre-demo checklist |
| [07 · Radar Handoff](docs/07-雷达数据接入交接.md) | Full debugging history: drivers, protocols, the two-board migration, every pitfall |

## Testing

```bash
python -m pytest tests/ -q        # 171 passed
```

Coverage includes signal processing (byte-by-byte parser resync, threshold drift regression), radar protocol parsing against frames from the official datasheet, presence-trigger regressions for the two real-world failure modes, and plan resolution.
