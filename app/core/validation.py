"""Small shared input-sanitization helpers used across the ingestion pipeline."""

import os

from fastapi import HTTPException, status

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}

def safe_extension(filename: str) -> str:
    """
    Extract and allow-list-validate a filename's extension before it's used
    to build a storage key (e.g. a Cloudinary public_id). Anything outside
    the known-safe set is dropped rather than trusted verbatim, so a crafted
    filename can't smuggle path-like junk into the storage path.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in ALLOWED_EXTENSIONS else ""

_EXT_TO_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}

# Declared types browsers/OSes actually send that mean the same thing as a
# canonical allowed type.
_MIME_ALIASES = {
    "text/x-markdown": "text/markdown",
    "application/x-pdf": "application/pdf",
}

def resolve_upload_mime(filename: str, declared_mime: str, allowed: list) -> str:
    """
    Turn the client's declared Content-Type into a canonical allowed MIME, or
    raise 415. Handles the real-world mess: charset suffixes
    ("text/plain; charset=utf-8"), alias types (text/x-markdown), and
    browsers that send no useful type at all (empty or
    application/octet-stream — common for .md files), where the allow-listed
    extension decides instead. The magic-byte sniff (sniff_mime_or_raise)
    still runs afterwards on the resolved type, so extension fallback can't
    smuggle a mislabeled PDF/DOCX through.
    """
    mime = (declared_mime or "").split(";")[0].strip().lower()
    mime = _MIME_ALIASES.get(mime, mime)
    if mime in allowed:
        return mime
    if mime in ("", "application/octet-stream"):
        fallback = _EXT_TO_MIME.get(os.path.splitext(filename or "")[1].lower())
        if fallback and fallback in allowed:
            return fallback
    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail=f"File type '{declared_mime}' is not allowed. Allowed: PDF, DOCX, TXT, MD.",
    )

_MAGIC_CHECKS = {
    "application/pdf": lambda content: content[:5] == b"%PDF-",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        lambda content: content[:4] == b"PK\x03\x04"
    ),
}

def sniff_mime_or_raise(content: bytes, declared_mime: str) -> None:
    """
    Reject uploads whose declared Content-Type doesn't match the actual file
    signature, for types where a reliable magic-byte check exists (PDF,
    DOCX). TXT/MD have no reliable signature — the client-supplied type is
    trusted for those, same as before.
    """
    check = _MAGIC_CHECKS.get(declared_mime)
    if check is not None and not check(content):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"File content doesn't match the declared type '{declared_mime}'.",
        )

def safe_filename(name: str, max_len: int = 500) -> str:
    """
    Validate/normalize a filename (or scraped page title) before it's stored.

    Raises a clean 422 instead of letting an over-length or empty value hit
    a DB column width and raise a raw DataError/IntegrityError.
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Name cannot be empty.",
        )
    if len(name) > max_len:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Name is too long (max {max_len} characters).",
        )
    return name
