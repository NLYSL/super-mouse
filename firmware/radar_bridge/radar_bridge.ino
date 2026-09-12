/*
 * Super Mouse · 雷达桥（海凌科 HLK-LD2454-24G）
 *
 * 立创·ESP32-S3R8N8 上跑这个固件，把 LD2454 的串口数据转发到 USB。
 *
 * 依据《LD2454 串口通信协议 V1.00》（2026-05-13）：
 *
 *   接口 J1：Pin1 5V · Pin2 GND · Pin3 TX · Pin4 RX      —— 没有 OUT 引脚
 *   串口：   256000 波特率，8 数据位，1 停止位，无校验
 *   数据帧： AA FF 03 00 | 目标1(8B) 目标2(8B) 目标3(8B) | 55 CC   共 30 字节，10 帧/秒
 *   单目标： X(2) Y(2) 速度(2) 距离分辨率(2)，全小端
 *            符号约定特殊：最高位 1 = 正，0 = 负，低 15 位是数值
 *
 *   **出厂默认是"单目标追踪"** —— 这是关键：默认模式下雷达只报一个目标，
 *   永远检测不到"身后出现第二个人"。所以本固件启动时必须下发命令切到多目标：
 *
 *     使能配置  FD FC FB FA 04 00 FF 00 01 00 04 03 02 01
 *     多目标    FD FC FB FA 02 00 90 00 04 03 02 01
 *     结束配置  FD FC FB FA 02 00 FE 00 04 03 02 01
 *
 *   下发任何命令前必须先"使能配置"，配完要"结束配置"恢复工作模式（协议 §2.2.1）。
 *
 * 接线（按协议 §1）：
 *     LD2454 Pin1 5V  → 板子 5V   ← 注意是 5V，不是 3V3
 *     LD2454 Pin2 GND → 板子 GND
 *     LD2454 Pin3 TX  → G10（丝印 T）   雷达发 → 我们收
 *     LD2454 Pin4 RX  → G11（丝印 R）   我们发 → 雷达收（下配置命令用）
 *
 * 输出协议（主机端 devices/radar 与 tools/radar_probe.py 依此解析）：
 *     '#' 开头        = 诊断文本
 *     '#BRIDGE rx=.. tx=.. baud=..' 之后 = 原始雷达字节流
 */

#include <Arduino.h>
#include "radar_protocol.h"

// 立创·ESP32-S3R8N8 丝印 → GPIO
//
// 实际接线（用户确认）：雷达 T(TX) → G10，雷达 R(RX) → G11。
// 所以默认就用 rx=10 / tx=11，扫描只是接线改动后的兜底。
//
// 扫描候选刻意避开这些脚，免得把板子搞挂：
//   0 / 3 / 45 / 46  strapping 脚
//   19 / 20          原生 USB D-/D+（占用会断掉串口）
//   26–37            Flash 与 Octal PSRAM（R8N8 是 8MB+8MB）
struct PinMap { const char *label; int gpio; };
static const PinMap PINS[] = {
  {"G10", 10}, {"G11", 11},                       // 实际接线，先试
  {"G12", 12}, {"G13", 13}, {"G14", 14}, {"G15", 15},
  {"G16", 16}, {"G17", 17}, {"G18", 18}, {"G21", 21},
  {"G4", 4}, {"G5", 5}, {"G6", 6}, {"G7", 7}, {"G8", 8}, {"G9", 9},
  {"G1", 1}, {"G2", 2},
};

// 已知接线：雷达 T → G10（我们收），雷达 R → G11（我们发配置命令）
static const int PIN_RX_DEFAULT = 10;
static const int PIN_TX_DEFAULT = 11;
static const int PINS_N = sizeof(PINS) / sizeof(PINS[0]);

static const long BAUD_DEFAULT = 256000;      // 协议 §2.3 出厂默认
// 兜底波特率（协议 §2.2.6 表6 全集）。若有人改过配置，扫这些能找回来。
static const long CAND_BAUD[] = {256000, 115200, 230400, 460800, 57600, 38400, 19200, 9600};
static const int CAND_BAUD_N = sizeof(CAND_BAUD) / sizeof(CAND_BAUD[0]);

// 数据帧头尾（协议 §2.3 表8）
static const uint8_t FRAME_HEAD[4] = {0xAA, 0xFF, 0x03, 0x00};
static const uint8_t FRAME_TAIL[2] = {0x55, 0xCC};
// 命令帧头尾（协议 §2.2）
static const uint8_t CMD_HEAD[4] = {0xFD, 0xFC, 0xFB, 0xFA};
static const uint8_t CMD_TAIL[4] = {0x04, 0x03, 0x02, 0x01};

static int  g_rx = -1, g_tx = -1;
static long g_baud = 0;
static bool g_bridging = false;
static bool g_multiTarget = false;

static void scan();
static void startBridge();

/* ---------------------------------------------------------------- 工具 */

static void printHex(const uint8_t *b, size_t n) {
  for (size_t i = 0; i < n; i++) {
    if (b[i] < 0x10) Serial.print('0');
    Serial.print(b[i], HEX);
    if (i + 1 < n) Serial.print(' ');
  }
}

/* 在 (rx, baud) 上听一会儿。tx 传 -1 = 只读不驱动。
   返回字节数，*hasFrame 标记是否见到 AA FF 03 00 帧头。 */
static size_t listen(int rx, int tx, long baud, uint32_t ms,
                     uint8_t *out, size_t cap, bool *hasFrame) {
  Serial1.end();
  delay(8);
  Serial1.begin(baud, SERIAL_8N1, rx, tx);
  delay(20);
  while (Serial1.available()) Serial1.read();     // 丢掉切换瞬间的垃圾

  size_t total = 0, kept = 0;
  uint32_t t0 = millis();
  while (millis() - t0 < ms) {
    while (Serial1.available()) {
      int c = Serial1.read();
      if (c < 0) break;
      total++;
      if (kept < cap) out[kept++] = (uint8_t)c;
    }
    delay(1);
  }
  *hasFrame = radar::hasTargetFrame(out, kept);
  return total;
}

/* ---------------------------------------------------------------- 引脚电平诊断

   判断某个脚是"被外部驱动"还是"悬空"：
   分别用内部下拉和上拉去读，悬空脚会跟着内部拉走（两次读数相反），
   被外部驱动的脚不会（两次一致）。

   对 LD2454 尤其有用：**空闲的 UART TX 线是恒定高电平**。
   恒高只与 UART 空闲相容，不足以证明接线/供电正确。
   雷达 RX 是高阻输入，跟随上下拉不代表没有接线。          */

static bool probeDriven(int gpio, int *levelOut) {
  pinMode(gpio, INPUT_PULLDOWN);
  delay(6);
  int lowPull = 0;
  for (int i = 0; i < 8; i++) { lowPull += digitalRead(gpio); delay(2); }

  pinMode(gpio, INPUT_PULLUP);
  delay(6);
  int highPull = 0;
  for (int i = 0; i < 8; i++) { highPull += digitalRead(gpio); delay(2); }

  pinMode(gpio, INPUT);
  if (lowPull >= 7 && highPull >= 7) { *levelOut = 1; return true; }
  if (lowPull <= 1 && highPull <= 1) { *levelOut = 0; return true; }
  return false;
}

static void reportLevels() {
  Serial1.end();
  delay(10);
  Serial.println("#LEVEL 引脚电平诊断（空闲 UART TX = 恒高）");
  int driven = 0;
  for (int i = 0; i < PINS_N; i++) {
    int lv = 0;
    bool d = probeDriven(PINS[i].gpio, &lv);
    if (d) driven++;
    Serial.printf("#  %-3s GPIO%-2d : %s\n", PINS[i].label, PINS[i].gpio,
                  d ? (lv ? "被驱动 · 恒高  ← 像接通但静默的 UART TX"
                          : "被驱动 · 恒低  ← 异常，TX 空闲应为高")
                    : "随内部上下拉变化（高阻输入也会如此，不证明未接线）");
  }
  if (driven == 0) {
    Serial.println("#LEVEL 未发现稳定外部驱动；请检查供电/共地/焊接，不能仅凭电平判定断线");
  } else {
    Serial.println("#LEVEL 有脚被外部驱动；恒高与空闲 UART 相容，但不能证明 TX 接线或供电正确");
    Serial.println("#  检查 J1 Pin1 5V 实测电压、共地和焊点；RX 是输入，高阻是正常现象");
  }
}

/* ---------------------------------------------------------------- 多目标配置

   协议 §2.2：任何命令前必须先"使能配置"，结束后"结束配置"。
   出厂默认单目标追踪 → 不发这组命令就永远只报 1 个目标，
   "身后出现第二个人"这个需求根本无法满足。                            */

static bool sendCmd(const uint8_t *value, size_t vlen, const char *what) {
  // 帧：FD FC FB FA | len(2,LE) | value | 04 03 02 01
  uint8_t frame[32];
  size_t n = 0;
  memcpy(frame + n, CMD_HEAD, 4); n += 4;
  frame[n++] = (uint8_t)(vlen & 0xFF);
  frame[n++] = (uint8_t)(vlen >> 8);
  memcpy(frame + n, value, vlen); n += vlen;
  memcpy(frame + n, CMD_TAIL, 4); n += 4;

  while (Serial1.available()) Serial1.read();
  Serial1.write(frame, n);
  Serial1.flush();

  radar::AckParser ack(uint16_t(value[0]) | (uint16_t(value[1]) << 8));
  radar::AckResult result = radar::AckResult::Pending;
  uint8_t buf[20];
  size_t got = 0;
  uint32_t t0 = millis();
  while (millis() - t0 < 600 && result == radar::AckResult::Pending) {
    while (Serial1.available() && result == radar::AckResult::Pending) {
      int c = Serial1.read();
      if (c < 0) break;
      if (got < sizeof(buf)) buf[got++] = (uint8_t)c;
      result = ack.feed((uint8_t)c);
    }
    delay(1);
  }
  bool ok = result == radar::AckResult::Success;

  Serial.printf("#CMD %-10s ", what);
  if (got) { Serial.print("ACK= "); printHex(buf, got < 20 ? got : 20); }
  else     { Serial.print("无回应");
  }
  Serial.println(ok ? "  → 成功" : "  → 失败");
  return ok;
}

static bool enableMultiTarget() {
  g_multiTarget = false;
  if (g_tx < 0) {
    Serial.println("#WARN TX 未接，无法下发命令 → 雷达停留在出厂默认的**单目标**模式");
    Serial.println("#     这样检测不到\"身后出现第二个人\"，请把 LD2454 Pin4 RX 接到 G11");
    return false;
  }
  const uint8_t enable[]  = {0xFF, 0x00, 0x01, 0x00};   // 0x00FF 值 0x0001
  const uint8_t multi[]   = {0x90, 0x00};               // 0x0090 多目标
  const uint8_t endCfg[]  = {0xFE, 0x00};               // 0x00FE 结束配置

  bool a = sendCmd(enable, sizeof(enable), "使能配置");
  bool b = a && sendCmd(multi, sizeof(multi), "多目标");
  bool c = sendCmd(endCfg, sizeof(endCfg), "结束配置");   // 无论如何都要恢复工作模式
  g_multiTarget = (a && b && c);
  Serial.printf("#MULTI %s\n", g_multiTarget ? "已启用多目标追踪（最多 3 人）"
                                             : "未确认（配置或结束配置未完整 ACK），不能保证检测第二人");
  return g_multiTarget;
}

/* ---------------------------------------------------------------- 扫描 */

static void scan() {
  g_bridging = false;
  g_multiTarget = false;
  g_rx = g_tx = -1;
  g_baud = 0;
  Serial.println("#SCAN start");
  Serial.printf("#  LD2454：先在 %ld 找 AA FF 03 00 帧头\n", BAUD_DEFAULT);

  uint8_t buf[256];
  bool hasFrame = false;
  int bestRx = -1; long bestBaud = 0; size_t bestN = 0;

  // 第一轮：默认波特率，逐个引脚找真帧头（最可能命中）
  for (int i = 0; i < PINS_N; i++) {
    size_t n = listen(PINS[i].gpio, -1, BAUD_DEFAULT, 260, buf, sizeof(buf), &hasFrame);
    if (!n) continue;
    Serial.printf("#  %-3s GPIO%-2d @ %6ld : %4u 字节%s  ",
                  PINS[i].label, PINS[i].gpio, BAUD_DEFAULT, (unsigned)n,
                  hasFrame ? "  ✔帧头命中" : "");
    printHex(buf, n < 32 ? n : 32);
    Serial.println();
    if (hasFrame) { bestRx = PINS[i].gpio; bestBaud = BAUD_DEFAULT; bestN = n; break; }
    if (n > bestN) { bestN = n; bestRx = PINS[i].gpio; bestBaud = BAUD_DEFAULT; }
  }

  // 第二轮：没找到帧头 → 有人改过波特率，扫协议表里的全集
  if (bestRx < 0 || !hasFrame) {
    Serial.println("#  默认波特率没命中帧头，扫其他波特率…");
    for (int i = 0; i < PINS_N && !hasFrame; i++) {
      for (int b = 0; b < CAND_BAUD_N; b++) {
        if (CAND_BAUD[b] == BAUD_DEFAULT) continue;
        size_t n = listen(PINS[i].gpio, -1, CAND_BAUD[b], 200, buf, sizeof(buf), &hasFrame);
        if (!n) continue;
        Serial.printf("#  %-3s @ %6ld : %4u 字节%s\n", PINS[i].label, CAND_BAUD[b],
                      (unsigned)n, hasFrame ? "  ✔帧头命中" : "");
        if (hasFrame) { bestRx = PINS[i].gpio; bestBaud = CAND_BAUD[b]; break; }
        if (n > bestN) { bestN = n; bestRx = PINS[i].gpio; bestBaud = CAND_BAUD[b]; }
      }
    }
  }

  Serial1.end();

  if (bestRx < 0) {
    Serial.println("#SCAN 失败：没有任何引脚收到数据");
    // 电平仅作线索，不能据此断言连通性。
    reportLevels();
    Serial.println("#  按协议 §1 检查接线（模块出厂不带排针，需自己焊）：");
    Serial.println("#    Pin1 5V  → 板子 5V    ← 是 5V，不是 3V3！供电不足会时好时坏");
    Serial.println("#    Pin2 GND → 板子 GND   ← 不共地一定收不到");
    Serial.println("#    Pin3 TX  → G10（丝印 T）");
    Serial.println("#    Pin4 RX  → G11（丝印 R）   ← 不接则只能单目标");
    return;
  }

  g_rx = bestRx;
  g_baud = bestBaud;
  // 仅在确认到完整目标帧、且 RX 正是约定的 G10 时才驱动 G11。
  // 对未知接线不猜 TX —— 猜错会让 ESP32 的推挽输出和雷达输出对顶。
  g_tx = (hasFrame && g_rx == PIN_RX_DEFAULT) ? PIN_TX_DEFAULT : -1;
  Serial.printf("#SCAN rx=GPIO%d baud=%ld %s\n", g_rx, g_baud,
                hasFrame ? "（帧头已确认）" : "（有数据但未见帧头，波特率可能不对）");

  // 开双向串口后下发多目标命令
  Serial1.end(); delay(8);
  Serial1.begin(g_baud, SERIAL_8N1, g_rx, g_tx);
  delay(30);
  enableMultiTarget();

  startBridge();
}

static void startBridge() {
  // 命令阶段已经用 (g_rx, g_tx) 打开了 Serial1，直接进入转发
  Serial.printf("#BRIDGE rx=%d tx=%d baud=%ld multi=%d\n",
                g_rx, g_tx, g_baud, g_multiTarget ? 1 : 0);
  Serial.flush();
  g_bridging = true;
}

static bool validPin(int gpio) {
  for (int i = 0; i < PINS_N; i++) if (PINS[i].gpio == gpio) return true;
  return false;
}

static void handleCommand(String line) {
  line.trim();
  if (line == "!scan") {
    scan();
  } else if (line == "!info") {
    Serial.printf("#INFO rx=%d tx=%d baud=%ld multi=%d bridging=%d\n",
                  g_rx, g_tx, g_baud, g_multiTarget ? 1 : 0, g_bridging ? 1 : 0);
  } else if (line == "!level") {
    bool resume = g_bridging;
    g_bridging = false;
    reportLevels();
    if (resume) {
      Serial1.begin(g_baud, SERIAL_8N1, g_rx, g_tx);
      startBridge();
    }
  } else if (line == "!multi") {
    if (g_rx < 0 || g_tx < 0 || g_baud == 0) {
      Serial.println("#ERR 先确认接线，再用 !pins 17 16 初始化 UART");
      return;
    }
    g_bridging = false;
    enableMultiTarget();
    startBridge();
  } else if (line.startsWith("!pins ")) {
    int sp = line.indexOf(' ', 6);
    if (sp > 0) {
      int rx = -1, tx = -1;
      char extra;
      if (sscanf(line.c_str(), "!pins %d %d %c", &rx, &tx, &extra) != 2
          || rx == tx || !validPin(rx) || (tx != -1 && !validPin(tx))) {
        Serial.println("#ERR 两个引脚必须不同，TX 可设 -1 表示只读");
        return;
      }
      g_rx = rx; g_tx = tx;
      if (g_baud == 0) g_baud = BAUD_DEFAULT;
      Serial1.end(); delay(8);
      Serial1.begin(g_baud, SERIAL_8N1, g_rx, g_tx);
      delay(30);
      enableMultiTarget();
      startBridge();
    } else {
      Serial.println("#ERR 用法 !pins <rx> <tx>");
    }
  } else if (line.startsWith("!baud ")) {
    long b = line.substring(6).toInt();
    if (b > 0) {
      g_baud = b;
      if (g_rx < 0) { g_rx = PIN_RX_DEFAULT; g_tx = -1; }
      g_multiTarget = false;
      Serial1.end(); delay(8);
      Serial1.begin(g_baud, SERIAL_8N1, g_rx, g_tx);
      delay(30);
      startBridge();
    } else {
      Serial.println("#ERR 波特率无效");
    }
  } else if (line.length()) {
    Serial.printf("#ERR 未知命令 %s\n", line.c_str());
  }
}

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 2000) delay(10);
  delay(300);
  Serial.println();
  Serial.println("#HELLO Super Mouse 雷达桥 v2 · HLK-LD2454-24G");
  Serial.println("#  协议 V1.00 · 256000 8N1 · AA FF 03 00 … 55 CC · 最多 3 目标");
  scan();
}

void loop() {
  static String cmd;
  while (Serial.available()) {
    int c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmd.length()) { handleCommand(cmd); cmd = ""; }
    } else if (cmd.length() < 64) {
      cmd += (char)c;
    }
  }

  if (!g_bridging) { delay(20); return; }

  uint8_t buf[128];
  size_t n = 0;
  while (Serial1.available() && n < sizeof(buf)) {
    int c = Serial1.read();
    if (c < 0) break;
    buf[n++] = (uint8_t)c;
  }
  if (n) Serial.write(buf, n);
}
