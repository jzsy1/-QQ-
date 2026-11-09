#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
诊断和修复闪退问题
"""

import os
import sys
import sqlite3
import time
from pathlib import Path

def check_imports():
    """检查导入问题"""
    print("1. 检查导入问题...")
    try:
        # 添加路径
        current_dir = Path(__file__).parent
        sys.path.insert(0, str(current_dir))

        # 尝试导入
        import router
        print("   [OK] router 模块导入成功")
        return True
    except ImportError as e:
        print(f"   [FAIL] 导入失败: {e}")
        return False

def check_database():
    """检查数据库问题"""
    print("\n2. 检查数据库...")
    data_dir = Path(__file__).parent / "data"
    db_path = data_dir / "kekesi.db"

    if not db_path.exists():
        print("   [FAIL] 数据库文件不存在")
        return False

    # 检查是否被锁定
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # 简单查询
        cursor.execute("SELECT COUNT(*) FROM memory_digests")
        count = cursor.fetchone()[0]
        print(f"   [OK] 数据库正常，digests 表有 {count} 条记录")

        conn.close()
        return True
    except Exception as e:
        print(f"   [FAIL] 数据库错误: {e}")
        return False

def check_resources():
    """检查资源使用"""
    print("\n3. 检查资源...")

    # 检查内存
    try:
        import psutil
        process = psutil.Process()
        memory_mb = process.memory_info().rss / 1024 / 1024
        print(f"   [INFO] 内存使用: {memory_mb:.1f} MB")

        if memory_mb > 500:
            print("   [WARN] 内存使用过高")
    except ImportError:
        print("   [INFO] 未安装 psutil，跳过内存检查")

    # 检查文件句柄
    try:
        import resource
        fd_count = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        print(f"   [INFO] 文件句柄限制: {fd_count}")
    except:
        pass

def check_dependencies():
    """检查依赖"""
    print("\n4. 检查依赖...")

    required_modules = [
        "yaml", "httpx", "requests", "chromadb",
        "langchain", "anthropic", "openai"
    ]

    missing = []
    for module in required_modules:
        try:
            __import__(module)
            print(f"   [OK] {module}")
        except ImportError:
            print(f"   [FAIL] {module}")
            missing.append(module)

    if missing:
        print(f"\n   [FAIL] 缺少依赖: {missing}")
        return False
    return True

def test_workaround():
    """测试解决方案"""
    print("\n5. 测试解决方案...")

    # 使用简化模式
    print("   测试简化模式...")
    os.system(f'cd "{Path(__file__).parent}" && python simple_main.py --once "测试"')

    # 使用语音模式
    print("   提示: 可以尝试语音模式")
    print("   cd G:\\kks\\assistant-x-openclaw")
    print("   scripts\\start.bat")

def main():
    print("诊断 Kekesi 闪退问题")
    print("=" * 50)

    issues = []

    # 执行检查
    if not check_imports():
        issues.append("导入问题")

    if not check_database():
        issues.append("数据库问题")

    if not check_dependencies():
        issues.append("依赖问题")

    check_resources()

    # 输出结果
    print("\n" + "=" * 50)
    print("诊断结果:")

    if issues:
        print(f"发现 {len(issues)} 个问题:")
        for issue in issues:
            print(f"  - {issue}")

        print("\n建议解决方案:")
        test_workaround()
    else:
        print("未发现问题，可能其他原因导致闪退")
        print("建议:")
        print("1. 检查系统资源")
        print("2. 尝试重启程序")
        print("3. 查看日志文件")

if __name__ == "__main__":
    main()