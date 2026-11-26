"""Unit tests: upload validation (app.core.validation) — CHAT/SEC upload cases."""
import pytest
from fastapi import HTTPException

from app.core import validation as v

ALLOWED = ["application/pdf", "text/plain", "text/markdown",
           "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
DOCX = ALLOWED[-1]


@pytest.mark.parametrize("name,ext", [
    ("a.PDF", ".pdf"), ("x.docx", ".docx"), ("n.txt", ".txt"), ("r.md", ".md"),
    ("evil.exe", ""), ("noext", ""), ("", ""), (None, ""), ("a.pdf.exe", ""),
])
def test_safe_extension(name, ext):
    assert v.safe_extension(name) == ext


def test_declared_mime_accepted():
    assert v.resolve_upload_mime("a.pdf", "application/pdf", ALLOWED) == "application/pdf"


def test_charset_suffix_and_alias_normalised():
    assert v.resolve_upload_mime("a.txt", "text/plain; charset=utf-8", ALLOWED) == "text/plain"
    assert v.resolve_upload_mime("a.md", "text/x-markdown", ALLOWED) == "text/markdown"


def test_octet_stream_falls_back_to_extension():
    assert v.resolve_upload_mime("notes.md", "application/octet-stream", ALLOWED) == "text/markdown"
    assert v.resolve_upload_mime("notes.md", "", ALLOWED) == "text/markdown"


@pytest.mark.parametrize("name,mime", [
    ("a.exe", "application/x-msdownload"), ("a.exe", "application/octet-stream"),
    ("a.png", "image/png"), ("a.zip", ""),
])
def test_disallowed_type_is_415(name, mime):
    with pytest.raises(HTTPException) as e:
        v.resolve_upload_mime(name, mime, ALLOWED)
    assert e.value.status_code == 415


def test_pdf_magic_bytes():
    v.sniff_mime_or_raise(b"%PDF-1.7 ...", "application/pdf")
    with pytest.raises(HTTPException) as e:
        v.sniff_mime_or_raise(b"MZ\x90\x00 pretend", "application/pdf")
    assert e.value.status_code == 415


def test_docx_magic_bytes():
    v.sniff_mime_or_raise(b"PK\x03\x04rest", DOCX)
    with pytest.raises(HTTPException):
        v.sniff_mime_or_raise(b"%PDF-1.7", DOCX)


def test_txt_has_no_signature_check():
    v.sniff_mime_or_raise(b"anything", "text/plain")


def test_safe_filename_trims_and_returns():
    assert v.safe_filename("  report.pdf  ") == "report.pdf"


@pytest.mark.parametrize("bad", ["", "   ", None, "x" * 501])
def test_safe_filename_rejects_with_422(bad):
    with pytest.raises(HTTPException) as e:
        v.safe_filename(bad)
    assert e.value.status_code == 422


def test_safe_filename_boundary_500_ok():
    assert len(v.safe_filename("x" * 500)) == 500
