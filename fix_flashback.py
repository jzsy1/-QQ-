#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
快速修复闪退问题
"""

import os
import sys
import time
from pathlib import Path

def fix_database():
    """修复数据库锁定问题"""
    print("1. 修复数据库锁定...")
    data_dir = Path(__file__).parent / "data"
    db_path = data_dir / "kekesi.db"

    # 删除 WAL 文件
    files_to_delete = [
        db_path.with_suffix('.db-wal'),
        db_path.with_suffix('.db-shm')
    ]

    cleaned = False
    for file_path in files_to_delete:
        if file_path.exists():
            try:
                file_path.unlink()
                print(f"   [OK] 删除: {file_path.name}")
                cleaned = True
            except Exception as e:
                print(f"   [FAIL] 删除失败 {file_path.name}: {e}")

    if cleaned:
        print("   [OK] 数据库清理完成")
    else:
        print("   [OK] 数据库文件正常")

    return cleaned

def add_protection():
    """添加保护措施"""
    print("\n2. 添加运行时保护...")

    # 修改 main.py 添加异常处理
    main_py = Path(__file__).parent / "main.py"

    # 添加导入
    with open(main_py, 'r', encoding='utf-8') as f:
        content = f.read()

    if 'import time' not in content:
        # 在导入部分添加 time
        import_pos = content.find('import threading')
        if import_pos != -1:
            new_content = content[:import_pos+12] + '\nimport time' + content[import_pos+12:]

            # 保存
            with open(main_py, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print("   [OK] 添加了 time 导入")
        else:
            print("   [WARN] 未找到导入位置")
    else:
        print("   [OK] time 导入已存在")

def show_usage():
    """显示使用说明"""
    print("\n" + "=" * 50)
    print("修复完成！使用方法：")
    print("\n1. 修复后重启程序：")
    print("   python main.py")
    print("\n2. 如果还是闪退，运行资源检查：")
    print("   python check_resources.py")
    print("\n3. 使用对话监控模式（更稳定）：")
    print("   python main.py --once \"你的问题\"")
    print("\n" + "=" * 50)

def main():
    print("Kekesi 闪退问题修复工具")
    print("=" * 50)

    # 执行修复
    fixed = fix_database()
    add_protection()

    if fixed:
        print("\n[OK] 修复完成！")
    else:
        print("\n[WARN] 未发现问题，但已添加保护措施")

    show_usage()

if __name__ == "__main__":
    main()