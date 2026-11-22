"""Local document extraction and deterministic text chunking."""

import hashlib
import io
import re
from html.parser import HTMLParser


class ExtractionUnavailable(RuntimeError):
    pass


class _HtmlText(HTMLParser):
    skip = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.skip:
            self.skipping += 1
        if tag in {"p", "h1", "h2", "h3", "li", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.skip and self.skipping:
            self.skipping -= 1
        if tag in {"p", "h1", "h2", "h3", "li", "br"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping and data.strip():
            self.parts.append(data)


def normalize_text(text: str) -> str:
    text = text.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunk_text(text: str, *, size: int = 420, overlap: int = 60) -> list[str]:
    if size < 40 or overlap < 0 or overlap >= size:
        raise ValueError("Chunk size must be at least 40 and overlap must be smaller than size.")
    words = normalize_text(text).split()
    if not words:
        return []
    chunks = []
    step = size - overlap
    for start in range(0, len(words), step):
        excerpt = " ".join(words[start:start + size])
        if excerpt:
            chunks.append(excerpt)
        if start + size >= len(words):
            break
    return chunks


def extract_html(data: bytes) -> list[tuple[int | None, str]]:
    parser = _HtmlText()
    parser.feed(data.decode("utf-8", errors="replace"))
    text = normalize_text("".join(parser.parts))
    return [(None, text)] if text else []


def extract_plain_text(data: bytes) -> list[tuple[int | None, str]]:
    text = normalize_text(data.decode("utf-8", errors="replace"))
    return [(None, text)] if text else []


def extract_pdf(data: bytes, *, max_pages: int = 100) -> list[tuple[int, str]]:
    try:
        import fitz
    except ImportError as exc:
        raise ExtractionUnavailable("PDF extraction requires PyMuPDF. Install backend requirements.") from exc
    try:
        pdf = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError("The uploaded PDF could not be opened.") from exc
    if pdf.page_count > max_pages:
        raise ValueError(f"The PDF exceeds the {max_pages}-page processing limit.")
    pages = []
    for index, page in enumerate(pdf):
        text = normalize_text(page.get_text("text"))
        if text:
            pages.append((index + 1, text))
    return pages


def extract_pdf_with_ocr(data: bytes, *, max_pages: int = 100) -> tuple[list[tuple[int, str]], str]:
    """Extract each PDF page; OCR scanned pages when a local Tesseract binary exists."""
    try:
        import fitz
    except ImportError as exc:
        raise ExtractionUnavailable("PDF extraction requires PyMuPDF. Install backend requirements.") from exc
    try:
        pdf = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError("The uploaded PDF could not be opened.") from exc
    if pdf.page_count > max_pages:
        raise ValueError(f"The PDF exceeds the {max_pages}-page processing limit.")
    pages: list[tuple[int, str]] = []
    ocr_needed = False
    for index, page in enumerate(pdf):
        text = normalize_text(page.get_text("text"))
        if not text:
            ocr_needed = True
            try:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
                image_text = extract_image(pixmap.tobytes("png"))
                text = image_text[0][1] if image_text else ""
            except ExtractionUnavailable:
                return pages, "ocr_unavailable"
        if text:
            pages.append((index + 1, text))
    if ocr_needed and not pages:
        return [], "ocr_empty"
    return pages, "ocr_used" if ocr_needed else "text_extracted"


def extract_image(data: bytes) -> list[tuple[int, str]]:
    try:
        from PIL import Image
        import pytesseract
    except ImportError as exc:
        raise ExtractionUnavailable("Image OCR requires Pillow and pytesseract. Install backend requirements and Tesseract OCR.") from exc
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as exc:
        raise ValueError("The uploaded image could not be opened.") from exc
    try:
        text = normalize_text(pytesseract.image_to_string(image))
    except Exception as exc:
        raise ExtractionUnavailable("Tesseract OCR is unavailable. Install Tesseract and configure its executable path.") from exc
    return [(1, text)] if text else []


def extract_document(data: bytes, mime_type: str) -> list[tuple[int | None, str]]:
    if mime_type == "application/pdf":
        return extract_pdf_with_ocr(data)[0]
    if mime_type == "text/html":
        return extract_html(data)
    if mime_type in {"image/jpeg", "image/png"}:
        return extract_image(data)
    raise ValueError("Unsupported document type.")


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
