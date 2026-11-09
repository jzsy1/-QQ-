# -*- coding: utf-8 -*-
"""PrintWindow 窗口截图 — 被遮挡窗仍可抓取内容，可选暗色 OCR 增强。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

_PW_CLIENTONLY = 0x1
_PW_RENDERFULLCONTENT = 0x2
_SRCCOPY = 0x00CC0020
_BI_RGB = 0
_DIB_RGB_COLORS = 0


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _find_hwnd(title: str = "", hwnd: int = 0) -> int:
    if hwnd and user32.IsWindow(hwnd):
        return int(hwnd)
    needle = (title or "").strip().lower()
    if not needle:
        return 0
    found = ctypes.c_void_p(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _lp):  # type: ignore[misc]
        if not user32.IsWindowVisible(h):
            return True
        n = user32.GetWindowTextLengthW(h)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        if needle in (buf.value or "").lower():
            found.value = h
            return False
        return True

    user32.EnumWindows(_cb, 0)
    return int(found.value or 0)


def capture_window(
    *,
    title: str = "",
    hwnd: int = 0,
    path: str | None = None,
    client_only: bool = False,
) -> tuple[bool, str, str]:
    """PrintWindow 截取窗口位图。返回 (ok, message, saved_path)。"""
    try:
        from PIL import Image
    except ImportError:
        return False, "缺少 Pillow", ""

    target = _find_hwnd(title=title, hwnd=hwnd)
    if not target:
        return False, f"未找到窗口「{title or hwnd}」", ""

    rect = _RECT()
    if not user32.GetWindowRect(target, ctypes.byref(rect)):
        return False, "无法读取窗口矩形", ""
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width < 2 or height < 2:
        return False, "窗口尺寸过小（可能最小化）", ""

    hwnd_dc = user32.GetWindowDC(target)
    if not hwnd_dc:
        return False, "GetWindowDC 失败", ""
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    old = gdi32.SelectObject(mem_dc, bmp)

    flags = _PW_RENDERFULLCONTENT
    if client_only:
        flags |= _PW_CLIENTONLY
    ok_print = bool(user32.PrintWindow(target, mem_dc, flags))
    if not ok_print:
        # 回退 BitBlt（遮挡时可能黑屏）
        gdi32.BitBlt(mem_dc, 0, 0, width, height, hwnd_dc, 0, 0, _SRCCOPY)

    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = _BI_RGB

    buf_len = width * height * 4
    buf = (ctypes.c_ubyte * buf_len)()
    gdi32.GetDIBits(mem_dc, bmp, 0, height, buf, ctypes.byref(bmi), _DIB_RGB_COLORS)

    gdi32.SelectObject(mem_dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(target, hwnd_dc)

    img = Image.frombuffer("RGBA", (width, height), bytes(buf), "raw", "BGRA", 0, 1)
    img = img.convert("RGB")

    out = path or os.path.join(tempfile.gettempdir(), "kekesi_printwindow.png")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return True, f"已 PrintWindow 截取 {width}x{height} → {out}", out


def enhance_for_dark_ocr(img):
    """暗色主题增强：反色 + 对比度，提高 Windows.OCR 命中。"""
    from PIL import ImageEnhance, ImageOps, ImageStat

    rgb = img.convert("RGB")
    stat = ImageStat.Stat(rgb)
    mean = sum(stat.mean) / 3.0
    # 偏暗才增强
    if mean < 110:
        rgb = ImageOps.invert(rgb)
        rgb = ImageEnhance.Contrast(rgb).enhance(1.6)
        rgb = ImageEnhance.Sharpness(rgb).enhance(1.2)
    return rgb


def ocr_window(
    *,
    title: str = "",
    hwnd: int = 0,
    language: str = "zh-Hans-CN",
    dark_enhance: bool = True,
) -> tuple[bool, str]:
    """对指定窗口 PrintWindow 后 OCR（可暗色增强）。"""
    from .ocr_ops import ocr_screen

    ok, msg, path = capture_window(title=title, hwnd=hwnd)
    if not ok or not path:
        return False, msg

    try:
        from PIL import Image

        img = Image.open(path)
        if dark_enhance:
            img = enhance_for_dark_ocr(img)
            enh = path.replace(".png", "_dark.png")
            img.save(enh, "PNG")
            path = enh
    except Exception as e:
        logger.debug("dark enhance skip: %s", e)

    result = _ocr_image_file(path, language=language)
    if not result[0]:
        return result
    prefix = f"窗口「{title or hex(hwnd)}」OCR"
    return True, f"{prefix}：\n{result[1]}"


def _ocr_image_file(path: str, *, language: str = "zh-Hans-CN") -> tuple[bool, str]:
    """对本地图片文件跑 Windows.OCR（与 ocr_ops 同引擎）。"""
    from .winops import powershell

    safe_path = path.replace("'", "''")
    safe_lang = (language or "zh-Hans-CN").replace("'", "''")
    script = f"""
Add-Type -AssemblyName 'System.Runtime.WindowsRuntime, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089' -ErrorAction SilentlyContinue
[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
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
$file = _Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = _Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = _Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bmp = _Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$stream.Close()
$lang = ([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) | Where-Object {{ $_.LanguageTag -eq $langTag }} | Select-Object -First 1
if (-not $lang) {{ $lang = ([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) | Select-Object -First 1 }}
if (-not $lang) {{ Write-Output 'NO_LANG'; exit 0 }}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
if (-not $engine) {{ Write-Output 'ENGINE_FAIL'; exit 0 }}
$result = _Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
if (-not $result -or -not $result.Lines) {{ Write-Output 'EMPTY'; exit 0 }}
foreach ($line in $result.Lines) {{
  $x = 0; $y = 0; $w = 0; $h = 0; $n = 0
  foreach ($word in $line.Words) {{
    $r = $word.BoundingRect
    if ($n -eq 0) {{ $x = $r.X; $y = $r.Y }}
    $right = $r.X + $r.Width; $bottom = $r.Y + $r.Height
    if ($right -gt ($x + $w)) {{ $w = $right - $x }}
    if ($bottom -gt ($y + $h)) {{ $h = $bottom - $y }}
    $n++
  }}
  Write-Output ("LINE|$x|$y|$w|$h|" + $line.Text)
}}
"""
    try:
        out = (powershell(script, timeout=45) or "").strip()
    except Exception as e:
        return False, f"OCR 失败: {e}"
    if not out or out in ("EMPTY", "NO_LANG", "ENGINE_FAIL"):
        return True, f"未识别到文字（{out or '空'}）"
    lines = []
    for ln in out.splitlines():
        if not ln.startswith("LINE|"):
            continue
        parts = ln.split("|", 5)
        if len(parts) < 6:
            continue
        try:
            x, y, w, h = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
            cx, cy = int(x + w / 2), int(y + h / 2)
        except ValueError:
            cx, cy = 0, 0
        lines.append(f"  [{cx},{cy}] {parts[5]}")
    if not lines:
        return True, "未识别到文字"
    return True, f"识别到 {len(lines)} 行：\n" + "\n".join(lines)
