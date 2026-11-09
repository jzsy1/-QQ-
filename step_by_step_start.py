#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
分步启动脚本 - 逐步导入各个模块，定位问题
"""

import sys
from pathlib import Path
import logging

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

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("step_by_step")

def step1_basic_imports():
    """步骤1: 基本导入测试"""
    print("\n步骤1: 基本模块导入")
    try:
        import argparse
        import os
        import json
        from pathlib import Path
        print("✓ 基本模块导入成功")
        return True
    except Exception as e:
        print(f"✗ 基本模块导入失败: {e}")
        return False

def step2_config_check():
    """步骤2: 配置文件检查"""
    print("\n步骤2: 配置文件检查")
    try:
        config_path = current_dir / "config.yaml"
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                import yaml
                config = yaml.safe_load(f)
                print(f"✓ 配置文件加载成功")
                brain = config.get('brain', {}) or {}
                profiles = brain.get('profiles')
                if isinstance(profiles, dict) and profiles:
                    brain = profiles.get(brain.get('active')) or next(
                        (p for p in profiles.values() if isinstance(p, dict)), {}
                    )
                print(f"  - Brain provider: {brain.get('provider', 'unknown')}")
                return True
        else:
            print("✗ 配置文件不存在")
            return False
    except Exception as e:
        print(f"✗ 配置文件检查失败: {e}")
        return False

def step3_core_modules():
    """步骤3: 核心模块导入"""
    print("\n步骤3: 核心模块导入")
    try:
        # 先尝试导入 utils
        from utils import logger as utils_logger
        print("✓ utils 模块导入成功")

        # 尝试导入 config_store
        from config_store import ConfigStore
        print("✓ config_store 模块导入成功")

        return True
    except Exception as e:
        print(f"✗ 核心模块导入失败: {e}")
        return False

def step4_router():
    """步骤4: Router 导入（关键步骤）"""
    print("\n步骤4: Router 导入")
    try:
        # 尝试直接导入 router
        import router
        print("✓ router 模块导入成功")

        # 检查 AgentRouter 类
        if hasattr(router, 'AgentRouter'):
            print("✓ AgentRouter 类存在")
            return True
        else:
            print("✗ AgentRouter 类不存在")
            return False
    except Exception as e:
        print(f"✗ Router 导入失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def step5_simple_test():
    """步骤5: 简单测试"""
    print("\n步骤5: 简单功能测试")
    try:
        import router

        # 尝试初始化一个简单的 router
        config_dir = current_dir
        try:
            router_instance = router.AgentRouter(config_dir=config_dir)
            print("✓ AgentRouter 初始化成功")
            return True
        except Exception as e:
            print(f"✗ AgentRouter 初始化失败: {e}")
            return False
    except Exception as e:
        print(f"✗ 简单测试失败: {e}")
        return False

def main():
    print("=" * 60)
    print("Kekesi v2.0 分步启动诊断")
    print("=" * 60)

    steps = [
        step1_basic_imports,
        step2_config_check,
        step3_core_modules,
        step4_router,
        step5_simple_test,
    ]

    results = []
    for step in steps:
        try:
            result = step()
            results.append(result)
        except Exception as e:
            print(f"步骤执行出错: {e}")
            results.append(False)

    print("\n" + "=" * 60)
    print("诊断结果总结:")
    for i, (step, result) in enumerate(zip(steps, results)):
        status = "✓ 通过" if result else "✗ 失败"
        print(f"  步骤{i+1} ({step.__name__}): {status}")

    if all(results):
        print("\n✓ 所有步骤通过，系统应该可以正常运行！")
    else:
        print(f"\n✗ {results.count(False)} 个步骤失败，需要修复")

    print("=" * 60)

if __name__ == "__main__":
    main()