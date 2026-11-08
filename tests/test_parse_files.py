"""合同文件解析测试：txt / docx / 文本型 PDF（PDF 依赖缺失时跳过）。"""

from __future__ import annotations

import io

import pytest

from mla.adapters.files import parse_contract_bytes


def _minimal_pdf(text: str) -> bytes:
    """构造一页只含一行 ASCII 文本的最小 PDF（不依赖 PDF 生成库）。"""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    return bytes(out)


def test_parse_txt_bytes() -> None:
    assert parse_contract_bytes("a.txt", "合同正文".encode()) == "合同正文"


def test_parse_docx_bytes() -> None:
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_paragraph("甲方聘用乙方")
    doc.add_paragraph("月工资 5000 元")
    buf = io.BytesIO()
    doc.save(buf)
    text = parse_contract_bytes("a.docx", buf.getvalue())
    assert "甲方聘用乙方" in text
    assert "月工资 5000 元" in text


def test_parse_pdf_bytes() -> None:
    pytest.importorskip("pypdf")
    payload = _minimal_pdf("Sample contract text for PDF parsing test.")
    text = parse_contract_bytes("sample.pdf", payload)
    assert "Sample contract text" in text and len(text) > 20
