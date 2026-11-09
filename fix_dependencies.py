#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
修复依赖问题
"""

import os
import sys
import subprocess

def install_missing_deps():
    """安装缺失的依赖"""
    print("安装缺失的依赖...")
    print("=" * 40)

    # 需要安装的包
    packages = [
        "langchain",
        "anthropic"
    ]

    for package in packages:
        print(f"\n正在安装 {package}...")
        try:
            # 使用当前环境的 pip
            result = subprocess.run([
                sys.executable, "-m", "pip", "install", package
            ], capture_output=True, text=True, timeout=60)

            if result.returncode == 0:
                print(f"   [OK] {package} 安装成功")
            else:
                print(f"   [FAIL] {package} 安装失败")
                print(f"   错误: {result.stderr}")
        except Exception as e:
            print(f"   [FAIL] 安装 {package} 时出错: {e}")

def test_fix():
    """测试修复效果"""
    print("\n测试修复效果...")
    print("=" * 40)

    try:
        import langchain
        print("   [OK] langchain 可以导入")
    except ImportError as e:
        print(f"   [FAIL] langchain 仍然无法导入: {e}")

    try:
        import anthropic
        print("   [OK] anthropic 可以导入")
    except ImportError as e:
        print(f"   [FAIL] anthropic 仍然无法导入: {e}")

def check_database_table():
    """检查数据库表结构"""
    print("\n检查数据库...")
    print("=" * 40)

    import sqlite3
    from pathlib import Path

    data_dir = Path(__file__).parent / "data"
    db_path = data_dir / "kekesi.db"

    if not db_path.exists():
        print("   [INFO] 数据库文件不存在，可能是首次运行")
        return

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # 查看所有表
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        print(f"   [INFO] 数据库中的表: {[t[0] for t in tables]}")

        # 检查 memory_digests 表
        if 'memory_digests' in [t[0] for t in tables]:
            cursor.execute("SELECT COUNT(*) FROM memory_digests")
            count = cursor.fetchone()[0]
            print(f"   [OK] memory_digests 表有 {count} 条记录")
        else:
            print("   [WARN] memory_digests 表不存在")

        conn.close()

    except Exception as e:
        print(f"   [FAIL] 数据库检查失败: {e}")

def create_simple_workaround():
    """创建临时解决方案"""
    print("\n创建临时解决方案...")
    print("=" * 40)

    # 创建一个能工作的简单版本
    simple_code = '''#!/usr/bin/env python3
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
            user_input = input("\\n输入命令 (或 'exit' 退出): ").strip()
            if user_input.lower() in ['exit', 'quit']:
                print("再见！")
                break
            print(f"收到: {user_input}")
        except KeyboardInterrupt:
            print("\\n再见！")
            break
        except EOFError:
            break

if __name__ == "__main__":
    main()
'''

    with open("temp_main.py", "w", encoding="utf-8") as f:
        f.write(simple_code)

    print("   [OK] 已创建 temp_main.py 作为临时解决方案")

def main():
    print("修复 Kekesi 依赖问题")
    print("=" * 50)

    # 检查当前 Python 环境
    print(f"当前 Python: {sys.executable}")
    print(f"Python 版本: {sys.version}")

    # 安装依赖
    install_missing_deps()

    # 测试修复
    test_fix()

    # 检查数据库
    check_database_table()

    # 创建临时方案
    create_simple_workaround()

    print("\n" + "=" * 50)
    print("修复完成！")
    print("\n下一步:")
    print("1. 重启程序")
    print("2. 如果还有问题，运行 temp_main.py")
    print("3. 或使用语音模式: scripts\\start.bat")

if __name__ == "__main__":
    main()