"""Draft ingestion — extract plain text from a user-uploaded draft file.

Called by the API (/research/draft-extract), the CLI (--draft) and the
Telegram bot (document upload) BEFORE a task is created, so every draft
problem is a fast, synchronous error with an English reason — never a
mid-task failure. Heavy parsers (pypdf, python-docx) import lazily.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_DRAFT_CHARS = 50_000


class DraftError(Exception):
    """User-facing draft problem; str(e) is the English reason."""


def extract_draft_text(filename: str, data: bytes) -> tuple[str, bool]:
    """Return (text, truncated). Raises DraftError with an English reason."""
    if len(data) > MAX_FILE_BYTES:
        raise DraftError("file too large (over 10 MB)")
    suffix = Path(filename).suffix.lower()
    if suffix in (".txt", ".md"):
        text = _decode(data)
    elif suffix == ".pdf":
        text = _from_pdf(data)
    elif suffix == ".docx":
        text = _from_docx(data)
    else:
        shown = suffix or "no extension"
        raise DraftError(f"unsupported draft format ({shown}) — use txt, md, pdf or docx")
    text = text.strip()
    if not text:
        raise DraftError("draft contains no text")
    if len(text) > MAX_DRAFT_CHARS:
        return text[:MAX_DRAFT_CHARS], True
    return text, False


def _decode(data: bytes) -> str:
    # cp1251 is a deliberate second guess for this RU/EN project and almost
    # never raises, so the errors="replace" tier is a last resort — and
    # non-utf8/non-cp1251 input may decode wrong rather than error.
    for enc in ("utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _from_pdf(data: bytes) -> str:
    from pypdf import PdfReader  # lazy

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise DraftError("PDF is password-protected")
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except DraftError:
        raise
    except Exception as e:  # pypdf's error tree + stdlib errors on bad streams
        raise DraftError("could not read the PDF file") from e
    if not text.strip():
        raise DraftError("no extractable text found (scanned document?)")
    return text


def _from_docx(data: bytes) -> str:
    import docx  # lazy

    try:
        document = docx.Document(BytesIO(data))
    except Exception as e:  # python-docx raises assorted types on corrupt input
        raise DraftError("could not read the DOCX file") from e
    return "\n\n".join(p.text for p in document.paragraphs)
