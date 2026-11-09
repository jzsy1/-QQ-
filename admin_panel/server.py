# -*- coding: utf-8 -*-
"""QQ 大脑独立控制后台 (Web Admin Panel)。

提供轻量、现代、零第三方依赖的 Web 控制面板：
- 运行状态监控与 QQ 服务启停
- 大脑模型配置 (OpenAI / Ollama / DeepSeek / 本地 vLLM) 与一键连通性测试
- 人设预设切换与系统提示词在线调控
- QQ 白名单、超管与主人身份配置
- 用户身份绑定表管理与独立记忆概览
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 确保 skills 与项目根目录可被导入
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PROJECT_ROOT / "skills"
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import yaml
from kekesi_common.llm import SimpleBrain, SimpleBrainConfig, load_simple_brain, _active_brain_section
from kekesi_common.personas import PersonaStore, resolve_system_prompt
import im.qq.brain as qq_brain
from im.qq.config import QQConfig, load_config
from im.qq.service import get_service, qq_status, start_qq, stop_qq
from im.qq.identity import IdentityMap

logger = logging.getLogger("AdminPanel")

DEFAULT_PORT = 18888


def _read_config_raw() -> dict:
    cfg_file = PROJECT_ROOT / "config.yaml"
    try:
        if cfg_file.exists():
            return yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("读取 config.yaml 失败: %s", exc)
    return {}


def _save_config_raw(data: dict) -> bool:
    cfg_file = PROJECT_ROOT / "config.yaml"
    try:
        cfg_file.write_text(
            yaml.dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return True
    except Exception as exc:
        logger.error("写入 config.yaml 失败: %s", exc)
        return False


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QQ 大脑控制后台</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --card-hover: #24344d;
      --border: #334155;
      --primary: #38bdf8;
      --primary-hover: #0284c7;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --text: #f8fafc;
      --text-muted: #94a3b8;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding-bottom: 40px;
    }
    .header {
      background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
      border-bottom: 1px solid var(--border);
      padding: 18px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 50;
      backdrop-filter: blur(8px);
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand h1 { font-size: 1.25rem; font-weight: 700; color: #fff; letter-spacing: 0.5px; }
    .badge-status {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 0.8rem;
      font-weight: 600;
      background: rgba(16, 185, 129, 0.15);
      color: var(--success);
      border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .badge-status.stopped {
      background: rgba(239, 68, 68, 0.15);
      color: var(--danger);
      border-color: rgba(239, 68, 68, 0.3);
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
    .nav-tabs {
      display: flex;
      gap: 8px;
      padding: 12px 28px;
      background: rgba(15, 23, 42, 0.8);
      border-bottom: 1px solid var(--border);
      overflow-x: auto;
    }
    .nav-btn {
      padding: 8px 18px;
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-muted);
      font-size: 0.9rem;
      font-weight: 600;
      border-radius: 8px;
      cursor: pointer;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
      gap: 8px;
      white-space: nowrap;
    }
    .nav-btn:hover { background: var(--card); color: #fff; }
    .nav-btn.active {
      background: var(--card);
      color: var(--primary);
      border-color: var(--border);
    }
    .container {
      max-width: 1100px;
      margin: 24px auto;
      padding: 0 20px;
    }
    .tab-pane { display: none; }
    .tab-pane.active { display: block; animation: fadeIn 0.25s ease; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px;
      margin-bottom: 24px;
      box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
    }
    .card-title {
      font-size: 1.1rem;
      font-weight: 700;
      color: #fff;
      margin-bottom: 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 18px; }
    .grid-3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }
    .form-group { margin-bottom: 16px; }
    label {
      display: block;
      font-size: 0.85rem;
      font-weight: 600;
      color: var(--text-muted);
      margin-bottom: 6px;
    }
    input, select, textarea {
      width: 100%;
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 8px;
      color: #fff;
      padding: 10px 14px;
      font-size: 0.9rem;
      transition: border-color 0.2s ease;
    }
    input:focus, select:focus, textarea:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.2);
    }
    textarea { resize: vertical; min-height: 100px; font-family: inherit; }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 10px 20px;
      border-radius: 8px;
      font-size: 0.9rem;
      font-weight: 600;
      border: none;
      cursor: pointer;
      transition: all 0.2s ease;
    }
    .btn-primary { background: var(--primary); color: #0f172a; }
    .btn-primary:hover { background: var(--primary-hover); }
    .btn-success { background: var(--success); color: #fff; }
    .btn-danger { background: var(--danger); color: #fff; }
    .btn-outline { background: transparent; border: 1px solid var(--border); color: #fff; }
    .btn-outline:hover { background: rgba(255, 255, 255, 0.05); }
    .btn-sm { padding: 6px 12px; font-size: 0.8rem; border-radius: 6px; }
    .stat-box {
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      text-align: center;
    }
    .stat-val { font-size: 1.5rem; font-weight: 700; color: var(--primary); }
    .stat-lbl { font-size: 0.8rem; color: var(--text-muted); margin-top: 4px; }
    .preset-card {
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      cursor: pointer;
      transition: all 0.2s ease;
      background: #0f172a;
    }
    .preset-card:hover { border-color: var(--primary); }
    .preset-card.selected { border-color: var(--primary); background: rgba(56, 189, 248, 0.08); }
    .preset-title { font-weight: 700; color: #fff; font-size: 0.95rem; }
    .preset-desc { font-size: 0.8rem; color: var(--text-muted); margin-top: 4px; }
    .table-responsive { width: 100%; overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.85rem; }
    th, td { padding: 12px 14px; border-bottom: 1px solid var(--border); }
    th { color: var(--text-muted); font-weight: 600; }
    .toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      padding: 12px 20px;
      background: #1e293b;
      color: #fff;
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: 0 10px 15px -3px rgba(0,0,0,0.5);
      z-index: 100;
      transform: translateY(100px);
      opacity: 0;
      transition: all 0.3s ease;
    }
    .toast.show { transform: translateY(0); opacity: 1; }
    .log-box {
      background: #090d16;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      font-family: Consolas, monospace;
      font-size: 0.85rem;
      color: #38bdf8;
      max-height: 180px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }
  </style>
</head>
<body>

  <header class="header">
    <div class="brand">
      <span style="font-size: 1.5rem;">🤖</span>
      <div>
        <h1>QQ 大脑控制后台</h1>
        <div style="font-size: 0.75rem; color: var(--text-muted);">独立架构 · 模型热更 · 人设调控 · 记忆隔离</div>
      </div>
    </div>
    <div style="display: flex; gap: 12px; align-items: center;">
      <div id="statusBadge" class="badge-status">
        <span class="dot"></span>
        <span id="statusText">检测中...</span>
      </div>
      <button id="toggleServiceBtn" class="btn btn-sm btn-primary" onclick="toggleQQService()">启动接入</button>
    </div>
  </header>

  <nav class="nav-tabs">
    <button class="nav-btn active" onclick="switchTab('dashboard')">📊 运行概览</button>
    <button class="nav-btn" onclick="switchTab('model')">🧠 大脑方案与微调</button>
    <button class="nav-btn" onclick="switchTab('vision')">👁️ 视觉收图理解</button>
    <button class="nav-btn" onclick="switchTab('vector')">🧩 向量与记忆切片</button>
    <button class="nav-btn" onclick="switchTab('persona')">🎭 人设与提示词</button>
    <button class="nav-btn" onclick="switchTab('qq')">⚙️ QQ 与白名单</button>
    <button class="nav-btn" onclick="switchTab('identity')">👥 身份与记忆</button>
  </nav>

  <main class="container">

    <!-- 1. 仪表盘 -->
    <div id="tab-dashboard" class="tab-pane active">
      <div class="grid-3" style="margin-bottom: 24px;">
        <div class="stat-box">
          <div class="stat-val" id="statReceived">-</div>
          <div class="stat-lbl">接收消息总数</div>
        </div>
        <div class="stat-box">
          <div class="stat-val" id="statResponded">-</div>
          <div class="stat-lbl">生成回复总数</div>
        </div>
        <div class="stat-box">
          <div class="stat-val" id="statActiveModel">-</div>
          <div class="stat-lbl">当前运行模型</div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">
          <span>📡 QQ 服务即时状态</span>
          <button class="btn btn-sm btn-outline" onclick="refreshStatus()">刷新状态</button>
        </div>
        <div class="log-box" id="liveStatusText">正在读取服务状态...</div>
      </div>

      <div class="card">
        <div class="card-title">⚡ 快速连通性诊断</div>
        <div style="display: flex; gap: 12px; align-items: center;">
          <input type="text" id="quickTestMsg" value="回复两个字：正常" placeholder="输入测试语句">
          <button class="btn btn-primary" onclick="runLLMTest(true)" style="white-space: nowrap;">测试大模型</button>
        </div>
        <div id="quickTestResult" style="margin-top: 12px; font-size: 0.9rem; color: var(--text-muted);"></div>
      </div>
    </div>

    <!-- 2. 模型配置 -->
    <div id="tab-model" class="tab-pane">
      <div class="card" style="border: 1px solid rgba(56, 189, 248, 0.4); background: rgba(15, 23, 42, 0.85); margin-bottom: 20px;">
        <div class="card-title">
          <div style="display: flex; align-items: center; gap: 8px;">
            <span style="font-size: 1.25rem;">🎯</span>
            <span>多大脑方案 (Brain Profiles) 快捷切换</span>
          </div>
          <span id="activeProfileBadge" class="badge-status" style="font-size: 0.8rem;">当前生效: 加载中</span>
        </div>
        <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 14px;">
          系统支持保存多套大模型方案（如 DeepSeek官方、硅基流动、千问、谷歌云等）。在此直接选择方案并点击一键切换，即可实现秒级热更新，无需重启后台服务。
        </p>
        <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
          <select id="profileSelector" style="flex: 1; min-width: 280px; max-width: 520px;" onchange="onProfileSelectChange(this.value)">
            <option value="">正在加载模型方案列表...</option>
          </select>
          <button class="btn btn-primary" onclick="switchActiveProfile()" id="btnSwitchProfile">⚡ 一键切换为此方案生效</button>
        </div>
      </div>

      <div class="card">
        <div class="card-title">
          <span>🧠 大语言模型 (LLM) 参数微调</span>
          <button class="btn btn-sm btn-outline" onclick="runLLMTest(false)">测试连通性</button>
        </div>
        <div class="grid-2">
          <div class="form-group">
            <label>Provider 协议类型</label>
            <select id="llmProvider">
              <option value="ollama">Ollama (本地)</option>
              <option value="openai">OpenAI 兼容协议 (SiliconFlow / DeepSeek / vLLM / 其它)</option>
            </select>
          </div>
          <div class="form-group">
            <label>API 基础地址 (Base URL)</label>
            <input type="text" id="llmBaseUrl" placeholder="http://127.0.0.1:11434 或 https://api.siliconflow.cn/v1">
          </div>
        </div>
        <div class="grid-2">
          <div class="form-group">
            <label>模型名称 (Model)</label>
            <input type="text" id="llmModel" placeholder="qwen2.5:3b 或 deepseek-chat">
          </div>
          <div class="form-group">
            <label>API Key (密钥)</label>
            <input type="password" id="llmApiKey" placeholder="sk-...">
          </div>
        </div>
        <div class="grid-2">
          <div class="form-group">
            <label>超时时间 (秒)</label>
            <input type="number" id="llmTimeout" value="60">
          </div>
          <div class="form-group">
            <label>快速预设填充</label>
            <select onchange="applyModelPreset(this.value)">
              <option value="">-- 选择常用预设快捷填充 --</option>
              <option value="ollama_qwen">本地 Ollama (qwen2.5:3b)</option>
              <option value="silicon_deepseek">SiliconFlow (deepseek-ai/DeepSeek-V3)</option>
              <option value="deepseek_official">DeepSeek 官方 (deepseek-chat)</option>
            </select>
          </div>
        </div>
        <div style="margin-top: 10px;">
          <button class="btn btn-primary" onclick="saveModelConfig()">保存模型配置并热加载</button>
        </div>
      </div>

    </div>

    <!-- 视觉理解配置 -->
    <div id="tab-vision" class="tab-pane">
      <div class="card">
        <div class="card-title">
          <div style="display: flex; align-items: center; gap: 8px;">
            <span style="font-size: 1.25rem;">👁️</span>
            <span>视觉收图理解 (Vision) 详细配置</span>
          </div>
          <span id="visionStatusBadge" class="badge-status">状态检测中</span>
        </div>
        <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 16px;">
          当群聊或私聊收到图片消息时，自动压缩送入多模态视觉大模型生成文字描述，再交由大脑决策回复。
        </p>

        <div class="grid-2">
          <div class="form-group">
            <label>是否启用视觉理解</label>
            <select id="visionEnabled">
              <option value="true">开启 (收到图片自动识别内容)</option>
              <option value="false">关闭 (忽略图片内容)</option>
            </select>
          </div>
          <div class="form-group">
            <label>视觉模型名称 (Vision Model)</label>
            <input type="text" id="visionModel" placeholder="Qwen/Qwen3-VL-30B-A3B-Instruct 或 gemini-3.8-flash">
          </div>
        </div>

        <div class="grid-2">
          <div class="form-group">
            <label>视觉 Base URL (留空默认复用主模型 Base URL)</label>
            <input type="text" id="visionBaseUrl" placeholder="https://api.siliconflow.cn/v1">
          </div>
          <div class="form-group">
            <label>视觉专属 API Key (留空默认复用当前模型 Key)</label>
            <input type="password" id="visionApiKey" placeholder="sk-...">
          </div>
        </div>

        <div class="grid-3">
          <div class="form-group">
            <label>超时时间 (秒)</label>
            <input type="number" id="visionTimeout" value="20">
          </div>
          <div class="form-group">
            <label>单条消息最多看图数</label>
            <input type="number" id="visionMaxImages" value="2">
          </div>
          <div class="form-group">
            <label>最大图像边长 (像素)</label>
            <input type="number" id="visionMaxEdge" value="1024">
          </div>
        </div>

        <div class="form-group">
          <label>快速预设填充</label>
          <select onchange="applyVisionPreset(this.value)">
            <option value="">-- 选择常用视觉模型快捷填充 --</option>
            <option value="silicon_qwen_vl">硅基流动 Qwen3-VL (Qwen/Qwen3-VL-30B-A3B-Instruct)</option>
            <option value="deepseek_chat">DeepSeek 多模态 (deepseek-chat)</option>
            <option value="gemini_flash">Google Gemini (gemini-3.8-flash)</option>
          </select>
        </div>

        <div style="margin-top: 10px;">
          <button class="btn btn-primary" onclick="saveVisionConfig()">保存视觉理解配置</button>
        </div>
      </div>
    </div>

    <!-- 向量与切片配置 -->
    <div id="tab-vector" class="tab-pane">
      <div class="card">
        <div class="card-title">
          <div style="display: flex; align-items: center; gap: 8px;">
            <span style="font-size: 1.25rem;">🧩</span>
            <span>记忆向量嵌入模型 (Embedding)</span>
          </div>
          <button class="btn btn-sm btn-outline" onclick="runEmbeddingTest()">测试向量连通性</button>
        </div>
        <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 16px;">
          向量嵌入模型负责将对话记忆切片转化为高维数学向量，用于语义级模糊记忆检索与事实关联。
        </p>

        <div class="grid-2">
          <div class="form-group">
            <label>是否启用向量索引</label>
            <select id="embEnabled">
              <option value="true">开启 (启用语义向量索引与召回)</option>
              <option value="false">关闭 (降级为纯关键词字面匹配)</option>
            </select>
          </div>
          <div class="form-group">
            <label>向量模型名称 (Model)</label>
            <input type="text" id="embModel" placeholder="BAAI/bge-m3 或 text-embedding-3-small">
          </div>
        </div>

        <div class="grid-2">
          <div class="form-group">
            <label>向量 API Base URL</label>
            <input type="text" id="embBaseUrl" placeholder="https://api.siliconflow.cn/v1">
          </div>
          <div class="form-group">
            <label>向量 API Key</label>
            <input type="password" id="embApiKey" placeholder="sk-...">
          </div>
        </div>

        <div class="grid-2">
          <div class="form-group">
            <label>超时时间 (秒)</label>
            <input type="number" id="embTimeout" value="10">
          </div>
          <div class="form-group">
            <label>快速预设填充</label>
            <select onchange="applyEmbeddingPreset(this.value)">
              <option value="">-- 选择常用向量模型预设 --</option>
              <option value="silicon_bgem3">硅基流动 BAAI/bge-m3 (推荐，1024维)</option>
              <option value="openai_small">OpenAI text-embedding-3-small (1536维)</option>
              <option value="ollama_nomic">本地 Ollama (nomic-embed-text)</option>
            </select>
          </div>
        </div>

        <div id="embTestResult" style="margin-top: 8px; margin-bottom: 12px; font-size: 0.85rem; color: var(--text-muted);"></div>

        <div style="margin-top: 10px;">
          <button class="btn btn-primary" onclick="saveEmbeddingConfig()">保存向量模型配置</button>
        </div>
      </div>

      <div class="card">
        <div class="card-title">
          <div style="display: flex; align-items: center; gap: 8px;">
            <span style="font-size: 1.25rem;">✂️</span>
            <span>记忆文本切片与检索召回参数 (Chunking & Retrieval)</span>
          </div>
        </div>
        <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 16px;">
          调节对话切分粒度与记忆检索过滤阈值，平衡记忆精准度与上下文窗口占用。
        </p>

        <div class="grid-2">
          <div class="form-group">
            <label>切片目标字符数 (Chunk Chars)</label>
            <input type="number" id="vecChunkChars" value="300" min="50" max="2000">
            <span style="font-size: 0.75rem; color: var(--text-muted);">每个记忆文本块的最大字符数，默认 300</span>
          </div>
          <div class="form-group">
            <label>切片重叠字符数 (Chunk Overlap)</label>
            <input type="number" id="vecChunkOverlap" value="60" min="0" max="500">
            <span style="font-size: 0.75rem; color: var(--text-muted);">相邻块之间的重叠字数，防止切断句子，默认 60</span>
          </div>
        </div>

        <div class="grid-2">
          <div class="form-group">
            <label>单次检索召回条数 (Top-K)</label>
            <input type="number" id="vecTopK" value="3" min="1" max="20">
            <span style="font-size: 0.75rem; color: var(--text-muted);">每次回复最多召回的相关记忆条目数，默认 3</span>
          </div>
          <div class="form-group">
            <label>最低相似度阈值 (Min Score 0.0 ~ 1.0)</label>
            <input type="number" id="vecMinScore" value="0.45" step="0.05" min="0.0" max="1.0">
            <span style="font-size: 0.75rem; color: var(--text-muted);">低于此余弦相似度的碎片将被过滤，默认 0.45</span>
          </div>
        </div>

        <div style="margin-top: 10px;">
          <button class="btn btn-primary" onclick="saveVectorChunkConfig()">保存切片与检索参数</button>
        </div>
      </div>
    </div>

    <!-- 3. 人设与提示词 -->
    <div id="tab-persona" class="tab-pane">
      <div class="card">
        <div class="card-title">🎭 人设方案快捷切换</div>
        <div class="grid-3" style="margin-bottom: 18px;">
          <div class="preset-card" id="preset-keks" onclick="selectPersonaPreset('keks')">
            <div class="preset-title">🐟 岁岁念 (鱼鱼)</div>
            <div class="preset-desc">俏皮自称“鱼鱼”，认“鱼片”为现实主人，对群友亲和但守住边界。</div>
          </div>
          <div class="preset-card" id="preset-jarvis" onclick="selectPersonaPreset('jarvis')">
            <div class="preset-title">🎩 贾维斯 (管家)</div>
            <div class="preset-desc">专业高效、彬彬有礼的管家风格，称呼主人为先生。</div>
          </div>
          <div class="preset-card" id="preset-custom" onclick="selectPersonaPreset('custom')">
            <div class="preset-title">💡 纯自定义人设</div>
            <div class="preset-desc">完全根据下方提示词框定义独特性格与对话准则。</div>
          </div>
        </div>

        <div class="form-group">
          <label>当前生效人设标识 (Agent ID)</label>
          <input type="text" id="personaAgentId" value="keks">
        </div>

        <div class="grid-2">
          <div class="form-group">
            <label>AI 自称 (Wake Words，逗号分隔)</label>
            <input type="text" id="personaWakeWords" value="鱼鱼, 小金鱼">
          </div>
          <div class="form-group">
            <label>现实主人称呼 (Owner Names，逗号分隔)</label>
            <input type="text" id="personaOwnerNames" value="鱼片, 主人">
          </div>
        </div>

        <div class="form-group">
          <label>系统提示词 (System Prompt - 留空则采用内置标准准则)</label>
          <textarea id="personaSystemPrompt" style="height: 160px;" placeholder="在此输入自定义系统提示词..."></textarea>
        </div>

        <button class="btn btn-primary" onclick="savePersonaConfig()">保存人设提示词</button>
      </div>
    </div>

    <!-- 4. QQ 与白名单 -->
    <div id="tab-qq" class="tab-pane">
      <div class="card">
        <div class="card-title">⚙️ NapCat 通信连接</div>
        <div class="grid-2">
          <div class="form-group">
            <label>NapCat WebSocket 地址 (OneBot v11 正向)</label>
            <input type="text" id="qqWsUrl" placeholder="ws://127.0.0.1:3001">
          </div>
          <div class="form-group">
            <label>Access Token (访问令牌，无则留空)</label>
            <input type="password" id="qqToken" placeholder="cccaa123">
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">🛡️ 白名单与权限列表 (留空代表不设限制全响应)</div>
        <div class="grid-2">
          <div class="form-group">
            <label>群聊白名单群号 (逗号分隔)</label>
            <input type="text" id="qqGroups" placeholder="留空允许所有群，多个用逗号隔开">
          </div>
          <div class="form-group">
            <label>私聊白名单 QQ 号 (逗号分隔)</label>
            <input type="text" id="qqPrivates" placeholder="留空允许所有私聊，多个用逗号隔开">
          </div>
        </div>
        <div class="grid-2">
          <div class="form-group">
            <label>超管 QQ 号 (可用 /bind 等控制命令，逗号分隔)</label>
            <input type="text" id="qqSuperUsers" placeholder="例如: 10001, 10002">
          </div>
          <div class="form-group">
            <label>现实主人 QQ 号 (群内免 @ 自动接话，逗号分隔)</label>
            <input type="text" id="qqOwners" placeholder="例如: 10001">
          </div>
        </div>
        <div class="grid-2">
          <div class="form-group">
            <label>单条回复最大字数限制</label>
            <input type="number" id="qqMaxChars" value="120">
          </div>
          <div class="form-group">
            <label>消息攒批等待时长 (毫秒)</label>
            <input type="number" id="qqBatchMs" value="3000">
          </div>
        </div>
        <button class="btn btn-primary" onclick="saveQQSettings()">保存 QQ 业务参数</button>
      </div>
    </div>

    <!-- 5. 身份与记忆 -->
    <div id="tab-identity" class="tab-pane">
      <div class="card">
        <div class="card-title">👥 QQ 跨平台手动身份绑定</div>
        <div style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 14px;">
          QQ 默认使用自身命名空间独立记忆（qq_QQ号）。如需打通历史或跨平台观众档案，可在此显式手动绑定。
        </div>
        <div style="display: flex; gap: 12px; margin-bottom: 16px;">
          <input type="text" id="bindQqId" placeholder="QQ 号" style="max-width: 200px;">
          <input type="text" id="bindAnchor" placeholder="外部身份标识 Anchor (填 unbind 为解绑)">
          <button class="btn btn-primary" onclick="submitBind()" style="white-space: nowrap;">绑定 / 更新</button>
        </div>

        <div class="table-responsive">
          <table>
            <thead>
              <tr>
                <th>QQ 号</th>
                <th>绑定外部身份 (Anchor)</th>
                <th>方式</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody id="bindingsTableBody">
              <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">暂无手动绑定记录</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-title">💾 独立记忆命名空间概览</div>
        <div style="font-size: 0.9rem; color: var(--text-muted);">
          <p>当前隔离命名空间: <strong style="color: var(--primary);">qq</strong></p>
          <p>存储目录: <code id="memDirDisplay">skills/im/qq/memory</code></p>
          <p style="margin-top: 8px;">状态: 具备 `namespace.json` 强校验保护，物理隔离第三方业务数据，绝不发生自动污染。</p>
        </div>
      </div>
    </div>

  </main>

  <div id="toast" class="toast">操作成功</div>

  <script>
    let currentConfig = {};
    let allProfiles = [];
    let currentActiveProfileId = '';

    function switchTab(tabId) {
      document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('.nav-btn').forEach(el => el.classList.remove('active'));
      document.getElementById('tab-' + tabId).classList.add('active');
      event.target.classList.add('active');
    }

    function showToast(msg, isError=false) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.style.borderColor = isError ? 'var(--danger)' : 'var(--success)';
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 2500);
    }

    async function fetchJSON(url, opts={}) {
      const resp = await fetch(url, opts);
      return await resp.json();
    }

    function renderProfiles(profiles, activeId) {
      allProfiles = profiles || [];
      currentActiveProfileId = activeId || '';
      const sel = document.getElementById('profileSelector');
      const badge = document.getElementById('activeProfileBadge');
      if(!sel) return;
      sel.innerHTML = '';
      if(!allProfiles.length) {
        sel.innerHTML = '<option value="">未检测到预设方案，请手动配置</option>';
        if(badge) badge.innerText = '当前生效: 自由配置';
        return;
      }
      allProfiles.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.id;
        const isActive = (p.id === currentActiveProfileId);
        opt.textContent = `${p.name || p.id} [${p.model}] ${isActive ? ' ★ (当前生效)' : ''}`;
        if(isActive) opt.selected = true;
        sel.appendChild(opt);
      });
      const activeObj = allProfiles.find(p => p.id === currentActiveProfileId);
      if(activeObj && badge) {
        badge.innerText = `当前生效: ${activeObj.name || activeObj.id} (${activeObj.model})`;
      }
    }

    function onProfileSelectChange(pid) {
      const p = allProfiles.find(x => x.id === pid);
      if(!p) return;
      document.getElementById('llmProvider').value = p.provider || 'openai';
      document.getElementById('llmBaseUrl').value = p.base_url || '';
      document.getElementById('llmModel').value = p.model || '';
      document.getElementById('llmApiKey').value = p.api_key || '';
      document.getElementById('llmTimeout').value = p.timeout_sec || 60;
    }

    async function switchActiveProfile() {
      const sel = document.getElementById('profileSelector');
      const pid = sel.value;
      if(!pid) return;
      const btn = document.getElementById('btnSwitchProfile');
      btn.innerText = '正在切换...';
      try {
        const res = await fetchJSON('/api/brain-profiles/switch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ active: pid })
        });
        if(res.ok) {
          showToast(`已成功切换为【${res.name}】(${res.model})，即刻生效！`);
          await loadAll();
        } else {
          showToast('切换失败: ' + (res.error || '未知错误'), true);
        }
      } catch(e) {
        showToast('请求异常: ' + e, true);
      } finally {
        btn.innerText = '⚡ 一键切换为此方案生效';
      }
    }

    async function loadAll() {
      try {
        const data = await fetchJSON('/api/config');
        currentConfig = data;
        populateUI(data);
        refreshStatus();
        loadBindings();
      } catch(e) {
        showToast('读取配置失败: ' + e, true);
      }
    }

    function populateUI(data) {
      const brain = data.brain || {};
      const qq = data.qq || {};

      // 方案切换列表
      renderProfiles(data.profiles || [], data.active_profile || (brain && brain.active) || '');

      // 模型
      document.getElementById('llmProvider').value = brain.provider || 'ollama';
      document.getElementById('llmBaseUrl').value = brain.base_url || '';
      document.getElementById('llmModel').value = brain.model || '';
      document.getElementById('llmApiKey').value = brain.api_key || '';
      document.getElementById('llmTimeout').value = brain.timeout_sec || 60;
      document.getElementById('statActiveModel').innerText = brain.model || '未设定';

      // 视觉理解 (Vision)
      const vis = data.vision || {};
      const visEnabled = (vis.enabled !== false);
      const selVisEnabled = document.getElementById('visionEnabled');
      if(selVisEnabled) selVisEnabled.value = visEnabled ? 'true' : 'false';
      document.getElementById('visionModel').value = vis.model || '';
      document.getElementById('visionBaseUrl').value = vis.base_url || '';
      document.getElementById('visionApiKey').value = vis.api_key || '';
      document.getElementById('visionTimeout').value = vis.timeout_sec || 20;
      document.getElementById('visionMaxImages').value = vis.max_images || 2;
      document.getElementById('visionMaxEdge').value = vis.max_edge || 1024;
      const visBadge = document.getElementById('visionStatusBadge');
      if(visBadge) {
        visBadge.className = visEnabled ? 'badge-status' : 'badge-status stopped';
        visBadge.innerText = visEnabled ? '已启用 (' + (vis.model || '未设定') + ')' : '未启用';
      }

      // 向量嵌入 (Embedding)
      const emb = data.embedding || {};
      const embEnabled = (emb.enabled !== false);
      const selEmbEnabled = document.getElementById('embEnabled');
      if(selEmbEnabled) selEmbEnabled.value = embEnabled ? 'true' : 'false';
      document.getElementById('embModel').value = emb.model || '';
      document.getElementById('embBaseUrl').value = emb.base_url || '';
      document.getElementById('embApiKey').value = emb.api_key || '';
      document.getElementById('embTimeout').value = emb.timeout_sec || 10;

      // 切片与召回 (Vectors & Chunking)
      const vec = data.vectors || {};
      document.getElementById('vecChunkChars').value = vec.chunk_chars || 300;
      document.getElementById('vecChunkOverlap').value = vec.chunk_overlap || 60;
      document.getElementById('vecTopK').value = vec.top_k || 3;
      document.getElementById('vecMinScore').value = (vec.min_score !== undefined) ? vec.min_score : 0.45;

      // 人设
      document.getElementById('personaAgentId').value = qq.agent_id || '';
      document.getElementById('personaWakeWords').value = (qq.wake_words || []).join(', ');
      document.getElementById('personaOwnerNames').value = (qq.owner_names || []).join(', ');
      document.getElementById('personaSystemPrompt').value = qq.system_prompt || '';

      // QQ
      document.getElementById('qqWsUrl').value = qq.ws_url || '';
      document.getElementById('qqToken').value = qq.access_token || '';
      document.getElementById('qqGroups').value = (qq.groups || []).join(', ');
      document.getElementById('qqPrivates').value = (qq.privates || []).join(', ');
      document.getElementById('qqSuperUsers').value = (qq.super_users || []).join(', ');
      document.getElementById('qqOwners').value = (qq.owner_qqs || []).join(', ');
      document.getElementById('qqMaxChars').value = qq.max_reply_chars || 120;
      document.getElementById('qqBatchMs').value = qq.batch_ms || 3000;
    }

    async function refreshStatus() {
      try {
        const res = await fetchJSON('/api/status');
        const badge = document.getElementById('statusBadge');
        const btn = document.getElementById('toggleServiceBtn');
        const txt = document.getElementById('statusText');
        const liveTxt = document.getElementById('liveStatusText');

        if (res.running) {
          badge.className = 'badge-status';
          txt.innerText = '运行中';
          btn.innerText = '停止服务';
          btn.className = 'btn btn-sm btn-danger';
        } else {
          badge.className = 'badge-status stopped';
          txt.innerText = '已停止';
          btn.innerText = '启动服务';
          btn.className = 'btn btn-sm btn-primary';
        }

        liveTxt.innerText = res.status_text || '无状态';
        document.getElementById('statReceived').innerText = res.stats?.received || 0;
        document.getElementById('statResponded').innerText = res.stats?.responded || 0;
      } catch(e) {
        document.getElementById('liveStatusText').innerText = '获取状态失败: ' + e;
      }
    }

    async function toggleQQService() {
      const res = await fetchJSON('/api/status');
      const action = res.running ? '/api/qq/stop' : '/api/qq/start';
      const r = await fetchJSON(action, { method: 'POST' });
      showToast(r.message || (r.ok ? '操作成功' : '失败'), !r.ok);
      refreshStatus();
    }

    async function saveModelConfig() {
      const payload = {
        provider: document.getElementById('llmProvider').value,
        base_url: document.getElementById('llmBaseUrl').value.trim(),
        model: document.getElementById('llmModel').value.trim(),
        api_key: document.getElementById('llmApiKey').value.trim(),
        timeout_sec: parseInt(document.getElementById('llmTimeout').value || 60),
      };
      const r = await fetchJSON('/api/config/brain', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      showToast(r.ok ? '模型配置已更新并热加载！' : r.error, !r.ok);
      if(r.ok) refreshStatus();
    }

    async function saveVisionConfig() {
      const payload = {
        vision_enabled: document.getElementById('visionEnabled').value === 'true',
        vision_model: document.getElementById('visionModel').value.trim(),
        vision_base_url: document.getElementById('visionBaseUrl').value.trim(),
        vision_api_key: document.getElementById('visionApiKey').value.trim(),
        vision_timeout_sec: parseInt(document.getElementById('visionTimeout').value || 20),
        vision_max_images: parseInt(document.getElementById('visionMaxImages').value || 2),
        vision_max_edge: parseInt(document.getElementById('visionMaxEdge').value || 1024),
      };
      const r = await fetchJSON('/api/config/vision', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      showToast(r.ok ? '视觉理解配置已成功保存！' : r.error, !r.ok);
      if(r.ok) loadAll();
    }

    async function saveEmbeddingConfig() {
      const payload = {
        enabled: document.getElementById('embEnabled').value === 'true',
        model: document.getElementById('embModel').value.trim(),
        base_url: document.getElementById('embBaseUrl').value.trim(),
        api_key: document.getElementById('embApiKey').value.trim(),
        timeout_sec: parseInt(document.getElementById('embTimeout').value || 10),
      };
      const r = await fetchJSON('/api/config/embedding', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      showToast(r.ok ? '向量嵌入模型配置已保存！' : r.error, !r.ok);
      if(r.ok) loadAll();
    }

    async function saveVectorChunkConfig() {
      const payload = {
        chunk_chars: parseInt(document.getElementById('vecChunkChars').value || 300),
        chunk_overlap: parseInt(document.getElementById('vecChunkOverlap').value || 60),
        top_k: parseInt(document.getElementById('vecTopK').value || 3),
        min_score: parseFloat(document.getElementById('vecMinScore').value || 0.45),
      };
      const r = await fetchJSON('/api/config/vectors', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      showToast(r.ok ? '切片与检索召回参数已保存！' : r.error, !r.ok);
      if(r.ok) loadAll();
    }

    function applyVisionPreset(val) {
      if(val === 'silicon_qwen_vl') {
        document.getElementById('visionModel').value = 'Qwen/Qwen3-VL-30B-A3B-Instruct';
        document.getElementById('visionBaseUrl').value = 'https://api.siliconflow.cn/v1';
      } else if(val === 'deepseek_chat') {
        document.getElementById('visionModel').value = 'deepseek-chat';
        document.getElementById('visionBaseUrl').value = 'https://api.deepseek.com';
      } else if(val === 'gemini_flash') {
        document.getElementById('visionModel').value = 'gemini-3.8-flash';
        document.getElementById('visionBaseUrl').value = 'https://generativelanguage.googleapis.com';
      }
    }

    function applyEmbeddingPreset(val) {
      if(val === 'silicon_bgem3') {
        document.getElementById('embModel').value = 'BAAI/bge-m3';
        document.getElementById('embBaseUrl').value = 'https://api.siliconflow.cn/v1';
        document.getElementById('embTimeout').value = 10;
      } else if(val === 'openai_small') {
        document.getElementById('embModel').value = 'text-embedding-3-small';
        document.getElementById('embBaseUrl').value = 'https://api.openai.com/v1';
        document.getElementById('embTimeout').value = 15;
      } else if(val === 'ollama_nomic') {
        document.getElementById('embModel').value = 'nomic-embed-text';
        document.getElementById('embBaseUrl').value = 'http://127.0.0.1:11434';
        document.getElementById('embApiKey').value = '';
        document.getElementById('embTimeout').value = 30;
      }
    }

    async function runEmbeddingTest() {
      const out = document.getElementById('embTestResult');
      out.innerText = '正在向向量模型发送嵌入测试请求...';
      try {
        const payload = {
          model: document.getElementById('embModel').value.trim(),
          base_url: document.getElementById('embBaseUrl').value.trim(),
          api_key: document.getElementById('embApiKey').value.trim(),
          text: '测试记忆向量索引与切片召回'
        };
        const res = await fetchJSON('/api/embedding/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if(res.ok) {
          out.innerHTML = `<span style="color:var(--success);">[连通正常] 成功生成 ${res.dim} 维向量，耗时 ${res.latency_ms}ms (模型: ${res.model})</span>`;
        } else {
          out.innerHTML = `<span style="color:var(--danger);">[测试失败] ${res.error}</span>`;
        }
      } catch(e) {
        out.innerHTML = `<span style="color:var(--danger);">[网络异常] ${e}</span>`;
      }
    }

    async function savePersonaConfig() {
      const payload = {
        agent_id: document.getElementById('personaAgentId').value.trim(),
        system_prompt: document.getElementById('personaSystemPrompt').value.trim(),
        wake_words: document.getElementById('personaWakeWords').value.split(',').map(s=>s.trim()).filter(Boolean),
        owner_names: document.getElementById('personaOwnerNames').value.split(',').map(s=>s.trim()).filter(Boolean),
      };
      const r = await fetchJSON('/api/config/persona', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      showToast(r.ok ? '人设配置已保存！' : r.error, !r.ok);
    }

    async function saveQQSettings() {
      const payload = {
        ws_url: document.getElementById('qqWsUrl').value.trim(),
        access_token: document.getElementById('qqToken').value.trim(),
        groups: document.getElementById('qqGroups').value.split(',').map(s=>s.trim()).filter(Boolean),
        privates: document.getElementById('qqPrivates').value.split(',').map(s=>s.trim()).filter(Boolean),
        super_users: document.getElementById('qqSuperUsers').value.split(',').map(s=>s.trim()).filter(Boolean),
        owner_qqs: document.getElementById('qqOwners').value.split(',').map(s=>s.trim()).filter(Boolean),
        max_reply_chars: parseInt(document.getElementById('qqMaxChars').value || 120),
        batch_ms: parseFloat(document.getElementById('qqBatchMs').value || 3000),
      };
      const r = await fetchJSON('/api/config/qq', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      showToast(r.ok ? 'QQ 业务参数已保存！' : r.error, !r.ok);
    }

    function selectPersonaPreset(type) {
      document.querySelectorAll('.preset-card').forEach(c => c.classList.remove('selected'));
      const el = document.getElementById('preset-' + type);
      if(el) el.classList.add('selected');
      if (type === 'keks') {
        document.getElementById('personaAgentId').value = 'keks';
        document.getElementById('personaWakeWords').value = '鱼鱼, 小金鱼';
        document.getElementById('personaOwnerNames').value = '鱼片, 主人';
        document.getElementById('personaSystemPrompt').value = '';
      } else if (type === 'jarvis') {
        document.getElementById('personaAgentId').value = 'jarvis';
        document.getElementById('personaWakeWords').value = '贾维斯';
        document.getElementById('personaOwnerNames').value = '先生, 主人';
        document.getElementById('personaSystemPrompt').value = '你是贾维斯，专业高效的高级AI管家，称呼用户为先生。回复简洁严谨。';
      }
    }

    function applyModelPreset(type) {
      if(type === 'ollama_qwen') {
        document.getElementById('llmProvider').value = 'ollama';
        document.getElementById('llmBaseUrl').value = 'http://127.0.0.1:11434';
        document.getElementById('llmModel').value = 'qwen2.5:3b';
        document.getElementById('llmApiKey').value = '';
      } else if(type === 'silicon_deepseek') {
        document.getElementById('llmProvider').value = 'openai';
        document.getElementById('llmBaseUrl').value = 'https://api.siliconflow.cn/v1';
        document.getElementById('llmModel').value = 'deepseek-ai/DeepSeek-V3';
      } else if(type === 'deepseek_official') {
        document.getElementById('llmProvider').value = 'openai';
        document.getElementById('llmBaseUrl').value = 'https://api.deepseek.com/v1';
        document.getElementById('llmModel').value = 'deepseek-chat';
      }
    }

    async function runLLMTest(isQuick) {
      const out = document.getElementById('quickTestResult');
      out.innerText = '正在向大模型发送对话请求...';
      try {
        const msg = isQuick ? document.getElementById('quickTestMsg').value : '你好，请用一句话自我介绍。';
        const res = await fetchJSON('/api/llm/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt: msg })
        });
        if(res.ok) {
          out.innerHTML = `<span style="color:var(--success);">[${res.latency_ms}ms 成功]</span> ${res.reply}`;
        } else {
          out.innerHTML = `<span style="color:var(--danger);">[调用失败]</span> ${res.error}`;
        }
      } catch(e) {
        out.innerHTML = `<span style="color:var(--danger);">[网络异常]</span> ${e}`;
      }
    }

    async function loadBindings() {
      try {
        const res = await fetchJSON('/api/qq/bindings');
        const tb = document.getElementById('bindingsTableBody');
        const list = res.bindings || {};
        const keys = Object.keys(list);
        if(!keys.length) {
          tb.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">暂无手动绑定记录（默认独立身份）</td></tr>';
          return;
        }
        tb.innerHTML = keys.map(k => `
          <tr>
            <td><strong>${k}</strong></td>
            <td><code style="color:var(--primary);">${list[k].anchor}</code></td>
            <td>${list[k].matched_by || 'manual'}</td>
            <td><button class="btn btn-sm btn-danger" onclick="unbindQQ('${k}')">解绑</button></td>
          </tr>
        `).join('');
      } catch(e) {}
    }

    async function submitBind() {
      const q = document.getElementById('bindQqId').value.trim();
      const a = document.getElementById('bindAnchor').value.trim();
      if(!q) return showToast('请输入 QQ 号', true);
      const r = await fetchJSON('/api/qq/bind', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ qq_id: q, anchor: a })
      });
      showToast(r.message || (r.ok ? '成功' : '失败'), !r.ok);
      loadBindings();
    }

    async function unbindQQ(qqId) {
      if(!confirm('确定要解绑 QQ ' + qqId + ' 吗？解绑后将恢复为独立身份 qq_' + qqId)) return;
      const r = await fetchJSON('/api/qq/bind', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ qq_id: qqId, anchor: 'unbind' })
      });
      showToast(r.message, !r.ok);
      loadBindings();
    }

    window.onload = loadAll;
    setInterval(refreshStatus, 8000);
  </script>
</body>
</html>
"""


class AdminRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 简化访问日志
        pass

    def _send_json(self, data: dict, status: int = HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_text: str):
        body = html_text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send_html(HTML_TEMPLATE)
            return

        if path == "/api/status":
            svc = get_service()
            stat_text = svc.status_text()
            stats = getattr(svc, "_stats", {})
            raw_cfg = _read_config_raw()
            brain_sec = _active_brain_section(raw_cfg)
            self._send_json({
                "ok": True,
                "running": getattr(svc, "_state", "stopped") == "running",
                "status_text": stat_text,
                "stats": stats,
                "model": brain_sec.get("model", ""),
                "provider": brain_sec.get("provider", "ollama"),
            })
            return

        if path == "/api/config":
            raw_cfg = _read_config_raw()
            active_brain = dict(_active_brain_section(raw_cfg))
            raw_brain = raw_cfg.get("brain", {})
            raw_qq = raw_cfg.get("qq", {})
            if not isinstance(raw_qq, dict):
                raw_qq = {}
            raw_emb = raw_cfg.get("embedding", {})
            if not isinstance(raw_emb, dict):
                raw_emb = {}
            raw_mem = raw_cfg.get("memory", {})
            if not isinstance(raw_mem, dict):
                raw_mem = {}
            raw_vec = raw_mem.get("vectors") if isinstance(raw_mem.get("vectors"), dict) else (
                raw_qq.get("vectors") if isinstance(raw_qq.get("vectors"), dict) else {}
            )

            active_id = ""
            profiles_list = []
            if isinstance(raw_brain, dict):
                active_id = raw_brain.get("active", "")
                profiles = raw_brain.get("profiles", {})
                if isinstance(profiles, dict):
                    for pid, pdata in profiles.items():
                        if isinstance(pdata, dict):
                            profiles_list.append({
                                "id": pid,
                                "name": pdata.get("name", pid),
                                "provider": pdata.get("provider", "openai"),
                                "model": pdata.get("model", ""),
                                "base_url": pdata.get("base_url", ""),
                                "api_key": pdata.get("api_key", ""),
                                "timeout_sec": pdata.get("timeout_sec", 60),
                            })
            active_brain["active"] = active_id

            vision_data = {
                "enabled": bool(raw_qq.get("vision_enabled", True)),
                "model": str(raw_qq.get("vision_model") or "Qwen/Qwen3-VL-30B-A3B-Instruct"),
                "base_url": str(raw_qq.get("vision_base_url") or ""),
                "api_key": str(raw_qq.get("vision_api_key") or ""),
                "timeout_sec": int(raw_qq.get("vision_timeout_sec") or 20),
                "max_images": int(raw_qq.get("vision_max_images") or 2),
                "max_bytes": int(raw_qq.get("vision_max_bytes") or 800000),
                "max_edge": int(raw_qq.get("vision_max_edge") or 1024),
            }

            embedding_data = {
                "enabled": bool(raw_emb.get("enabled", True)),
                "model": str(raw_emb.get("model") or "BAAI/bge-m3"),
                "base_url": str(raw_emb.get("base_url") or "https://api.siliconflow.cn/v1"),
                "api_key": str(raw_emb.get("api_key") or ""),
                "timeout_sec": int(raw_emb.get("timeout_sec") or 10),
            }

            vectors_data = {
                "chunk_chars": int(raw_vec.get("chunk_chars") or 300),
                "chunk_overlap": int(raw_vec.get("chunk_overlap") or 60),
                "top_k": int(raw_vec.get("top_k") or 3),
                "min_score": float(raw_vec.get("min_score") if raw_vec.get("min_score") is not None else 0.45),
            }

            self._send_json({
                "ok": True,
                "brain": active_brain,
                "active_profile": active_id,
                "profiles": profiles_list,
                "vision": vision_data,
                "embedding": embedding_data,
                "vectors": vectors_data,
                "qq": raw_qq,
            })
            return

        if path == "/api/brain-profiles":
            raw_cfg = _read_config_raw()
            raw_brain = raw_cfg.get("brain", {})
            active_id = raw_brain.get("active", "") if isinstance(raw_brain, dict) else ""
            profiles = raw_brain.get("profiles", {}) if isinstance(raw_brain, dict) else {}
            profiles_list = []
            if isinstance(profiles, dict):
                for pid, pdata in profiles.items():
                    if isinstance(pdata, dict):
                        profiles_list.append({
                            "id": pid,
                            "name": pdata.get("name", pid),
                            "provider": pdata.get("provider", "openai"),
                            "model": pdata.get("model", ""),
                            "base_url": pdata.get("base_url", ""),
                            "api_key": pdata.get("api_key", ""),
                            "timeout_sec": pdata.get("timeout_sec", 60),
                        })
            self._send_json({
                "ok": True,
                "active": active_id,
                "profiles": profiles_list,
            })
            return

        if path == "/api/qq/bindings":
            ident = IdentityMap(data_dir=PROJECT_ROOT / "skills" / "im" / "qq" / "data")
            self._send_json({
                "ok": True,
                "bindings": ident.bindings(),
            })
            return

        self._send_json({"ok": False, "error": "Not Found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get("Content-Length", 0))
        body = {}
        if length > 0:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                body = {}

        if path in ("/api/brain-profiles/switch", "/api/brain/switch"):
            profile_id = str(body.get("active") or body.get("profile_id") or "").strip()
            if not profile_id:
                self._send_json({"ok": False, "error": "缺少目标配置 profile_id"}, status=HTTPStatus.BAD_REQUEST)
                return
            raw_cfg = _read_config_raw()
            raw_brain = raw_cfg.setdefault("brain", {})
            profiles = raw_brain.get("profiles", {})
            if profile_id not in profiles or not isinstance(profiles.get(profile_id), dict):
                self._send_json({"ok": False, "error": f"配置方案不存在: {profile_id}"}, status=HTTPStatus.NOT_FOUND)
                return
            target_profile = profiles[profile_id]
            raw_brain["active"] = profile_id
            for k in ("provider", "base_url", "model", "api_key", "timeout_sec"):
                if k in target_profile:
                    raw_brain[k] = target_profile[k]
            ok = _save_config_raw(raw_cfg)
            try:
                brain = qq_brain.get_brain()
                if hasattr(brain, "reload_if_changed"):
                    brain.reload_if_changed()
            except Exception:
                pass
            self._send_json({
                "ok": ok,
                "active": profile_id,
                "name": target_profile.get("name", profile_id),
                "model": target_profile.get("model", ""),
                "provider": target_profile.get("provider", "openai"),
            })
            return

        if path == "/api/config/brain":
            raw_cfg = _read_config_raw()
            raw_cfg.setdefault("brain", {})
            for k in ("provider", "base_url", "model", "api_key", "timeout_sec"):
                if k in body:
                    raw_cfg["brain"][k] = body[k]
            active = raw_cfg["brain"].get("active")
            profiles = raw_cfg["brain"].get("profiles")
            if isinstance(profiles, dict) and active and active in profiles and isinstance(profiles[active], dict):
                for k in ("provider", "base_url", "model", "api_key", "timeout_sec"):
                    if k in body:
                        profiles[active][k] = body[k]
            ok = _save_config_raw(raw_cfg)
            # 热重载 SimpleBrain
            try:
                brain = qq_brain.get_brain()
                if hasattr(brain, "reload_if_changed"):
                    brain.reload_if_changed()
            except Exception:
                pass
            self._send_json({"ok": ok})
            return

        if path == "/api/config/vision":
            raw_cfg = _read_config_raw()
            qq_sec = raw_cfg.setdefault("qq", {})
            for k in ("vision_enabled", "vision_model", "vision_base_url", "vision_api_key",
                      "vision_timeout_sec", "vision_max_images", "vision_max_bytes", "vision_max_edge"):
                if k in body:
                    qq_sec[k] = body[k]
            ok = _save_config_raw(raw_cfg)
            self._send_json({"ok": ok})
            return

        if path == "/api/config/embedding":
            raw_cfg = _read_config_raw()
            emb_sec = raw_cfg.setdefault("embedding", {})
            for k in ("enabled", "model", "base_url", "api_key", "timeout_sec"):
                if k in body:
                    emb_sec[k] = body[k]
            qq_sec = raw_cfg.setdefault("qq", {})
            qq_emb = qq_sec.setdefault("embedding", {})
            for k in ("enabled", "model", "base_url", "api_key", "timeout_sec"):
                if k in body:
                    qq_emb[k] = body[k]
            ok = _save_config_raw(raw_cfg)
            self._send_json({"ok": ok})
            return

        if path == "/api/config/vectors":
            raw_cfg = _read_config_raw()
            mem_sec = raw_cfg.setdefault("memory", {})
            vec_sec = mem_sec.setdefault("vectors", {})
            for k in ("chunk_chars", "chunk_overlap", "top_k", "min_score"):
                if k in body:
                    vec_sec[k] = body[k]
            qq_sec = raw_cfg.setdefault("qq", {})
            qq_vec = qq_sec.setdefault("vectors", {})
            for k in ("chunk_chars", "chunk_overlap", "top_k", "min_score"):
                if k in body:
                    qq_vec[k] = body[k]
            ok = _save_config_raw(raw_cfg)
            try:
                svc = get_service()
                if hasattr(svc, "_memory") and svc._memory:
                    if "chunk_chars" in body:
                        svc._memory.chunk_target_chars = int(body["chunk_chars"])
                    if "chunk_overlap" in body:
                        svc._memory.chunk_overlap_chars = int(body["chunk_overlap"])
            except Exception:
                pass
            self._send_json({"ok": ok})
            return

        if path == "/api/embedding/test":
            raw_cfg = _read_config_raw()
            emb_cfg = raw_cfg.get("embedding", {})
            model = str(body.get("model") or emb_cfg.get("model") or "BAAI/bge-m3").strip()
            base_url = str(body.get("base_url") or emb_cfg.get("base_url") or "https://api.siliconflow.cn/v1").strip().rstrip("/")
            api_key = str(body.get("api_key") or emb_cfg.get("api_key") or "").strip()
            text = str(body.get("text") or "测试向量检索").strip()
            url = base_url if base_url.endswith("/embeddings") else f"{base_url}/embeddings"

            t0 = time.time()
            try:
                payload = json.dumps({"model": model, "input": text}).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=12) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                latency = int((time.time() - t0) * 1000)
                vec = resp_data["data"][0]["embedding"]
                self._send_json({"ok": True, "dim": len(vec), "latency_ms": latency, "model": model})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)})
            return

        if path == "/api/config/persona":
            raw_cfg = _read_config_raw()
            raw_cfg.setdefault("qq", {})
            for k in ("agent_id", "system_prompt", "wake_words", "owner_names"):
                if k in body:
                    raw_cfg["qq"][k] = body[k]
            ok = _save_config_raw(raw_cfg)
            self._send_json({"ok": ok})
            return

        if path == "/api/config/qq":
            raw_cfg = _read_config_raw()
            raw_cfg.setdefault("qq", {})
            for k in ("ws_url", "access_token", "groups", "privates", "super_users", "owner_qqs", "max_reply_chars", "batch_ms"):
                if k in body:
                    raw_cfg["qq"][k] = body[k]
            ok = _save_config_raw(raw_cfg)
            self._send_json({"ok": ok})
            return

        if path == "/api/llm/test":
            prompt = str(body.get("prompt") or "你好，用一句话自我介绍。").strip()
            brain = load_simple_brain(PROJECT_ROOT)
            t0 = time.time()
            try:
                res = brain.chat_once([{"role": "user", "content": prompt}])
                latency = int((time.time() - t0) * 1000)
                reply = res.get("content", "") if isinstance(res, dict) else str(res or "")
                self._send_json({"ok": True, "reply": reply, "latency_ms": latency})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)})
            return

        if path == "/api/qq/start":
            ok, msg = start_qq()
            self._send_json({"ok": ok, "message": msg})
            return

        if path == "/api/qq/stop":
            ok, msg = stop_qq()
            self._send_json({"ok": ok, "message": msg})
            return

        if path == "/api/qq/bind":
            qq_id = str(body.get("qq_id") or "").strip()
            anchor = str(body.get("anchor") or "").strip()
            ident = IdentityMap(data_dir=PROJECT_ROOT / "skills" / "im" / "qq" / "data")
            ok, msg = ident.bind(qq_id, anchor)
            self._send_json({"ok": ok, "message": msg})
            return

        self._send_json({"ok": False, "error": "Not Found"}, status=HTTPStatus.NOT_FOUND)


def run_admin_server(port: int = DEFAULT_PORT):
    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, AdminRequestHandler)
    print(f"[Admin] Web 控制后台已启动: http://127.0.0.1:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[Admin] Web 控制后台已停止。")
        httpd.server_close()


def start_admin_server_background(port: int = DEFAULT_PORT) -> threading.Thread:
    th = threading.Thread(
        target=run_admin_server,
        args=(port,),
        name="qq-admin-server",
        daemon=True,
    )
    th.start()
    return th


if __name__ == "__main__":
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    run_admin_server(port)
