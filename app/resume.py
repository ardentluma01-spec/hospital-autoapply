"""Stores your resume and pulls the text out of it for the AI."""
from pathlib import Path

from .settings import DATA

RESUME_DIR = DATA / "resume"
RESUME_DIR.mkdir(exist_ok=True)


def save(filename: str, content: bytes) -> Path:
    ext = Path(filename).suffix.lower()
    if ext not in (".pdf", ".docx"):
        raise ValueError("Please upload a PDF or Word (.docx) resume")
    for old in RESUME_DIR.glob("resume.*"):
        old.unlink()
    path = RESUME_DIR / f"resume{ext}"
    path.write_bytes(content)
    return path


def current() -> Path | None:
    files = sorted(RESUME_DIR.glob("resume.*"))
    return files[0] if files else None


def text() -> str:
    path = current()
    if not path:
        return ""
    if path.suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    from docx import Document
    return "\n".join(p.text for p in Document(path).paragraphs)
