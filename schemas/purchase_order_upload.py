from typing import Literal, Optional

from pydantic import BaseModel, Field

PoUploadAction = Literal[
    "duplicate",
    "created",
    "attachment_updated",
    "revision_created",
]


class PurchaseOrderUploadResponse(BaseModel):
    action: PoUploadAction
    message: str
    po_id: Optional[int] = None
    po_number: Optional[str] = None
    revision_no: Optional[int] = None
    document_hash: Optional[str] = Field(None, max_length=64)
    business_hash: Optional[str] = Field(None, max_length=64)
    duplicate: bool = False
    ocr_skipped: bool = False
