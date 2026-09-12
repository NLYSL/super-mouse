"""主机上运行实际固件 ACK 解析器，AddressSanitizer 检查分包时越界。"""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_firmware_protocol(tmp_path):
    compiler = shutil.which("clang++") or shutil.which("g++")
    if not compiler:
        pytest.skip("需要 C++ 编译器以运行固件协议回归")
    source = Path(__file__).with_name("radar_protocol_test.cpp")
    binary = tmp_path / "radar-protocol-test"
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
