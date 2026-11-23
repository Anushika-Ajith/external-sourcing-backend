from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class SalesProductMatchConfirmRequest(BaseModel):
    lineIndex: int = Field(..., ge=1)
    selectedProductId: int = Field(..., ge=1)
    salesLine: Dict[str, Any] = Field(default_factory=dict)
    tenantId: int = Field(1, ge=1)
    companyId: int = Field(3, ge=1)
    createAlias: bool = True


class SalesProductMatchConfirmResponse(BaseModel):
    status: str
    lineStatus: str
    matchColor: str
    salesTransactionId: int
    productId: int
    aliasCreated: bool = False
    aliasSkippedDuplicate: bool = False
    alias: Optional[Dict[str, Any]] = None
    catalogProductName: Optional[str] = None
    ocrProductName: Optional[str] = None
    lineIndex: Optional[int] = None
