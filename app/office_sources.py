"""Extract clauses with true PDF page / Word body-block locations."""
from io import BytesIO
import re
from zipfile import ZipFile

from docx import Document
from docx.table import Table
from pypdf import PdfReader

MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT = 1_000_000
NUMBERED = re.compile(r"^(?:第\s*([\d一二三四五六七八九十百]+)\s*条\s*|(\d+(?:\.\d+)+|\d+(?=[、)]))[、.)]?\s+)(.{1,100})$")
INLINE_ARTICLE = re.compile(r"^第\s*([\d〇零一二三四五六七八九十百千]+)\s*条\s*(.+[。！？；])$")


def docx_blocks(raw):
    with ZipFile(BytesIO(raw)) as package:
        files = package.infolist()
        if ("word/document.xml" not in package.namelist() or len(files) > 1500
                or sum(f.file_size for f in files) > 48 * 1024 * 1024
                or any(f.filename.lower().endswith("vbaproject.bin") for f in files)):
            raise ValueError("Word 文档不符合导入限制。请另存为普通 DOCX。")
    doc = Document(BytesIO(raw))
    for number, item in enumerate(doc.iter_inner_content(), 1):
        if isinstance(item, Table):
            rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in item.rows]
            yield number, "\n".join(rows), "body"
        else:
            style = item.style.name if item.style else ""
            kind = "title" if style == "Title" else "heading" if style.startswith("Heading ") else "body"
            yield number, item.text, kind


def pdf_blocks(raw):
    if not raw.startswith(b"%PDF-"):
        raise ValueError("文件不是有效的 PDF。")
    pdf = PdfReader(BytesIO(raw), strict=False)
    if pdf.is_encrypted:
        raise ValueError("请上传已获授权并解密的文本 PDF。")
    if not 0 < len(pdf.pages) <= 100:
        raise ValueError("本地版每份 PDF 支持 1 至 100 页。")
    for page_no, page in enumerate(pdf.pages, 1):
        text = page.extract_text() or ""
        if not text.strip():
            raise ValueError(f"第 {page_no} 页没有可提取文字。请先 OCR，或移除空白页后再提交。")
        for line in text.splitlines():
            yield page_no, line, "body"


def parse_office(raw, extension, source, document_id):
    if len(raw) > MAX_BYTES:
        raise ValueError("文件超过 8 MiB。")
    locator = "source_page" if extension == ".pdf" else "source_block"
    factory = pdf_blocks if extension == ".pdf" else docx_blocks
    title = document_id
    chunks, seen = [], {}
    label, heading, body, start, end = "引言", "正文", [], 1, 1
    total = 0

    def flush():
        if not body:
            return
        count = seen.get(label, 0) + 1
        seen[label] = count
        chunks.append({"section": label, "fragment_id": label if count == 1 else f"{label}~{count}",
                       "section_title": heading, "source": source, locator: start,
                       locator + "_end": end, "text": "\n".join(body)})
        body.clear()

    try:
        for pos, value, kind in factory(raw):
            value = value.strip()
            if not value:
                continue
            total += len(value)
            if total > MAX_TEXT:
                raise ValueError("文档提取文本超过 100 万字符，请按文件分册提交。")
            article = INLINE_ARTICLE.fullmatch(value)
            match = NUMBERED.fullmatch(value)
            if kind == "title":
                title = value
                continue
            if (title == document_id and not body and not chunks and pos == 1 and kind == "body"
                    and not match and not article and len(value) < 90 and not value.endswith(("。", "！", "？", "；"))):
                # The PDF has no paragraph styles. Keep its first line in the evidence too.
                title = value
            if article:
                flush()
                label, heading = article.group(1), "条文"
                start = end = pos
                body.append(article.group(2))
            elif match or kind == "heading":
                flush()
                label = (match.group(1) or match.group(2)) if match else f"段落{pos}"
                heading = match.group(3) if match else value
                start = end = pos
            else:
                if label == "引言" and not body:
                    start = pos
                end = pos
                body.append(value)
        flush()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("原文件无法解析，请确认格式并重新导出。") from exc
    if not chunks:
        raise ValueError("未提取到条款正文，请检查文件中的文本。")
    return title, chunks


def source_location(row):
    for key, unit in (("source_page", "页"), ("source_block", "正文块"), ("source_line", "行")):
        if key in row:
            first, last = row[key], row.get(key + "_end", row[key])
            return f"第 {first} {unit}" if first == last else f"第 {first}–{last} {unit}"
    return "原件"


def source_reference(row):
    """Keep filename:line usable for Markdown and show native locations for Office files."""
    if "source_line" in row:
        return f"{row['source']}:{row['source_line']}"
    return f"{row['source']} · {source_location(row)}"
