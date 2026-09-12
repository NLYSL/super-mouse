#include <assert.h>
#include <vector>
#include "../firmware/radar_bridge/radar_protocol.h"
using radar::AckResult;

std::vector<uint8_t> ack(uint8_t command, uint8_t status = 0) {
  return {0xFD,0xFC,0xFB,0xFA,4,0,command,1,status,0,4,3,2,1};
}
AckResult feed(radar::AckParser &parser, const std::vector<uint8_t> &data) {
  AckResult result = AckResult::Pending;
  for (auto b : data) result = parser.feed(b);
  return result;
}
int main() {
  const auto success = ack(0x90);
  radar::AckParser p(0x90);
  // 截断帧甚至已收到 status 也不能成功，必须等长度指定的帧尾。
  for (size_t i=0; i+1<success.size(); ++i) assert(p.feed(success[i]) == AckResult::Pending);
  assert(p.feed(success.back()) == AckResult::Success);
  radar::AckParser failed(0x90);
  assert(feed(failed, ack(0x90, 1)) == AckResult::Failure);
  radar::AckParser wrong(0x90);
  assert(feed(wrong, ack(0xFF)) == AckResult::Pending);
  assert(feed(wrong, success) == AckResult::Success);
  radar::AckParser noisy(0x90);
  assert(feed(noisy, std::vector<uint8_t>(1000, 0xAA)) == AckResult::Pending);
  auto bad = success; bad.back()=0;
  assert(feed(noisy, bad) == AckResult::Pending);
  assert(feed(noisy, success) == AckResult::Success);
  radar::AckParser len(0x90);
  assert(feed(len, {0xFD,0xFC,0xFB,0xFA,0xFF,0xFF}) == AckResult::Pending);
  assert(feed(len, success) == AckResult::Success);
  radar::AckParser enable(0xFF);
  assert(feed(enable, {0xFD,0xFC,0xFB,0xFA,8,0,0xFF,1,0,0,1,0,0x40,0,4,3,2,1}) == AckResult::Success);
  std::vector<uint8_t> frame(30,0);
  frame[0]=0xAA; frame[1]=0xFF; frame[2]=3; frame[28]=0x55; frame[29]=0xCC;
  assert(radar::hasTargetFrame(frame.data(), frame.size()));
  assert(!radar::hasTargetFrame(frame.data(), frame.size()-1));
  frame[28]=0;
  assert(!radar::hasTargetFrame(frame.data(), frame.size()));
}
