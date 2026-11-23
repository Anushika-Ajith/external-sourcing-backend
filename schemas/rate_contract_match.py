from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class RateContractMatchConfirmRequest(BaseModel):
    vendorId: int = Field(..., ge=1)
    lineIndex: int = Field(..., ge=1)
    selectedVendorItemId: Optional[int] = Field(
        None,
        description="Existing vendor_items.id to update; omit or null to insert as new product.",
    )
    ocrLine: Dict[str, Any] = Field(default_factory=dict)
    createAlias: bool = True
    createUomParameter: bool = Field(
        True,
        description=(
            "When OCR UOM is not in Foundation global_parameter (type=UOM), "
            "create it on confirm (same pattern as product-name aliases)."
        ),
    )
    confirmedUomCode: Optional[str] = Field(
        None,
        max_length=3,
        description="Optional CHAR(3) UOM code to store when creating a new global_parameter UOM.",
    )


class RateContractMatchConfirmResponse(BaseModel):
    status: str
    lineStatus: str
    vendorItemId: int
    wasInserted: bool = False
    fingerprintsSynced: int = 0
    aliasCreated: bool = False
    aliasSkippedDuplicate: bool = False
    alias: Optional[Dict[str, Any]] = None
    uomParameterCreated: bool = False
    uomParameterSkippedDuplicate: bool = False
    uomParameter: Optional[Dict[str, Any]] = None
    ocrUom: Optional[str] = None
    confirmedUomCode: Optional[str] = None
    itemStatus: Optional[str] = None
    catalogProductName: Optional[str] = None
    catalogDescription: Optional[str] = None
    ocrProductName: Optional[str] = None
    lineIndex: Optional[int] = None
