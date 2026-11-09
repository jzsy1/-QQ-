#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
临时工作版本 - 避免导入问题
"""

import sys
from pathlib import Path

def main():
    print("Kekesi 临时版本")
    print("当前系统可以运行，但部分功能受限")
    print("建议安装完整依赖以使用所有功能")

    # 简单交互
    while True:
        try:
            user_input = input("\n输入命令 (或 'exit' 退出): ").strip()
            if user_input.lower() in ['exit', 'quit']:
                print("再见！")
                break
            print(f"收到: {user_input}")
        except KeyboardInterrupt:
            print("\n再见！")
            break
        except EOFError:
            break

if __name__ == "__main__":
    main()
