#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
临时修复导入问题的脚本
"""

import sys
from pathlib import Path

def fix_imports():
    """修复导入问题"""
    current_dir = Path(__file__).parent
    sys.path.insert(0, str(current_dir))

    # 设置包名，使相对导入工作
    sys.path.insert(0, str(current_dir.parent))

    print("已修复导入问题")
    return True

def test_single_run():
    """测试单次运行"""
    try:
        import main
        result = main.main(['--once', '测试'])
        print(f"单次运行结果: {result}")
        return True
    except Exception as e:
        print(f"单次运行失败: {e}")
        return False

if __name__ == "__main__":
    print("修复闪退问题 - 第一步：解决导入")
    print("=" * 40)

    fix_imports()

    print("\n测试单次运行...")
    if test_single_run():
        print("\n✓ 单次运行成功，可以启动程序了")
        print("\n使用方法：")
        print("python fix_import.py")
    else:
        print("\n✗ 仍需要其他修复")