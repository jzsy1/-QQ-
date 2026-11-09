"""OCR — 使用 Windows 内置 OCR 提取屏幕文字及坐标。

Windows 10/11 自带 OCR 引擎（Windows.Media.Ocr），无需安装 Tesseract。
支持中英文等多种语言（需在系统设置中安装对应语言包）。

核心能力：
- ocr_screen：对整屏或指定区域 OCR，返回文字块及坐标
- ocr_find_text：查找屏幕上的指定文字，返回坐标
- list_ocr_languages：列出可用的 OCR 语言
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from dataclasses import dataclass, field

from .winops import powershell

logger = logging.getLogger(__name__)


@dataclass
class OcrWord:
    """OCR 识别出的单词。"""
    text: str
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[int, int]:
        return int(self.x + self.width / 2), int(self.y + self.height / 2)


@dataclass
class OcrLine:
    """OCR 识别出的行。"""
    text: str
    words: list[OcrWord] = field(default_factory=list)

    @property
    def center(self) -> tuple[int, int]:
        if not self.words:
            return 0, 0
        xs = [w.x + w.width / 2 for w in self.words]
        ys = [w.y + w.height / 2 for w in self.words]
        return int(sum(xs) / len(xs)), int(sum(ys) / len(ys))


@dataclass
class OcrResult:
    ok: bool
    message: str
    text: str = ""
    lines: list[OcrLine] = field(default_factory=list)


def list_ocr_languages() -> tuple[bool, str]:
    """列出系统可用的 OCR 语言。"""
    script = """
[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime] | Out-Null
$langs = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages
if (-not $langs -or $langs.Count -eq 0) {
  Write-Output 'NONE'
} else {
  foreach ($l in $langs) {
    Write-Output ($l.LanguageTag + ' | ' + $l.DisplayName)
  }
}
"""
    try:
        out = powershell(script, timeout=10)
        out = (out or "").strip()
        if out == "NONE" or not out:
            return False, "系统未安装任何 OCR 语言包（可在「设置 → 时间和语言 → 语言」中添加）"
        return True, "可用 OCR 语言：\n" + out
    except Exception as e:
        return False, f"查询 OCR 语言失败: {e}"


def ocr_screen(
    *,
    region: tuple[int, int, int, int] | None = None,
    language: str = "zh-Hans-CN",
) -> OcrResult:
    """对屏幕（或指定区域）进行 OCR。

    Args:
        region: (x, y, width, height) 区域，None 表示整屏
        language: OCR 语言标签（如 zh-Hans-CN、en-US），需已安装
    """
    # 1) 截图到临时文件
    try:
        from PIL import ImageGrab, Image
    except ImportError:
        return OcrResult(False, "缺少 Pillow，无法截图（pip install Pillow）")

    path = os.path.join(tempfile.gettempdir(), "kekesi_ocr.png")
    # 全屏截图(all_screens)的图像 (0,0) 对应虚拟屏原点（多显示器时可为负），
    # OCR 结果需加该偏移才是真实屏幕坐标；区域截图时区域左上角即偏移
    if region:
        offset_x, offset_y = region[0], region[1]
    else:
        try:
            import ctypes

            offset_x = int(ctypes.windll.user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
            offset_y = int(ctypes.windll.user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        except Exception:
            offset_x, offset_y = 0, 0
    try:
        if region:
            x, y, w, h = region
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        else:
            img = ImageGrab.grab(all_screens=True)
        img.convert("RGB").save(path, "PNG")
    except Exception as e:
        return OcrResult(False, f"截图失败: {e}")

    # 2) 用 Windows.Media.Ocr 识别
    # 通过 PowerShell 调用 WinRT API
    safe_path = path.replace("'", "''")
    safe_lang = (language or "zh-Hans-CN").replace("'", "''")

    script = f"""
Add-Type -AssemblyName 'System.Runtime.WindowsRuntime, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089' -ErrorAction SilentlyContinue
[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null

# PowerShell 5.1 下 WinRT IAsyncOperation 不能直接 .AsTask()，需用反射调用扩展方法
$_asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
}}) | Select-Object -First 1
function _Await($op, $resultType) {{
  $m = $_asTaskGeneric.MakeGenericMethod($resultType)
  $t = $m.Invoke($null, @($op))
  $t.Wait(-1) | Out-Null
  $t.Result
}}

$path = '{safe_path}'
$langTag = '{safe_lang}'

# 加载图片（用 _Await 替代 .AsTask().GetAwaiter().GetResult()）
$file = _Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = _Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = _Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bmp = _Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$stream.Close()

# 选择 OCR 语言
$lang = ([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) | Where-Object {{ $_.LanguageTag -eq $langTag }} | Select-Object -First 1
if (-not $lang) {{
  $lang = ([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) | Select-Object -First 1
}}
if (-not $lang) {{
  Write-Output 'NO_LANG'
  exit 0
}}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
if (-not $engine) {{
  Write-Output 'ENGINE_FAIL'
  exit 0
}}

$result = _Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
if (-not $result -or -not $result.Lines) {{
  Write-Output 'EMPTY'
  exit 0
}}

$ox = {offset_x}
$oy = {offset_y}
$imgW = $bmp.PixelWidth
$imgH = $bmp.PixelHeight

foreach ($line in $result.Lines) {{
  $lineText = $line.Text
  # PowerShell 5.1 下 IVectorView 索引/管道访问 ComObject 失败，用 foreach 收集
  $firstWord = $null
  $lastWord = $null
  foreach ($w in $line.Words) {{
    if (-not $firstWord) {{ $firstWord = $w }}
    $lastWord = $w
  }}
  if (-not $firstWord) {{ continue }}
  # Windows OCR 的 BoundingRect 已是像素坐标（相对于截图），无需乘以图片尺寸
  $lx = [int]($firstWord.BoundingRect.X) + $ox
  $ly = [int]($firstWord.BoundingRect.Y) + $oy
  $lw = [int]($lastWord.BoundingRect.X + $lastWord.BoundingRect.Width - $firstWord.BoundingRect.X)
  $lh = [int]($firstWord.BoundingRect.Height)
  Write-Output ("LINE|{{0}}|{{1}}|{{2}}|{{3}}|{{4}}" -f $lineText, $lx, $ly, $lw, $lh)
  foreach ($w in $line.Words) {{
    $wx = [int]($w.BoundingRect.X) + $ox
    $wy = [int]($w.BoundingRect.Y) + $oy
    $ww = [int]($w.BoundingRect.Width)
    $wh = [int]($w.BoundingRect.Height)
    Write-Output ("WORD|{{0}}|{{1}}|{{2}}|{{3}}|{{4}}" -f $w.Text, $wx, $wy, $ww, $wh)
  }}
}}
"""
    try:
        out = powershell(script, timeout=30)
    except Exception as e:
        return OcrResult(False, f"OCR 执行失败: {e}")

    out = (out or "").strip()
    if not out:
        return OcrResult(False, "OCR 无输出")
    if out == "NO_LANG":
        return OcrResult(False, "系统未安装 OCR 语言包")
    if out == "ENGINE_FAIL":
        return OcrResult(False, "OCR 引擎创建失败")
    if out == "EMPTY":
        return OcrResult(True, "未识别到文字", text="", lines=[])

    # 解析输出
    lines: list[OcrLine] = []
    current_line: OcrLine | None = None
    full_text_parts: list[str] = []

    for raw in out.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split("|")
        if len(parts) < 6:
            continue
        kind = parts[0]
        try:
            # 末尾 4 字段恒为坐标；中间 `*text_parts` 可能含 `|`，需完整还原文本
            *text_parts, xs, ys, ws, hs = parts[1:]
            text = "|".join(text_parts)
            x, y, w, h = float(xs), float(ys), float(ws), float(hs)
        except (ValueError, IndexError):
            continue

        if kind == "LINE":
            if current_line:
                lines.append(current_line)
            current_line = OcrLine(text=text)
            full_text_parts.append(text)
        elif kind == "WORD":
            word = OcrWord(text=text, x=x, y=y, width=w, height=h)
            if current_line is None:
                current_line = OcrLine(text=text)
            current_line.words.append(word)

    if current_line:
        lines.append(current_line)

    full_text = "\n".join(full_text_parts)
    return OcrResult(
        ok=True,
        message=f"识别到 {len(lines)} 行文字",
        text=full_text,
        lines=lines,
    )


def ocr_find_text(
    needle: str,
    *,
    region: tuple[int, int, int, int] | None = None,
    language: str = "zh-Hans-CN",
    partial: bool = True,
) -> tuple[bool, str, tuple[int, int] | None]:
    """在屏幕上查找指定文字，返回其中心坐标。

    Args:
        needle: 要查找的文字
        region: 限定搜索区域
        language: OCR 语言
        partial: 是否允许部分匹配

    Returns:
        (ok, message, (x, y)) —— 找到时 (x, y) 为中心坐标，未找到时为 None
    """
    needle = (needle or "").strip()
    if not needle:
        return False, "请提供要查找的文字", None

    result = ocr_screen(region=region, language=language)
    if not result.ok:
        return False, result.message, None

    # OCR 中文识别时字间常带空格（"文 件"），匹配前去掉所有空白
    needle_norm = needle.lower().replace(" ", "").replace("\t", "")
    if not needle_norm:
        return False, "请提供要查找的文字", None

    # 收集全部候选按匹配紧密度评分选最优，防止屏幕其他区域含同名文字时
    # 「第一个命中」误点到无关窗口（如聊天记录/文档里出现同样的词）
    best: tuple[float, int, int, str] | None = None

    def _consider(score: float, cx: int, cy: int, hint: str) -> None:
        nonlocal best
        if best is None or score > best[0] + 1e-9:
            best = (score, cx, cy, hint)

    for line in result.lines:
        line_norm = line.text.lower().replace(" ", "").replace("\t", "")
        if not line_norm:
            continue
        if needle_norm == line_norm:
            _consider(1.0, *line.center, "整行精确")
            continue
        for word in line.words:
            word_norm = word.text.lower().replace(" ", "")
            if not word_norm:
                continue
            if needle_norm == word_norm:
                _consider(0.95, *word.center, "词级精确")
            elif needle_norm in word_norm:
                _consider(0.8 * len(needle_norm) / len(word_norm), *word.center, "包含于词")
            elif partial and word_norm in needle_norm:
                _consider(0.7 * len(word_norm) / len(needle_norm), *word.center, "词被包含")
        if partial and needle_norm in line_norm:
            # 行越长说明目标只是顺带出现，紧密度越低
            _consider(0.5 * len(needle_norm) / len(line_norm), *line.center, "行内包含")

    if best:
        score, cx, cy, hint = best
        return True, f"找到「{needle}」于 ({cx}, {cy})（{hint}）", (cx, cy)
    return False, f"屏幕上未找到「{needle}」", None
