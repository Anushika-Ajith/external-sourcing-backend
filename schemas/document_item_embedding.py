from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

EMBEDDING_DIM = 1536


class DocumentItemEmbeddingBase(BaseModel):
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    source_document_type: Optional[str] = Field(
        None, max_length=50, description="PO, INVOICE, GRN, etc."
    )
    source_document_no: Optional[str] = Field(None, max_length=50)
    source_line_no: Optional[int] = None
    item_id: Optional[int] = None
    fingerprint: Optional[Dict[str, Any]] = Field(
        None, description="Structured item fingerprint payload"
    )
    embedding_text: Optional[str] = None
    embedding: Optional[List[float]] = Field(
        None, description=f"{EMBEDDING_DIM}-dimension embedding vector"
    )

    @field_validator("embedding")
    @classmethod
    def validate_embedding_dim(cls, value: Optional[List[float]]) -> Optional[List[float]]:
        if value is not None and len(value) != EMBEDDING_DIM:
            raise ValueError(f"embedding must have exactly {EMBEDDING_DIM} dimensions")
        return value


class DocumentItemEmbeddingCreate(DocumentItemEmbeddingBase):
    pass


class DocumentItemEmbeddingUpdate(DocumentItemEmbeddingBase):
    pass


class DocumentItemEmbeddingResponse(DocumentItemEmbeddingBase):
    embedding_id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class DocumentItemEmbeddingSimilaritySearch(BaseModel):
    embedding: List[float] = Field(..., description=f"Query vector ({EMBEDDING_DIM} dims)")
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    source_document_type: Optional[str] = Field(None, max_length=50)
    source_document_no: Optional[str] = Field(None, max_length=50)
    item_id: Optional[int] = None
    limit: int = Field(10, ge=1, le=100)

    @field_validator("embedding")
    @classmethod
    def validate_query_embedding(cls, value: List[float]) -> List[float]:
        if len(value) != EMBEDDING_DIM:
            raise ValueError(f"embedding must have exactly {EMBEDDING_DIM} dimensions")
        return value


class DocumentItemEmbeddingSimilarityResult(DocumentItemEmbeddingResponse):
    distance: float = Field(..., description="Cosine distance (lower is more similar)")
