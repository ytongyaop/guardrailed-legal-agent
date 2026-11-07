"""文件适配：.docx / 文本型 PDF 取文（不做 OCR）。"""

from __future__ import annotations


def extract_text(path: str) -> str:
    """返回纯文本；.docx 用 python-docx，PDF 用 pdfplumber。尚未实现。"""
    raise NotImplementedError("尚未实现 docx / pdf 抽取")
