#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
资源检查和清理工具
用于诊断和解决多轮对话后的闪退问题
"""

import os
import sys
import time
import sqlite3
from pathlib import Path

def check_database_lock():
    """检查数据库锁定状态"""
    print("检查数据库锁定...")
    db_path = Path(__file__).parent / "data" / "kekesi.db"

    if not db_path.exists():
        print("  ✗ 数据库文件不存在")
        return False

    # 检查 WAL 文件
    wal_path = db_path.with_suffix('.db-wal')
    shm_path = db_path.with_suffix('.db-shm')

    if wal_path.exists() or shm_path.exists():
        print(f"  ⚠ 发现 WAL 文件:")
        if wal_path.exists():
            print(f"    - {wal_path.name} (大小: {wal_path.stat().st_size} 字节)")
        if shm_path.exists():
            print(f"    - {shm_path.name} (大小: {shm_path.stat().st_size} 字节)")
        print("    这可能导致数据库锁定")
        return True
    else:
        print("  ✓ 数据库状态正常")
        return False

def clean_database_files():
    """清理数据库 WAL 文件"""
    print("\n尝试清理数据库文件...")
    db_path = Path(__file__).parent / "data" / "kekesi.db"
    wal_path = db_path.with_suffix('.db-wal')
    shm_path = db_path.with_suffix('.db-shm')

    cleaned = False
    for f in [wal_path, shm_path]:
        if f.exists():
            try:
                f.unlink()
                print(f"  ✓ 已删除: {f.name}")
                cleaned = True
            except Exception as e:
                print(f"  ✗ 删除失败 {f.name}: {e}")

    return cleaned

def check_memory_usage():
    """检查内存使用情况"""
    print("\n检查内存使用...")
    try:
        import psutil
        process = psutil.Process()
        memory_mb = process.memory_info().rss / 1024 / 1024
        print(f"  当前进程内存使用: {memory_mb:.1f} MB")

        if memory_mb > 500:  # 超过500MB
            print("  ⚠ 内存使用较高，可能导致闪退")
            return True
        return False
    except ImportError:
        print("  未安装 psutil，无法检查内存")
        return False

def check_python_objects():
    """检查 Python 对象数量"""
    print("\n检查 Python 对象...")
    import gc

    # 获取对象统计
    obj_count = len(gc.get_objects())
    print(f"  当前 Python 对象数量: {obj_count}")

    # 触发垃圾回收
    collected = gc.collect()
    print(f"  垃圾回收释放了 {collected} 个对象")

    return obj_count > 10000  # 对象过多可能有问题

def test_database_connection():
    """测试数据库连接"""
    print("\n测试数据库连接...")
    db_path = Path(__file__).parent / "data" / "kekesi.db"

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # 简单查询
        cursor.execute("SELECT COUNT(*) FROM memory_digests")
        count = cursor.fetchone()[0]
        print(f"  ✓ 数据库连接正常，digests 表有 {count} 条记录")

        conn.close()
        return True
    except Exception as e:
        print(f"  ✗ 数据库连接失败: {e}")
        return False

def main():
    print("=" * 50)
    print("Kekesi 资源检查工具")
    print("=" * 50)

    issues_found = []

    # 检查各项
    if check_database_lock():
        issues_found.append("数据库锁定")

    memory_high = check_memory_usage()
    if memory_high:
        issues_found.append("内存过高")

    objects_high = check_python_objects()
    if objects_high:
        issues_found.append("对象过多")

    if not test_database_connection():
        issues_found.append("数据库连接问题")

    # 处理发现的问题
    if issues_found:
        print(f"\n发现 {len(issues_found)} 个问题:")
        for issue in issues_found:
            print(f"  - {issue}")

        # 询问是否自动修复
        print("\n是否尝试自动修复？(y/n)")
        response = input().strip().lower()
        if response == 'y':
            if clean_database_files():
                print("✓ 数据库清理完成")
            if objects_high:
                print("✓ 已执行垃圾回收")
    else:
        print("\n✓ 未发现问题，系统运行正常")

    print("\n" + "=" * 50)

if __name__ == "__main__":
    main()