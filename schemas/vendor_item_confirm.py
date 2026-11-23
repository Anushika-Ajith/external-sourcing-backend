from typing import Optional

from pydantic import BaseModel


class VendorItemConfirmResponse(BaseModel):
    status: str
    vendorItemId: int
    itemStatus: str
    alreadyConfirmed: bool = False
    vendorProductCode: Optional[str] = None
    erpCode: Optional[str] = None
    companyCode: Optional[str] = None
