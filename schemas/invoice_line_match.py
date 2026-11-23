from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class InvoiceLineMatchReviewResponse(BaseModel):
    invoiceNo: str
    versionNo: int
    lines: List[Dict[str, Any]] = Field(default_factory=list)


class InvoiceLineMatchConfirmRequest(BaseModel):
    tenantId: int
    companyId: int
    documentType: str = Field(..., max_length=100)
    invoiceNo: str = Field(..., max_length=30)
    versionNo: int = 1
    lineNo: int = Field(..., ge=1)
    selectedPoLineNo: int = Field(..., ge=1)
    selectedItemId: Optional[int] = None
    selectedProductFingerprint: Optional[Dict[str, Any]] = None
    createAlias: bool = True


class InvoiceLineMatchConfirmResponse(BaseModel):
    status: str
    lineStatus: str
    invoiceLine: Dict[str, Any]
    userCorrected: bool = False
    aliasCreated: Optional[Dict[str, Any]] = None


class InvoiceConfirmedNotifyRequest(BaseModel):
    poNumber: str = Field(..., max_length=30)
    invoiceNo: Optional[str] = Field(None, max_length=30)
    invoiceNos: Optional[List[str]] = None
    lineCount: Optional[int] = Field(None, ge=1)
    to: Optional[str] = Field(None, max_length=20)
    message: Optional[str] = None


class InvoiceConfirmedNotifyResponse(BaseModel):
    ok: bool = True
    sent: bool = False
    reason: Optional[str] = None
