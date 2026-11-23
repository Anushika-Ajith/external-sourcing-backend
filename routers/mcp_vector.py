from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_vector_db
from models import MCPVector, User
from schemas.mcp_vector import (
    EMBEDDING_DIM,
    MCPVectorCreate,
    MCPVectorResponse,
    MCPVectorSimilarityResult,
    MCPVectorSimilaritySearch,
    MCPVectorUpdate,
    MCPWhatsAppMatch,
    MCPWhatsAppResolveRequest,
    MCPWhatsAppResolveResponse,
)
from services.auth_utils import get_current_user
from services.embedding_service import embed_text
from services.whatsapp_auth import require_whatsapp_api_key

router = APIRouter(
    prefix="/mcp-vectors",
    tags=["mcp-vectors"],
    responses={404: {"description": "Not found"}},
)


def _similarity_score(distance: float) -> float:
    return max(0.0, min(1.0, 1.0 - float(distance)))


def _to_whatsapp_match(row: MCPVector, distance: float) -> MCPWhatsAppMatch:
    return MCPWhatsAppMatch(
        mcp_vector_id=row.mcp_vector_id,
        mcp_id=row.mcp_id,
        mcp_type=row.mcp_type,
        sample_query=row.sample_query,
        description=row.description,
        natural_language_description=row.natural_language_description,
        api_name=row.api_name,
        api_endpoint=row.api_endpoint,
        http_method=row.http_method,
        execution_type=row.execution_type,
        request_schema=row.request_schema,
        required_params=row.required_params,
        response_type=row.response_type,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        distance=float(distance),
        similarity_score=_similarity_score(distance),
    )


def _get_mcp_vector_or_404(vector_db: Session, mcp_vector_id: UUID) -> MCPVector:
    row = (
        vector_db.query(MCPVector)
        .filter(MCPVector.mcp_vector_id == mcp_vector_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP vector not found",
        )
    return row


def _apply_list_filters(
    q,
    *,
    tenant_id: Optional[int],
    company_id: Optional[int],
    mcp_id: Optional[str],
    mcp_type: Optional[str],
    api_name: Optional[str],
    http_method: Optional[str],
    execution_type: Optional[str],
    response_type: Optional[str],
):
    if tenant_id is not None:
        q = q.filter(MCPVector.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(MCPVector.company_id == company_id)
    if mcp_id:
        q = q.filter(MCPVector.mcp_id == mcp_id)
    if mcp_type:
        q = q.filter(MCPVector.mcp_type == mcp_type)
    if api_name:
        q = q.filter(MCPVector.api_name == api_name)
    if http_method:
        q = q.filter(MCPVector.http_method == http_method.upper())
    if execution_type:
        q = q.filter(MCPVector.execution_type == execution_type)
    if response_type:
        q = q.filter(MCPVector.response_type == response_type)
    return q


@router.get("", response_model=List[MCPVectorResponse])
async def list_mcp_vectors(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    mcp_id: Optional[str] = Query(None),
    mcp_type: Optional[str] = Query(None),
    api_name: Optional[str] = Query(None),
    http_method: Optional[str] = Query(None),
    execution_type: Optional[str] = Query(None),
    response_type: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    vector_db: Session = Depends(get_vector_db),
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    q = _apply_list_filters(
        vector_db.query(MCPVector),
        tenant_id=tenant_id,
        company_id=company_id,
        mcp_id=mcp_id,
        mcp_type=mcp_type,
        api_name=api_name,
        http_method=http_method,
        execution_type=execution_type,
        response_type=response_type,
    )
    return (
        q.order_by(MCPVector.created_at.desc(), MCPVector.mcp_vector_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.post(
    "/whatsapp/resolve",
    response_model=MCPWhatsAppResolveResponse,
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def resolve_mcp_for_whatsapp(
    body: MCPWhatsAppResolveRequest,
    vector_db: Session = Depends(get_vector_db),
):
    """
    WhatsApp bot path: embed the user query (768-d) and search mcp_vectors.

    Returns confidence band for the top match:
      - high   → distance <= high_confidence_max_distance
      - medium → distance <= medium_confidence_max_distance
      - low    → a candidate exists but outside medium band
      - none   → no candidates
    """
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="query is required",
        )
    if body.medium_confidence_max_distance < body.high_confidence_max_distance:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="medium_confidence_max_distance must be >= high_confidence_max_distance",
        )

    try:
        embedding = embed_text(query, dimensions=EMBEDDING_DIM)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate embedding: {exc}",
        ) from exc

    distance = MCPVector.embedding.cosine_distance(embedding).label("distance")
    q = vector_db.query(MCPVector, distance).filter(MCPVector.embedding.isnot(None))
    q = _apply_list_filters(
        q,
        tenant_id=body.tenant_id,
        company_id=body.company_id,
        mcp_id=None,
        mcp_type=body.mcp_type,
        api_name=None,
        http_method=None,
        execution_type=None,
        response_type=None,
    )
    rows = q.order_by(distance).limit(body.limit).all()
    candidates = [_to_whatsapp_match(row, dist) for row, dist in rows]

    if not candidates:
        return MCPWhatsAppResolveResponse(
            query=query,
            confidence="none",
            match=None,
            candidates=[],
        )

    top = candidates[0]
    if top.distance <= body.high_confidence_max_distance:
        confidence = "high"
    elif top.distance <= body.medium_confidence_max_distance:
        confidence = "medium"
    else:
        confidence = "low"

    return MCPWhatsAppResolveResponse(
        query=query,
        confidence=confidence,
        match=top,
        candidates=candidates,
    )


@router.get(
    "/whatsapp/by-mcp-id/{mcp_id}",
    response_model=MCPVectorResponse,
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def get_mcp_vector_by_mcp_id_for_whatsapp(
    mcp_id: str,
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    vector_db: Session = Depends(get_vector_db),
):
    """Lookup a single mcp_vectors row by mcp_id for WhatsApp alias execution."""
    q = vector_db.query(MCPVector).filter(MCPVector.mcp_id == mcp_id)
    if tenant_id is not None:
        q = q.filter(MCPVector.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(MCPVector.company_id == company_id)
    row = q.order_by(MCPVector.created_at.desc()).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No mcp_vectors row found for mcp_id={mcp_id}",
        )
    return row


@router.get("/{mcp_vector_id}", response_model=MCPVectorResponse)
async def get_mcp_vector(
    mcp_vector_id: UUID,
    vector_db: Session = Depends(get_vector_db),
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    return _get_mcp_vector_or_404(vector_db, mcp_vector_id)


def _resolve_embedding(
    *,
    sample_query: Optional[str],
    embedding: Optional[list],
    reembed_from_query: bool = False,
) -> list:
    """Use provided embedding, or generate one from sample_query."""
    if embedding is not None and not reembed_from_query:
        return list(embedding)
    query = (sample_query or "").strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sample_query is required to generate embedding",
        )
    try:
        return embed_text(query, dimensions=EMBEDDING_DIM)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate embedding: {exc}",
        ) from exc


@router.post("", response_model=MCPVectorResponse, status_code=status.HTTP_201_CREATED)
async def create_mcp_vector(
    body: MCPVectorCreate,
    vector_db: Session = Depends(get_vector_db),
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    payload = body.model_dump()
    payload["embedding"] = _resolve_embedding(
        sample_query=payload.get("sample_query"),
        embedding=payload.get("embedding"),
    )
    row = MCPVector(**payload)
    try:
        vector_db.add(row)
        vector_db.commit()
    except IntegrityError as exc:
        vector_db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not create MCP vector: {exc.orig}",
        )
    vector_db.refresh(row)
    return row


@router.put("/{mcp_vector_id}", response_model=MCPVectorResponse)
async def update_mcp_vector(
    mcp_vector_id: UUID,
    body: MCPVectorUpdate,
    vector_db: Session = Depends(get_vector_db),
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    row = _get_mcp_vector_or_404(vector_db, mcp_vector_id)
    updates = body.model_dump(exclude_unset=True)

    # Re-embed when sample_query changes and caller did not send a new embedding
    if "sample_query" in updates and "embedding" not in updates:
        updates["embedding"] = _resolve_embedding(
            sample_query=updates.get("sample_query") or row.sample_query,
            embedding=None,
            reembed_from_query=True,
        )
    elif "embedding" in updates and updates["embedding"] is None:
        updates["embedding"] = _resolve_embedding(
            sample_query=updates.get("sample_query") or row.sample_query,
            embedding=None,
            reembed_from_query=True,
        )

    for key, value in updates.items():
        setattr(row, key, value)

    try:
        vector_db.commit()
    except IntegrityError as exc:
        vector_db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not update MCP vector: {exc.orig}",
        )
    vector_db.refresh(row)
    return row


@router.delete("/{mcp_vector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_vector(
    mcp_vector_id: UUID,
    vector_db: Session = Depends(get_vector_db),
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    row = _get_mcp_vector_or_404(vector_db, mcp_vector_id)
    vector_db.delete(row)
    vector_db.commit()


@router.post("/search/similar", response_model=List[MCPVectorSimilarityResult])
async def search_similar_mcp_vectors(
    body: MCPVectorSimilaritySearch,
    vector_db: Session = Depends(get_vector_db),
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    distance = MCPVector.embedding.cosine_distance(body.embedding).label("distance")
    q = vector_db.query(MCPVector, distance).filter(MCPVector.embedding.isnot(None))
    q = _apply_list_filters(
        q,
        tenant_id=body.tenant_id,
        company_id=body.company_id,
        mcp_id=body.mcp_id,
        mcp_type=body.mcp_type,
        api_name=body.api_name,
        http_method=body.http_method,
        execution_type=body.execution_type,
        response_type=body.response_type,
    )
    rows = q.order_by(distance).limit(body.limit).all()

    return [
        MCPVectorSimilarityResult(
            mcp_vector_id=row.mcp_vector_id,
            mcp_id=row.mcp_id,
            mcp_type=row.mcp_type,
            sample_query=row.sample_query,
            description=row.description,
            natural_language_description=row.natural_language_description,
            embedding=list(row.embedding) if row.embedding is not None else [],
            tenant_id=row.tenant_id,
            company_id=row.company_id,
            api_name=row.api_name,
            api_endpoint=row.api_endpoint,
            http_method=row.http_method,
            execution_type=row.execution_type,
            request_schema=row.request_schema,
            required_params=row.required_params,
            response_type=row.response_type,
            created_at=row.created_at,
            distance=float(dist),
        )
        for row, dist in rows
    ]
