#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace radar {
static const uint8_t HEAD[] = {0xAA, 0xFF, 0x03, 0x00};
static const uint8_t ACK_HEAD[] = {0xFD, 0xFC, 0xFB, 0xFA};
static const uint8_t ACK_TAIL[] = {0x04, 0x03, 0x02, 0x01};

inline bool hasTargetFrame(const uint8_t *data, size_t size) {
  for (size_t i = 0; i + 30 <= size; ++i) {
    if (!memcmp(data + i, HEAD, 4) && data[i + 28] == 0x55 && data[i + 29] == 0xCC) return true;
  }
  return false;
}

enum class AckResult { Pending, Success, Failure };

// 完整的头/长度/回显/状态/尾全通过才接受；支持逐字节到达、数据帧和噪声夹杂。
class AckParser {
 public:
  explicit AckParser(uint16_t command) : expected_(command | 0x0100), size_(0) {}

  AckResult feed(uint8_t byte) {
    if (size_ == sizeof(buf_)) drop(1);
    buf_[size_++] = byte;
    while (size_ >= 4) {
      if (memcmp(buf_, ACK_HEAD, 4)) { drop(1); continue; }
      if (size_ < 6) return AckResult::Pending;
      size_t len = buf_[4] | (uint16_t(buf_[5]) << 8);
      if (len < 4 || len > 64) { drop(1); continue; }
      size_t total = len + 10;
      if (size_ < total) return AckResult::Pending;
      if (memcmp(buf_ + 6 + len, ACK_TAIL, 4)) { drop(1); continue; }
      uint16_t cmd = buf_[6] | (uint16_t(buf_[7]) << 8);
      uint16_t status = buf_[8] | (uint16_t(buf_[9]) << 8);
      drop(total);
      if (cmd == expected_) return status == 0 ? AckResult::Success : AckResult::Failure;
    }
    return AckResult::Pending;
  }

 private:
  void drop(size_t n) { memmove(buf_, buf_ + n, size_ - n); size_ -= n; }
  uint16_t expected_;
  uint8_t buf_[74];
  size_t size_;
};
}  // namespace radar
