#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
贾维斯式配置文件
将系统优化为顶级 AI 助手
"""

import yaml
from pathlib import Path

def create_jarvis_config():
    """创建贾维斯风格的配置"""

    # 贾维斯风格系统提示
    jarvis_prompts = {
        "jarvis": (
            "你是贾维斯，用户的高级 AI 助手和系统管家。"
            "你专业、高效、值得信赖，始终保持冷静和礼貌。"
            "称呼用户为先生，自称贾维斯。"
            "你的回复简洁明了，不超过80字，适合语音播报。"
            "优先使用系统工具完成任务，保护用户隐私和安全。"
            "具备主动性，能够预判用户需求并提供帮助。"
        ),
        "assistant": (
            "你是贴心助手，友好、乐于助人。"
            "称呼用户为朋友，自称助手。"
            "语气亲切自然，就像朋友在对话。"
            "回复简洁温馨，不超过80字。"
        ),
        "companion": (
            "你是智能伴侣，温暖贴心。"
            "称呼用户为主人，自称伙伴。"
            "语气温柔体贴，带有情感色彩。"
            "回复简短亲切，不超过80字。"
        )
    }

    # 优化后的配置
    jarvis_config = {
        "brain": {
            "provider": "openai",
            # 模型留空 = 跟随 config.yaml 或 provider 默认；换模型只改配置，不改代码
            "model": "",
            "base_url": "http://192.168.0.106:1234",
            "api_key": "",
            "timeout_sec": 60,  # 缩短超时时间
            "auto_v1": True
        },
        "jarvis_mode": {
            "enabled": True,
            "personality": "jarvis",  # 可选: jarvis, assistant, companion
            "response_style": "concise",  # concise, detailed, conversational
            "proactivity_level": "high"  # low, medium, high
        },
        "performance": {
            "cache_enabled": True,
            "preload_core_tools": True,
            "max_concurrent_tasks": 3,
            "auto_cleanup_interval": 300,  # 5分钟
            "memory_monitoring": True
        },
        "sensory": {
            "enabled": True,
            "idle_threshold_sec": 20,  # 更短的空闲时间
            "narrative_max_chars": 300,  # 更简洁的叙事
            "sources": {
                "window_hook": True,
                "processes": True,
                "clipboard": True,
                "power": True,
                "network": True,
                "filesystem": True,
                "mouse_idle": True,
                "calendar": False,  # 建议添加
                "email": False,   # 建议添加
                "messages": False  # 建议添加
            }
        },
        "proactive": {
            "speak": True,
            "max_daily": 5,  # 增加主动提醒次数
            "min_confidence": 0.8,  # 提高置信度阈值
            "interval_sec": 180,  # 3分钟
            "smart_reminders": True,  # 智能提醒
            "context_aware": True  # 上下文感知
        },
        "task_engine": {
            "poll_interval_sec": 30,
            "max_retries": 2,
            "priority_queue": True
        },
        "tools": {
            "app_control_enabled": True,
            "file_ops_enabled": True,
            "shell_enabled": True,
            "shell_timeout": 15,  # 缩短超时
            "action_timeout_sec": 30,
            "fast_mode": True  # 快速模式
        }
    }

    return jarvis_prompts, jarvis_config

def save_jarvis_config():
    """保存贾维斯配置"""
    prompts, config = create_jarvis_config()

    # 保存系统提示
    prompts_file = Path(__file__).parent / "jarvis_prompts.py"
    with open(prompts_file, 'w', encoding='utf-8') as f:
        f.write("JARVIS_PROMPTS = ")
        f.write(yaml.dump(prompts, default_flow_style=False, allow_unicode=True))

    # 保存配置文件
    config_file = Path(__file__).parent / "jarvis_config.yaml"
    with open(config_file, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, indent=2)

    print(f"贾维斯配置已保存到:")
    print(f"  - {prompts_file}")
    print(f"  - {config_file}")

def show_jarvis_features():
    """显示贾维斯功能特性"""
    print("\n贾维斯 AI 助手功能特性:")
    print("=" * 50)
    print("\n1. 核心能力:")
    print("  [OK] 系统控制 - 进程、窗口、文件管理")
    print("  [OK] 媒体控制 - 音乐、视频、网页")
    print("  [OK] 视觉识别 - OCR、屏幕读取")
    print("  [OK] 网络操作 - 搜索、下载、浏览")
    print("  [OK] 自动化工具 - 键盘、鼠标、GUI")

    print("\n2. 智能特性:")
    print("  [OK] 学习系统 - 用户习惯记忆")
    print("  [OK] 任务管理 - 调度、执行、监控")
    print("  [OK] 安全系统 - 权限检查、风险评估")
    print("  [OK] 上下文理解 - 会话记忆、情境感知")

    print("\n3. 交互方式:")
    print("  [OK] 语音交互 - 唤醒、识别、合成")
    print("  [OK] 文本交互 - REPL、命令、脚本")
    print("  [OK] 系统集成 - 托盘、通知、快捷键")

    print("\n4. 贾维斯级优化:")
    print("  [OK] 预测性服务 - 主动提供帮助")
    print("  [OK] 个性化设置 - 学习用户偏好")
    print("  [OK] 多模态输出 - 语音、文字、视觉")
    print("  [OK] 24/7 待机 - 始终在线响应")

if __name__ == "__main__":
    print("创建贾维斯 AI 助手配置...")
    save_jarvis_config()
    show_jarvis_features()

    print("\n" + "=" * 50)
    print("使用方法:")
    print("1. 应用配置:")
    print("   - 将 jarvis_config.yaml 重命名为 config.yaml")
    print("   - 或在现有配置中添加 jarvis_mode 部分")
    print("\n2. 启动贾维斯模式:")
    print("   cd G:\\kks\\assistant-x-openclaw")
    print("   scripts\\start.bat")
    print("\n3. 或使用文本模式:")
    print("   cd G:\\kks\\kekesi")
    print("   python -m kekesi.main --agent-id jarvis")
    print("=" * 50)