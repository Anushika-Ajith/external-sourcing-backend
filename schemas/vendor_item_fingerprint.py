from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

EMBEDDING_DIM = 1536


class VendorItemFingerprintBase(BaseModel):
    fingerprint_json: Optional[Dict[str, Any]] = Field(
        None, description="Structured product fingerprint payload"
    )
    embedding: Optional[List[float]] = Field(
        None, description=f"{EMBEDDING_DIM}-dimension embedding vector"
    )
    embedding_model: Optional[str] = Field(None, max_length=100)

    @field_validator("embedding")
    @classmethod
    def validate_embedding_dim(cls, value: Optional[List[float]]) -> Optional[List[float]]:
        if value is not None and len(value) != EMBEDDING_DIM:
            raise ValueError(f"embedding must have exactly {EMBEDDING_DIM} dimensions")
        return value


class VendorItemFingerprintCreate(VendorItemFingerprintBase):
    vendor_item_id: int = Field(..., description="vendor_items.id")


class VendorItemFingerprintUpdate(VendorItemFingerprintBase):
    vendor_item_id: Optional[int] = None


class VendorItemFingerprintResponse(VendorItemFingerprintBase):
    id: int
    vendor_item_id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class VendorItemFingerprintSimilaritySearch(BaseModel):
    embedding: List[float] = Field(..., description=f"Query vector ({EMBEDDING_DIM} dims)")
    vendor_item_id: Optional[int] = Field(
        None, description="Restrict search to fingerprints for one vendor item"
    )
    embedding_model: Optional[str] = Field(None, max_length=100)
    limit: int = Field(10, ge=1, le=100)

    @field_validator("embedding")
    @classmethod
    def validate_query_embedding(cls, value: List[float]) -> List[float]:
        if len(value) != EMBEDDING_DIM:
            raise ValueError(f"embedding must have exactly {EMBEDDING_DIM} dimensions")
        return value


class VendorItemFingerprintSimilarityResult(VendorItemFingerprintResponse):
    distance: float = Field(..., description="Cosine distance (lower is more similar)")
