#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
简化的启动脚本 - 直接复制必要的代码避免导入问题
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
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

logger = logging.getLogger("kekesi.simple")

_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_AGENT_ID = "keks"
_DEFAULT_SESSION_ID = "default"
_ADMIN_PORT_DEFAULT = 18791

def _setup_logging(level: str, config_dir: Path) -> None:
    """配置根日志：控制台 + 文件（可选）。"""
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # 文件日志
    try:
        log_dir = config_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir / "kekesi-simple.log",
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logging.getLogger().addHandler(file_handler)
    except Exception:
        logger.warning("文件日志初始化失败，仅使用控制台日志", exc_info=True)

def _print_reply(reply: str | None, layer: str) -> None:
    """格式化打印 Agent 回复。"""
    if not reply:
        print("(无回复)")
        return
    print(f"\n科科斯: {reply}")
    if layer and str(layer).lower() not in ("empty", "chat", ""):
        print(f"  [路由: {layer}]")

def simple_run_once(text: str) -> int:
    """最简单的单次运行测试"""
    print(f"测试输入: {text}")
    print("这是一个简化的测试模式")
    return 0

def main(argv: list[str] | None = None) -> int:
    """主入口。"""
    parser = argparse.ArgumentParser(
        prog="kekesi.simple",
        description="简化的 Kekesi 入口",
    )
    parser.add_argument(
        "--once",
        type=str,
        default=None,
        help="单次查询模式",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=_DEFAULT_LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help=f"日志级别（默认: {_DEFAULT_LOG_LEVEL}）",
    )

    args = parser.parse_args(argv)

    # 解析配置目录
    config_dir = Path(__file__).resolve().parent

    _setup_logging(args.log_level, config_dir)

    logger.info("简化模式启动中... config_dir=%s", config_dir)

    # 单次查询模式
    if args.once:
        return simple_run_once(args.once)

    print("\n简化模式已启动")
    print("输入消息后回车发送；输入 /exit 退出。")

    while True:
        try:
            user_input = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if not user_input:
            continue

        if user_input in ("/exit", "/quit", "/bye"):
            print("再见。")
            break

        print(f"\n回复: 收到你的消息 - {user_input}")

    return 0

if __name__ == "__main__":
    sys.exit(main())