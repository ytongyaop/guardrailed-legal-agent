"""文件适配：合同文件取文（.txt / .docx / 文本型 PDF，不做 OCR）。

界面与脚本都从这里取文，避免"同一能力两处实现、一处是空壳"。
"""

from __future__ import annotations

import io
import re
from pathlib import Path

# 演示 / 评测集的固定文件名：它们不是用户的合同名，展示与存档一律换成“示例合同”。
_SAMPLE_NAME_RE = re.compile(r"^(?:contract_(?:traps|compliant)_\d+|sample_contract)$", re.IGNORECASE)
UNNAMED_CONTRACT = "未命名合同"


def normalize_contract_name(raw: str) -> str:
    """把文件名变成可展示的合同名；只清洗已知评测集名，不动真实文件名。

    输入可能带路径或扩展名，先取 stem 再去首尾空白；空名给“未命名合同”。
    函数必须幂等：清洗过的名字再洗一遍结果不变（历史数据里已经存着清洗值）。
    """
    stem = Path(str(raw or "").replace("\\", "/")).stem.strip()
    if not stem:
        return UNNAMED_CONTRACT
    if _SAMPLE_NAME_RE.match(stem):
        return "示例合同"
    return stem


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
