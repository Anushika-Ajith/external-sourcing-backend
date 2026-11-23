"""
PDF inspection and text extraction.

Inspect selectable text and embedded images per page before OCR.
Avoid rasterizing whole PDFs; OCR only pages that need it, preferring
embedded images at native resolution over forced high-DPI renders.
"""

from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

MIN_PAGE_TEXT_CHARS = 15
MIN_DOCUMENT_TEXT_CHARS = 50
MIN_EMBEDDED_IMAGE_PX = 80


@dataclass
class PdfPageInfo:
    index: int
    extractable_text: str
    has_embedded_images: bool
    needs_ocr: bool


@dataclass
class PdfInspection:
    path: str
    pages: List[PdfPageInfo] = field(default_factory=list)

    @property
    def selectable_text(self) -> str:
        return "\n".join(
            page.extractable_text
            for page in self.pages
            if page.extractable_text.strip()
        )

    @property
    def has_sufficient_selectable_text(self) -> bool:
        return len(self.selectable_text.strip()) >= MIN_DOCUMENT_TEXT_CHARS

    @property
    def pages_needing_ocr(self) -> List[PdfPageInfo]:
        return [page for page in self.pages if page.needs_ocr]


@lru_cache(maxsize=1)
def _ocr_settings() -> Tuple[int, int, int]:
    try:
        from config import settings

        return (
            int(settings.ocr_max_workers),
            int(settings.ocr_dpi),
            int(settings.ocr_dpi_fallback),
        )
    except Exception:
        return (4, 200, 300)


def inspect_pdf(path: str) -> PdfInspection:
    """Per-page inspection: extractable text vs embedded images."""
    fitz_inspection = _inspect_pdf_fitz(path)
    if fitz_inspection is not None:
        return fitz_inspection
    return _inspect_pdf_pdfplumber(path)


def _inspect_pdf_fitz(path: str) -> Optional[PdfInspection]:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None

    inspection = PdfInspection(path=path)
    try:
        with fitz.open(path) as doc:
            for index, page in enumerate(doc):
                text = (page.get_text("text") or "").strip()
                has_images = bool(page.get_images(full=True))
                has_text = len(text) >= MIN_PAGE_TEXT_CHARS
                inspection.pages.append(
                    PdfPageInfo(
                        index=index,
                        extractable_text=text if has_text else "",
                        has_embedded_images=has_images,
                        needs_ocr=not has_text,
                    )
                )
    except Exception as exc:
        logger.debug("inspect_pdf fitz failed for %s: %s", path, exc)
        return None

    return inspection


def _inspect_pdf_pdfplumber(path: str) -> PdfInspection:
    inspection = PdfInspection(path=path)
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            for index, page in enumerate(pdf.pages):
                text = (page.extract_text() or "").strip()
                has_images = bool(getattr(page, "images", None))
                has_text = len(text) >= MIN_PAGE_TEXT_CHARS
                inspection.pages.append(
                    PdfPageInfo(
                        index=index,
                        extractable_text=text if has_text else "",
                        has_embedded_images=has_images,
                        needs_ocr=not has_text,
                    )
                )
    except Exception as exc:
        logger.debug("inspect_pdf pdfplumber failed for %s: %s", path, exc)

    return inspection


def extract_selectable_text(path: str) -> str:
    """Extract embedded/selectable PDF text only — no OCR or rasterization."""
    inspection = inspect_pdf(path)
    if inspection.selectable_text.strip():
        return inspection.selectable_text

    collected = ""
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    collected += page_text + "\n"
        if collected.strip():
            return collected
    except Exception as exc:
        logger.debug("pdfplumber text extract failed for %s: %s", path, exc)

    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                collected += page_text + "\n"
    except Exception as exc:
        logger.debug("pypdf text extract failed for %s: %s", path, exc)

    return collected


def ocr_image_file(path: str) -> str:
    """OCR a standalone image at native resolution."""
    from PIL import Image
    import pytesseract

    with Image.open(path) as img:
        return pytesseract.image_to_string(img, lang="eng")


def _ocr_embedded_images(page, doc) -> str:
    from PIL import Image
    import pytesseract

    parts: List[str] = []
    for img in page.get_images(full=True):
        xref = img[0]
        try:
            base = doc.extract_image(xref)
            width = int(base.get("width") or 0)
            height = int(base.get("height") or 0)
            if width < MIN_EMBEDDED_IMAGE_PX or height < MIN_EMBEDDED_IMAGE_PX:
                continue
            pil = Image.open(io.BytesIO(base["image"]))
            text = pytesseract.image_to_string(pil, lang="eng")
            if text.strip():
                parts.append(text.strip())
        except Exception as exc:
            logger.debug("embedded image OCR failed xref=%s: %s", xref, exc)
    return "\n".join(parts)


def _ocr_page_render(page, *, dpi: int) -> str:
    from PIL import Image
    import pytesseract

    try:
        import fitz

        scale = dpi / 72.0
        matrix = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
    except Exception as exc:
        logger.debug("page render failed dpi=%s: %s", dpi, exc)
        return ""

    if pix.width < 10 or pix.height < 10:
        return ""

    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    return pytesseract.image_to_string(img, lang="eng")


def _ocr_single_page_fitz(path: str, page_info: PdfPageInfo) -> str:
    import fitz

    _, primary_dpi, fallback_dpi = _ocr_settings()
    with fitz.open(path) as doc:
        page = doc[page_info.index]
        ocr_text = ""
        if page_info.has_embedded_images:
            ocr_text = _ocr_embedded_images(page, doc)
        if len(ocr_text.strip()) < MIN_PAGE_TEXT_CHARS:
            rendered = _ocr_page_render(page, dpi=primary_dpi)
            if len(rendered.strip()) < MIN_PAGE_TEXT_CHARS and primary_dpi != fallback_dpi:
                rendered = _ocr_page_render(page, dpi=fallback_dpi)
            if rendered.strip():
                ocr_text = (
                    f"{ocr_text}\n{rendered}".strip()
                    if ocr_text.strip()
                    else rendered
                )
        return ocr_text.strip()


def _ocr_single_page_pdf2image(path: str, page_index: int) -> str:
    from pdf2image import convert_from_path
    from PIL import Image
    import pytesseract

    _, primary_dpi, fallback_dpi = _ocr_settings()
    for dpi in (primary_dpi, fallback_dpi):
        try:
            images = convert_from_path(
                path,
                dpi=dpi,
                first_page=page_index + 1,
                last_page=page_index + 1,
            )
        except Exception as exc:
            logger.debug("pdf2image page %s dpi=%s failed: %s", page_index, dpi, exc)
            continue

        for image in images:
            if not isinstance(image, Image.Image):
                continue
            text = pytesseract.image_to_string(image, lang="eng")
            if len(text.strip()) >= MIN_PAGE_TEXT_CHARS:
                return text.strip()
    return ""


def _ocr_pages_parallel(path: str, pages: List[PdfPageInfo]) -> str:
    if not pages:
        return ""

    max_workers, _, _ = _ocr_settings()
    workers = min(max_workers, len(pages))
    ordered: List[Optional[str]] = [None] * len(pages)

    def _run(page_info: PdfPageInfo) -> Tuple[int, str]:
        text = _ocr_single_page_fitz(path, page_info)
        if not text:
            text = _ocr_single_page_pdf2image(path, page_info.index)
        return page_info.index, text

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_run, page_info) for page_info in pages]
        for future in as_completed(futures):
            try:
                index, text = future.result()
                pos = next(i for i, p in enumerate(pages) if p.index == index)
                ordered[pos] = text or ""
            except Exception as exc:
                logger.debug("parallel OCR page failed for %s: %s", path, exc)

    return "\n".join(part for part in ordered if part)


def extract_pdf_with_targeted_ocr(path: str) -> str:
    """
    Extract PDF text: selectable layers first, then OCR only on pages that need it.
    """
    inspection = inspect_pdf(path)
    text_parts: List[str] = []

    if inspection.selectable_text.strip():
        text_parts.append(inspection.selectable_text.strip())

    pages_needing_ocr = inspection.pages_needing_ocr
    if not pages_needing_ocr:
        return "\n".join(text_parts)

    ocr_text = _ocr_pages_parallel(path, pages_needing_ocr)
    if ocr_text.strip():
        text_parts.append(ocr_text.strip())

    return "\n".join(text_parts)
