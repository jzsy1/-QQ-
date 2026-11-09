"""唤醒词录音识别与关键词生成 — 供后台「录音重录唤醒词」使用。

浏览器录音 -> WAV(base64) -> SenseVoice 离线识别 -> pypinyin 声调切分
-> sherpa-onnx KWS 关键词行。模块 fail-soft：模型缺失或依赖未安装时
抛出带说明的异常，由 HTTP 端点转成 JSON 错误返回后台。
"""

from __future__ import annotations

import io
import logging
import re
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np
from pypinyin import Style, lazy_pinyin

logger = logging.getLogger(__name__)

_PKG_ROOT = Path(__file__).resolve().parent.parent
_ASSISTANT_DIR = _PKG_ROOT.parent / "assistant-x-openclaw"
_MODELS_DIR = _ASSISTANT_DIR / "models"

# 声母按长度倒序，保证 zh/ch/sh 先于 z/c/s 匹配
_INITIALS = (
    "zh", "ch", "sh",
    "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h",
    "j", "q", "x", "r", "z", "c", "s", "y", "w",
)
_TONE_TO_PLAIN = str.maketrans(
    "āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ",
    "aaaaeeeeiiiioooouuuuüüüü",
)

_recognizer = None
_recognizer_lock = threading.Lock()
_token_cache: dict[str, set[str]] = {}
_token_lock = threading.Lock()


def _find_model_dir(pattern: str) -> Path | None:
    """在 models/ 下按通配符找最新目录，避免写死模型版本号。"""
    if not _MODELS_DIR.is_dir():
        return None
    dirs = sorted(
        (p for p in _MODELS_DIR.glob(pattern) if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    return dirs[0] if dirs else None


def _load_tokens() -> set[str]:
    """读取 KWS tokens.txt 词表（校验生成的声母/韵母 token 是否存在）。"""
    model_dir = _find_model_dir("sherpa-onnx-kws-*")
    if model_dir is None:
        return set()
    path = model_dir / "tokens.txt"
    key = str(path)
    with _token_lock:
        if key in _token_cache:
            return _token_cache[key]
        tokens: set[str] = set()
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split()
                    if parts:
                        tokens.add(parts[0])
        except OSError as e:
            logger.warning("读取 KWS tokens 失败: %s", e)
        _token_cache[key] = tokens
        return tokens


def _split_syllable(syllable: str) -> list[str]:
    """把带声调拼音切成 sherpa 词表里的 token：声母 + 带调韵母。"""
    s = (syllable or "").strip().lower()
    if not s:
        return []
    for initial in _INITIALS:
        if s.startswith(initial) and len(s) > len(initial):
            return [initial, s[len(initial):]]
    return [s]


def build_keyword_lines(
    text: str,
    *,
    score: float = 3.0,
    threshold: float = 0.02,
) -> dict[str, Any]:
    """把唤醒词文本转成 sherpa KWS 关键词行。

    返回 {text, lines, missing}：missing 非空表示有 token 不在词表里，
    该行无法被唤醒词模型识别，需要换一个唤醒词或换发音。
    """
    cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text or ""))
    cleaned = cleaned.strip()
    if not cleaned:
        raise ValueError("唤醒词不能为空（建议使用中文）")

    syllables = [s for s in lazy_pinyin(cleaned, style=Style.TONE) if s]
    vocab = _load_tokens()
    tokens: list[str] = []
    missing: list[str] = []

    for syllable in syllables:
        parts = _split_syllable(syllable)
        if not parts:
            continue
        for token in parts:
            if vocab and token not in vocab:
                plain = token.translate(_TONE_TO_PLAIN)
                if vocab and plain in vocab and plain != token:
                    token = plain
                else:
                    missing.append(token)
            tokens.append(token)

    if tokens:
        line = f"{' '.join(tokens)} :{score:g} #{threshold:g} @{cleaned}"
        lines = [line]
    else:
        lines = []

    return {
        "text": cleaned,
        "tokens": tokens,
        "lines": lines,
        "content": "\n".join(lines) + ("\n" if lines else ""),
        "missing": missing,
    }


def _decode_wav(raw: bytes) -> tuple[np.ndarray, int]:
    """解码 16-bit PCM WAV（单/双声道均可），并重采样到 16kHz。"""
    try:
        with wave.open(io.BytesIO(raw), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except wave.Error as e:
        raise ValueError(f"无法解析录音（需要 WAV 格式）: {e}") from e

    if not frames:
        raise ValueError("录音为空")

    if width == 2:
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        samples = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"不支持的采样位宽: {width * 8} bit")

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    samples = np.ascontiguousarray(samples, dtype=np.float32)

    if rate != 16000 and samples.size:
        duration = samples.size / float(rate)
        target_len = max(1, int(round(duration * 16000)))
        samples = np.interp(
            np.linspace(0, samples.size - 1, target_len),
            np.arange(samples.size),
            samples,
        ).astype(np.float32)

    return samples, 16000


def _get_recognizer():
    """懒加载 SenseVoice 离线识别器（首次调用约需数秒）。"""
    global _recognizer
    with _recognizer_lock:
        if _recognizer is not None:
            return _recognizer
        model_dir = _find_model_dir("sherpa-onnx-sense-voice-*")
        if model_dir is None:
            raise FileNotFoundError("未找到 SenseVoice 模型目录（models/sherpa-onnx-sense-voice-*）")
        model = model_dir / "model.int8.onnx"
        tokens = model_dir / "tokens.txt"
        if not model.exists() or not tokens.exists():
            raise FileNotFoundError(f"SenseVoice 模型文件不完整: {model_dir}")

        import sherpa_onnx

        _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model),
            tokens=str(tokens),
            num_threads=2,
            use_itn=False,
            provider="cpu",
        )
        logger.info("SenseVoice 识别器已加载: %s", model_dir)
        return _recognizer


def transcribe_wav(wav_bytes: bytes) -> str:
    """把录音 WAV 识别成文字（中文优先，去掉标点与空白）。"""
    if not wav_bytes:
        raise ValueError("录音为空")
    samples, sample_rate = _decode_wav(wav_bytes)
    if samples.size < sample_rate * 0.3:
        raise ValueError("录音太短，请念完整唤醒词（约 1~3 秒）")
    # 唤醒词录制不需要长音频，超过 10 秒截断，避免识别耗时异常
    max_len = sample_rate * 10
    if samples.size > max_len:
        samples = samples[:max_len]

    recognizer = _get_recognizer()
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, samples)
    recognizer.decode_stream(stream)
    result = stream.result
    text = str(getattr(result, "text", "") or "").strip()
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
    if not text:
        raise ValueError("没有识别到语音，请靠近麦克风重试")
    return text
