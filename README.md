# 🐭 SUPER MOUSE — WITH 4 MODES

> 把你的鼠标光标换成一只可爱的老鼠。它看得见你的眼睛、懂你的工作、替你盯着身后。

| 模式 | 一句话 | 硬件 | 状态 |
|---|---|---|---|
| 👀 **SHARP EYES** | 前额叶脑机识别眨眼，自校准 + 自定义"眼势→热键"绑定 | 三电极脑机（USB 串口） | ✅ 真机跑通 |
| 🧠 **SUPER BRAIN** | 光标悬停应用/替身 → 老鼠举牌 → 空格确认 → 自动干活 | DeepSeek / Claude API | ✅ 真机跑通 |
| 📡 **STRONG SENSE** | 雷达数人头：你=1，来人=2 → 切回绑定的全屏工作页 | HLK-LD2454（多目标雷达） | ✅ 真机跑通 |

## 10 分钟跑起来

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

python tools/api_check.py --quick   # 自检：依赖/权限/硬件/配置
python -m supermouse --debug        # 启动（自动找脑机 + 雷达）
```

启动后：老鼠光标跟随鼠标，控制面板自动弹出，浏览器打开 `tools/dashboard.html` 可看实时事件流（EEG 波形 / 目标数 / 事件）。

无硬件也能演示：`python -m supermouse --sim --debug`，用热键模拟眨眼和雷达。

## 权限（首次必做）

系统设置 → 隐私与安全性，把**启动用的终端**加进并勾选：

- **辅助功能** —— 眼势点击、悬停识别、切屏动作
- **屏幕录制** —— AI 截图建议
- **输入监控** —— 全局热键（绑定工作页面等）

授权后**完全退出终端再重开**。首次对各 App 的自动化弹窗全部点允许。

## 硬件（均为即插即用）

| 设备 | 接入 | 实测 |
|---|---|---|
| 脑机（ThinkGear 协议） | USB 接收器 → CDC 串口，自动识别 | ≈250 Hz 原始波，poor_signal=0 |
| HLK-LD2454 雷达 | 立创 ESP32-S3R8N8（MicroPython，勿重刷）读雷达 → CH340K 串口 → Mac | 16 帧/秒，多目标坐标 |

雷达链路依赖 WCH CH34x 驱动（`brew install --cask wch-ch34x-usb-serial-driver`，装后重启）。
接线：雷达 T→G10、R→G11、5V/GND 对接。板上跑 MicroPython 桥（`firmware/ld2454_micropython/main.py`），**不要重刷**。

## 三个模式的用法

### 👀 SHARP EYES（面板可开关）

1. **校准**：面板「校准眼势」—— 60 秒（自然眨眼 → 用力眨 → 双眨节奏），把你的个人阈值写进配置
2. **绑定**：面板「绑定热键」—— 先做一个眼势（如双眨），再按要绑的键（如 ⌃V）；也可绑"点击"或"BRAIN 确认"
3. 出厂不预绑任何动作 —— 全部由你决定

### 🧠 SUPER BRAIN（面板可开关 + 可自定义）

- **内置技能即悬停即用**：桌面「Pages 文稿」替身 → 老鼠举牌「写一份今日股票报告？」→ **空格确认** → 抓新浪行情 → 生成 RTF → Pages 打开（全程无需 API）
- 其他内置：Mail 起草回复、Notes 日报、Chrome 早间标签页、文档总结（走 DeepSeek）
- **自定义**：面板「脑设置」给任意 App 绑技能或自定义 AI 指令（存 `~/.supermouse/brain/plans.json`）
- 确认方式：**空格**（推荐）/ 点牌子 / 重眨

### 📡 STRONG SENSE（面板可开关，实时显示目标数）

1. 切到你要的**全屏工作页** → 按 **⌃⌥B**（或面板按钮）绑定 —— 老鼠举牌确认
2. 有人走近：面板「目标 1（你）」变红「目标 2」→ 隐藏摸鱼 App → 切回绑定的全屏工作页 → 静音 → 老鼠钻洞
3. 人走开 10 秒 → 老鼠探头「回去继续？」→ 空格恢复

判定按**人头数**（你=1，来人=2），滑窗抗抖；热键可在面板「换绑定热键」自定义。

## AI 后端

默认 **DeepSeek**（`deepseek-flash`，OpenAI 兼容协议）；额度恢复后改回 Claude 只需把 `~/.supermouse/config.yaml` 的 `brain.provider` 改为 `anthropic`。

- 内置技能（股票报告等）**不需要任何 API**
- 自由 AI 任务（agent 工具循环）仅 Claude 后端支持，DeepSeek 下会提示改用内置技能
- API key 在 `~/.supermouse/config.yaml`，**不要提交或外传**

## 项目结构

```
supermouse/
  core/            事件总线、配置、WebSocket
  devices/
    bci/           脑机串口、ThinkGear 解析、眨眼检测（真机标定过参数）
    radar/         雷达串口、MicroPython 文本桥解析、多目标判定
    simulator.py   热键模拟 + JSONL 回放
  modes/
    sharp_eyes/    眼势状态机、校准、绑定
    smart_brain/   悬停识别、预设、技能、DeepSeek/Claude 双后端
    strong_sense/  目标计数判定、场景切换
  actions/macos.py Quartz 输入合成、AppleScript、替身解析
  ui/              老鼠光标、控制面板、校准/绑定向导
firmware/ld2454_micropython/   板上固件存档（勿重刷）
tests/             171 个测试（信号/雷达/预设/回归）
```

## 测试与工具

```bash
python -m pytest tests/ -q        # 171 passed
python tools/api_check.py        # 演示前 10 分钟自检
python tools/dashboard.html      # 调试面板（--debug 启动后浏览器打开）
```

## 关键实测数据（调参依据）

| 项 | 自然眨眼 | 用力眨眼 |
|---|---|---|
| 时长 | 68–132 ms | 184–284 ms |
| 归一化强度 | 0.36–0.67 | 0.93–1.00 |

重眨阈值 0.80（0.70–0.90 区间零误判）；换人佩戴需重新校准。
雷达基线：单人静坐时目标数稳定为 1（163 帧零跳变），双人实测触发已验证。

## 文档

| 文档 | 内容 |
|---|---|
| [01-PRD](docs/01-产品需求文档-PRD.md) | 产品定义、交互、验收标准 |
| [02-架构](docs/02-系统架构与技术方案.md) | 架构、事件协议、配置规范 |
| [03-硬件](docs/03-硬件接入指南.md) | 脑机/雷达接入与算法 |
| [04-AI 设计](docs/04-Smart-Brain-AI设计.md) | 喂养、悬停、技能、安全边界 |
| [05-Demo 脚本](docs/05-开发计划与Demo脚本.md) | 时间线、3 分钟演示脚本、检查清单 |
| [07-雷达交接](docs/07-雷达数据接入交接.md) | 雷达链路排查记录（含踩坑史） |
