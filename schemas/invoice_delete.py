from typing import List

from pydantic import BaseModel, Field


class InvoiceDeleteResponse(BaseModel):
    invoiceNo: str
    documentType: str
    versionNo: int
    deletedGrnNumbers: List[str] = Field(default_factory=list)
    ok: bool = True


class InvoiceDeleteBatchResponse(BaseModel):
    deletedInvoices: int
    deletedGrnNumbers: List[str] = Field(default_factory=list)
    results: List[InvoiceDeleteResponse] = Field(default_factory=list)
    ok: bool = True
