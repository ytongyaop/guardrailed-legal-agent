"""文件适配：合同文件取文（.txt / .docx / 文本型 PDF，不做 OCR）。

界面与脚本都从这里取文，避免"同一能力两处实现、一处是空壳"。
"""

from __future__ import annotations

import io


def parse_contract_bytes(filename: str, data: bytes) -> str:
    """解析上传的合同文件；PDF 需要 pypdf（未安装时抛 ImportError 由界面提示）。"""
    name = filename.lower()
    if name.endswith(".docx"):
        from docx import Document

        return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    return data.decode("utf-8", errors="ignore")
