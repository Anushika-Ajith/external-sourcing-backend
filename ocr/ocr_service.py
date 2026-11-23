from typing import List, Union

import os

from ocr.data_cleanup import extract_vendor_data
from services.pdf_inspection import extract_pdf_with_targeted_ocr, ocr_image_file


def extract_combined_text(files: Union[str, List[str]]) -> str:
    """
    OCR / pdf text extraction only — no LLM calls.

    PDFs: inspect selectable text and embedded images per page first.
    Rasterize and OCR only pages that lack extractable text, using embedded
    image resolution when possible instead of upscaling the whole document.
    """
    if isinstance(files, str):
        files = [files]

    extracted_text: List[str] = []

    for path in files:
        if not os.path.exists(path):
            raise FileNotFoundError(path)

        if path.lower().endswith(".pdf"):
            extracted_text.append(extract_pdf_with_targeted_ocr(path))

        elif path.lower().endswith((".png", ".jpg", ".jpeg")):
            extracted_text.append(ocr_image_file(path))

        else:
            raise ValueError(f"Unsupported file type: {path}")

    return "\n".join(extracted_text)


def extract_raw_text(files):
    combined_text = extract_combined_text(files)
    return extract_vendor_data(combined_text)
