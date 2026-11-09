"""收图理解：视觉模型把图片转成一句话描述，再进大脑。

走 OpenAI 兼容 chat/completions，图一律用 data URL（base64）传：
QQ 图床的 rkey 地址远端抓不动也会过期，base64 才稳。
返回 (描述, 错误)，错误由调用方记进状态，不再静默吞掉。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_PROMPT = (
    "用一句话客观描述这张图的内容（不超过40个字）："
    "主体是什么、在做什么、有没有文字。不要评论、不要表情符号。"
)


def describe_image(data_url: str, base_url: str, api_key: str,
                   model: str, timeout: float = 20.0) -> tuple[str, str]:
    """图片（data URL 或可访问的 http 地址）→ 一句话描述。

    失败返回 ("", 原因)。
    """
    if not data_url:
        return "", "没有图片"
    if not (base_url and model):
        return "", "视觉未配置（qq.vision_base_url / qq.vision_model）"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        "max_tokens": 120,
        "stream": False,
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = str(data["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8", "ignore")).get(
                "message") or "")[:120]
        except Exception:
            pass
        err = f"HTTP {exc.code} {detail}".strip()
        logger.warning("视觉接口报错 %s: %s", model, err)
        return "", err
    except Exception as exc:
        err = f"{type(exc).__name__}: {str(exc)[:120]}"
        logger.warning("视觉描述失败 %s: %s", model, err, exc_info=True)
        return "", err
    if not text:
        return "", "模型返回空"
    # 防止思维链模型吐出大段推理：只取最后一行
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return (lines[-1][:80] if lines else ""), ""
