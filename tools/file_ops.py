"""文件操作 — 搜索、复制、移动、重命名"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .winops import desktop_dir

# 解析/模糊匹配时剥掉的常见后缀（可多层，如 .txt.docx）
_KNOWN_EXTS = (
    ".txt.docx",
    ".txt.doc",
    ".docx",
    ".doc",
    ".txt",
    ".md",
    ".markdown",
    ".pdf",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
    ".csv",
    ".json",
    ".lnk",
    ".url",
)


def _strip_known_exts(name: str) -> str:
    """反复剥常见后缀，得到核心名（对话记录.txt.docx → 对话记录）。"""
    stem = (name or "").strip()
    changed = True
    while changed and stem:
        changed = False
        lower = stem.lower()
        for ext in _KNOWN_EXTS:
            if lower.endswith(ext):
                stem = stem[: -len(ext)].rstrip(" .")
                changed = True
                break
    return stem.strip() or (name or "").strip()


def _path_basename_query(raw: str) -> str:
    """绝对/相对路径 → 文件名；口语『桌面上的xx』→ xx。"""
    t = (raw or "").strip().strip("「」\"'“”")
    t = t.replace("\\", "/")
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    t = re.sub(r"^(?:桌面|下载|文档)(?:上的|里的|中的)?", "", t).strip()
    return t.strip("。，,.!?；; ")


def _score_name_match(query_name: str, candidate_name: str) -> float:
    """越高越像。0 = 不匹配。"""
    q = (query_name or "").strip()
    c = (candidate_name or "").strip()
    if not q or not c:
        return 0.0
    ql, cl = q.lower(), c.lower()
    if ql == cl:
        return 100.0
    q_core = _strip_known_exts(q).lower()
    c_core = _strip_known_exts(c).lower()
    if q_core and q_core == c_core:
        return 90.0
    if q_core and (cl.startswith(q_core) or c_core.startswith(q_core)):
        return 75.0 - min(len(c_core) - len(q_core), 20) * 0.5
    if q_core and q_core in cl:
        # 「对话记录」∈「对话记录.txt.docx」
        return 70.0 - min(len(cl) - len(q_core), 30) * 0.3
    if ql in cl:
        return 55.0
    if q_core and c_core and (q_core in c_core or c_core in q_core):
        return 50.0
    return 0.0


def fuzzy_find_files(
    keyword: str,
    search_dir: str | None = None,
    *,
    limit: int = 8,
    min_score: float = 50.0,
) -> list[tuple[float, str]]:
    """在目录内按文件名模糊匹配，返回 [(score, abs_path), ...] 高分在前。"""
    base = search_dir or desktop_dir()
    if not os.path.isdir(base):
        return []
    q_name = _path_basename_query(keyword)
    if not q_name:
        return []
    scored: list[tuple[float, str]] = []
    try:
        with os.scandir(base) as it:
            for ent in it:
                try:
                    name = ent.name
                except OSError:
                    continue
                if name.startswith("."):
                    continue
                sc = _score_name_match(q_name, name)
                if sc >= min_score:
                    scored.append((sc, ent.path))
    except OSError:
        return []
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return scored[: max(1, limit)]


def open_path(path: str) -> tuple[bool, str]:
    """按路径用系统默认程序打开文件/文件夹/快捷方式（os.startfile）。"""
    raw = (path or "").strip()
    if not raw:
        return False, "请提供要打开的路径"
    resolved = resolve_user_path(raw) or os.path.expanduser(raw)
    if not os.path.exists(resolved):
        return False, f"路径不存在: {raw}"
    try:
        os.startfile(resolved)  # noqa: S606
    except OSError as e:
        return False, f"打开失败: {e}"
    name = os.path.basename(resolved) or resolved
    kind = "文件夹" if os.path.isdir(resolved) else "文件"
    return True, f"已打开{kind}「{name}」"


def search_files(keyword: str, search_dir: str | None = None) -> tuple[bool, str]:
    base = search_dir or desktop_dir()
    if not os.path.isdir(base):
        base = str(Path.home())

    # 先目录内模糊（后缀猜错也能中）
    fuzzy = fuzzy_find_files(keyword, base, limit=10)
    if fuzzy:
        lines = [p for _sc, p in fuzzy]
        return True, "找到以下文件：\n" + "\n".join(lines)

    # 关键词用核心名，避免「对话记录.docx」like 不到「对话记录.txt.docx」
    core = _strip_known_exts(_path_basename_query(keyword)) or (keyword or "")
    safe_keyword = core.replace("'", "''")
    try:
        ps = (
            f'Get-ChildItem -Path "{base}" -Recurse -ErrorAction SilentlyContinue '
            f'| Where-Object {{ $_.Name -like "*{safe_keyword}*" }} '
            f'| Select-Object -First 10 FullName '
            f'| ForEach-Object {{ $_.FullName }}'
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = [l.strip() for l in (result.stdout or "").splitlines() if l.strip()]
        if not lines:
            return True, f"在 {base} 未找到包含「{keyword}」的文件"
        return True, "找到以下文件：\n" + "\n".join(lines)
    except Exception as e:
        return False, f"搜索失败: {e}"


def _resolve_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser((path or "").strip()))


_DIR_HINTS: dict[str, object] = {}


def _init_dir_hints() -> dict[str, object]:
    if _DIR_HINTS:
        return _DIR_HINTS
    home = Path.home()
    _DIR_HINTS.update(
        {
            "桌面": desktop_dir,
            "下载": lambda: str(home / "Downloads"),
            "文档": lambda: str(home / "Documents"),
        }
    )
    return _DIR_HINTS


def resolve_user_path(target: str) -> str | None:
    """把用户说的文件名/桌面路径解析为真实绝对路径（含后缀模糊）。"""
    raw = (target or "").strip().strip("。，,.!?；;")
    if not raw:
        return None

    direct = _resolve_path(raw)
    if os.path.exists(direct):
        return direct

    hints = _init_dir_hints()
    normalized = raw.replace("\\", "/")
    if "/" in normalized:
        head, rest = normalized.split("/", 1)
        for key, fn in hints.items():
            if head == key or head.startswith(key):
                joined = os.path.join(fn(), rest)
                if os.path.exists(joined):
                    return joined
                hits = fuzzy_find_files(rest, fn())
                if hits:
                    return hits[0][1]

    for key, fn in hints.items():
        if raw.startswith(key):
            rest = raw[len(key) :].lstrip("上的里 ")
            if rest:
                joined = os.path.join(fn(), rest)
                if os.path.exists(joined):
                    return joined
                hits = fuzzy_find_files(rest, fn())
                if hits:
                    return hits[0][1]

    desk = os.path.join(desktop_dir(), raw)
    if os.path.exists(desk):
        return desk

    # 绝对路径不存在：用文件名在桌面/父目录模糊
    base_name = _path_basename_query(raw)
    parent = None
    if os.path.isabs(direct) or (len(raw) > 2 and raw[1] == ":"):
        parent = os.path.dirname(direct)
    search_dirs: list[str] = []
    if parent and os.path.isdir(parent):
        search_dirs.append(parent)
    desk_dir = desktop_dir()
    if desk_dir not in search_dirs:
        search_dirs.append(desk_dir)

    best: tuple[float, str] | None = None
    for d in search_dirs:
        for sc, p in fuzzy_find_files(base_name or raw, d, limit=5):
            if best is None or sc > best[0]:
                best = (sc, p)
    if best and best[0] >= 50.0:
        return best[1]

    ok, msg = search_files(base_name or raw, desktop_dir())
    if ok and "找到以下文件" in msg:
        for line in msg.splitlines()[1:]:
            line = line.strip()
            if line and os.path.exists(line):
                return line

    return None


def copy_path(source: str, destination: str) -> tuple[bool, str]:
    src = _resolve_path(source)
    dst = _resolve_path(destination)
    if not os.path.exists(src):
        return False, f"源路径不存在: {source}"
    try:
        if os.path.isdir(src):
            if os.path.exists(dst):
                if os.path.isdir(dst):
                    dst = os.path.join(dst, os.path.basename(src))
                else:
                    return False, f"目标已存在: {destination}"
            shutil.copytree(src, dst)
        else:
            if os.path.isdir(dst):
                dst = os.path.join(dst, os.path.basename(src))
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copy2(src, dst)
        return True, f"已复制到 {dst}"
    except Exception as e:
        return False, f"复制失败: {e}"


def move_path(source: str, destination: str) -> tuple[bool, str]:
    src = _resolve_path(source)
    dst = _resolve_path(destination)
    if not os.path.exists(src):
        return False, f"源路径不存在: {source}"
    try:
        if os.path.isdir(dst) or dst.endswith((os.sep, "/")):
            dst = os.path.join(dst.rstrip("/\\"), os.path.basename(src))
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.move(src, dst)
        return True, f"已移动到 {dst}"
    except Exception as e:
        return False, f"移动失败: {e}"


def rename_path(path: str, new_name: str) -> tuple[bool, str]:
    src = _resolve_path(path)
    new_name = (new_name or "").strip()
    if not os.path.exists(src):
        return False, f"路径不存在: {path}"
    if not new_name or "/" in new_name or "\\" in new_name:
        return False, "新名称不能包含路径分隔符"
    dst = os.path.join(os.path.dirname(src), new_name)
    if os.path.exists(dst):
        return False, f"目标已存在: {new_name}"
    try:
        os.rename(src, dst)
        return True, f"已重命名为 {new_name}"
    except Exception as e:
        return False, f"重命名失败: {e}"


def _safe_filename(name: str, default: str = "文档") -> str:
    raw = (name or "").strip() or default
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad else c for c in raw).strip(" .")
    return (cleaned or default)[:80]


def _xml_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _write_minimal_docx(path: Path, title: str, body: str) -> None:
    """不依赖 python-docx，写一份可被 Word 打开的最小 OOXML。"""
    import zipfile

    paragraphs = []
    if title.strip():
        paragraphs.append(title.strip())
        paragraphs.append("")
    for line in (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        paragraphs.append(line)

    p_xml = []
    for line in paragraphs:
        if line == "":
            p_xml.append("<w:p/>")
        else:
            p_xml.append(
                "<w:p><w:r><w:t xml:space=\"preserve\">"
                f"{_xml_escape(line)}</w:t></w:r></w:p>"
            )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(p_xml)}<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)


def create_document(
    content: str,
    *,
    title: str = "",
    filename: str = "",
    folder: str = "",
    fmt: str = "docx",
) -> tuple[bool, str]:
    """在桌面（或指定目录）创建文档，写入正文。支持 docx / txt / md。"""
    body = (content or "").strip()
    if not body:
        return False, "文档内容不能为空"
    fmt_l = (fmt or "docx").strip().lower().lstrip(".")
    if fmt_l in ("word", "doc"):
        fmt_l = "docx"
    if fmt_l not in ("docx", "txt", "md", "markdown"):
        return False, f"不支持的格式「{fmt}」，请用 docx / txt / md"
    if fmt_l == "markdown":
        fmt_l = "md"

    base_name = _safe_filename(filename or title or "未命名文档")
    # 避免「对话记录.txt」+ format=docx → 对话记录.txt.docx
    stem = base_name
    for ext in (".docx", ".doc", ".txt", ".md", ".markdown"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)].rstrip(" .") or "未命名文档"
            break
    base_name = f"{stem}.{fmt_l}"

    if folder.strip():
        out_dir = Path(_resolve_path(folder))
    else:
        out_dir = Path(desktop_dir())
    out_path = out_dir / base_name
    # 重名则追加序号
    if out_path.exists():
        stem = out_path.stem
        for i in range(2, 100):
            cand = out_dir / f"{stem}_{i}{out_path.suffix}"
            if not cand.exists():
                out_path = cand
                break

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        if fmt_l == "docx":
            try:
                from docx import Document  # type: ignore

                doc = Document()
                if title.strip():
                    doc.add_heading(title.strip(), level=1)
                for line in body.replace("\r\n", "\n").split("\n"):
                    doc.add_paragraph(line)
                doc.save(str(out_path))
            except ImportError:
                _write_minimal_docx(out_path, title, body)
        else:
            text = f"{title.strip()}\n\n{body}" if title.strip() else body
            out_path.write_text(text, encoding="utf-8")
        return True, f"已创建文档：{out_path}"
    except Exception as e:
        return False, f"创建文档失败: {e}"
