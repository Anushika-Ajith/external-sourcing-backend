from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

EMBEDDING_DIM = 768
MCP_TYPES = ("faq", "action", "knowledge")


class MCPVectorBase(BaseModel):
    mcp_id: Optional[str] = Field(None, max_length=100)
    mcp_type: Optional[Literal["faq", "action", "knowledge"]] = None
    sample_query: Optional[str] = None
    description: Optional[str] = None
    natural_language_description: Optional[str] = None
    embedding: Optional[List[float]] = Field(
        None, description=f"{EMBEDDING_DIM}-dimension embedding vector"
    )
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    api_name: Optional[str] = Field(None, max_length=150)
    api_endpoint: Optional[str] = None
    http_method: Optional[str] = Field(None, max_length=10)
    execution_type: Optional[str] = Field(None, max_length=50)
    request_schema: Optional[Dict[str, Any]] = None
    required_params: Optional[Dict[str, Any]] = None
    response_type: Optional[str] = Field(None, max_length=20)

    @field_validator("embedding")
    @classmethod
    def validate_embedding_dim(cls, value: Optional[List[float]]) -> Optional[List[float]]:
        if value is not None and len(value) != EMBEDDING_DIM:
            raise ValueError(f"embedding must have exactly {EMBEDDING_DIM} dimensions")
        return value

    @field_validator("http_method")
    @classmethod
    def normalize_http_method(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return value.upper()


class MCPVectorCreate(MCPVectorBase):
    mcp_id: str = Field(..., max_length=100)
    mcp_type: Literal["faq", "action", "knowledge"]
    sample_query: str
    # Optional: when omitted, create/update endpoints embed from sample_query
    embedding: Optional[List[float]] = Field(
        None, description=f"{EMBEDDING_DIM}-dimension embedding vector (auto-generated if omitted)"
    )
    tenant_id: int
    company_id: int


class MCPVectorUpdate(MCPVectorBase):
    pass


class MCPVectorResponse(MCPVectorBase):
    mcp_vector_id: UUID
    mcp_id: str
    mcp_type: Literal["faq", "action", "knowledge"]
    sample_query: str
    embedding: List[float]
    tenant_id: int
    company_id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class MCPVectorSimilaritySearch(BaseModel):
    embedding: List[float] = Field(..., description=f"Query vector ({EMBEDDING_DIM} dims)")
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    mcp_id: Optional[str] = Field(None, max_length=100)
    mcp_type: Optional[Literal["faq", "action", "knowledge"]] = None
    api_name: Optional[str] = Field(None, max_length=150)
    http_method: Optional[str] = Field(None, max_length=10)
    execution_type: Optional[str] = Field(None, max_length=50)
    response_type: Optional[str] = Field(None, max_length=20)
    limit: int = Field(10, ge=1, le=100)

    @field_validator("embedding")
    @classmethod
    def validate_query_embedding(cls, value: List[float]) -> List[float]:
        if len(value) != EMBEDDING_DIM:
            raise ValueError(f"embedding must have exactly {EMBEDDING_DIM} dimensions")
        return value

    @field_validator("http_method")
    @classmethod
    def normalize_query_http_method(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return value.upper()


class MCPVectorSimilarityResult(MCPVectorResponse):
    distance: float = Field(..., description="Cosine distance (lower is more similar)")


class MCPWhatsAppResolveRequest(BaseModel):
    query: str = Field(..., min_length=1, description="User WhatsApp query text")
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    mcp_type: Optional[Literal["faq", "action", "knowledge"]] = None
    limit: int = Field(5, ge=1, le=20)
    high_confidence_max_distance: float = Field(
        0.40,
        ge=0.0,
        le=2.0,
        description="Cosine distance <= this value is high confidence",
    )
    medium_confidence_max_distance: float = Field(
        0.55,
        ge=0.0,
        le=2.0,
        description="Cosine distance <= this value is medium confidence",
    )


class MCPWhatsAppMatch(BaseModel):
    mcp_vector_id: UUID
    mcp_id: str
    mcp_type: Literal["faq", "action", "knowledge"]
    sample_query: str
    description: Optional[str] = None
    natural_language_description: Optional[str] = None
    api_name: Optional[str] = None
    api_endpoint: Optional[str] = None
    http_method: Optional[str] = None
    execution_type: Optional[str] = None
    request_schema: Optional[Dict[str, Any]] = None
    required_params: Optional[Dict[str, Any]] = None
    response_type: Optional[str] = None
    tenant_id: int
    company_id: int
    distance: float
    similarity_score: float = Field(
        ..., description="1 - cosine distance, clamped to [0, 1]"
    )


class MCPWhatsAppResolveResponse(BaseModel):
    query: str
    confidence: Literal["high", "medium", "low", "none"]
    match: Optional[MCPWhatsAppMatch] = None
    candidates: List[MCPWhatsAppMatch] = Field(default_factory=list)
