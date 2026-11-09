#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
调试模式启动脚本 - 直接执行 main.py 的内容
"""

import sys
from pathlib import Path

# Windows 控制台中文乱码修复
if sys.platform.startswith("win"):
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 添加当前目录到 sys.path
current_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(current_dir))

# 导入必要的模块
import argparse
import logging
import os
import signal
import threading
import time
import json
from collections import deque

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)

logger = logging.getLogger("kekesi.debug")

# 简化版测试 - 只打印基本信息
def test_basic_functionality():
    """测试基本功能是否正常"""
    try:
        # 尝试导入基本模块
        from pathlib import Path
        print("✓ pathlib 导入成功")

        # 尝试检查配置文件
        config_path = current_dir / "config.yaml"
        if config_path.exists():
            print(f"✓ 配置文件存在: {config_path}")
        else:
            print("✗ 配置文件不存在")

        # 尝试检查数据库目录
        data_dir = current_dir / "data"
        if not data_dir.exists():
            data_dir.mkdir(exist_ok=True)
            print(f"✓ 创建数据目录: {data_dir}")
        else:
            print(f"✓ 数据目录存在: {data_dir}")

        # 尝试列出一些 Python 文件
        py_files = list(current_dir.glob("*.py"))
        print(f"✓ 找到 {len(py_files)} 个 .py 文件")

        return True

    except Exception as e:
        print(f"✗ 基本功能测试失败: {e}")
        return False

if __name__ == "__main__":
    print("=" * 50)
    print("Kekesi v2.0 调试模式")
    print("=" * 50)
    print()

    # 基本功能测试
    print("正在进行基本功能测试...")
    basic_ok = test_basic_functionality()

    if basic_ok:
        print("\n✓ 基本功能测试通过")
        print("\n尝试启动简化模式...")

        # 尝试一个简单的测试
        try:
            print("\n当前工作目录:", os.getcwd())
            print("Python 路径:", sys.path[:3])

            # 检查是否有其他进程占用
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            try:
                result = sock.connect_ex(("127.0.0.1", 18791))
                if result == 0:
                    print("⚠ 端口 18791 已被占用，可能有其他实例在运行")
                else:
                    print("✓ 端口 18791 可用")
            finally:
                sock.close()

        except Exception as e:
            print(f"检查时出错: {e}")

    else:
        print("\n✗ 基本功能测试失败")

    print("\n" + "=" * 50)
    print("调试模式完成")