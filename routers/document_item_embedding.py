from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import DocumentItemEmbedding, User
from schemas.document_item_embedding import (
    DocumentItemEmbeddingCreate,
    DocumentItemEmbeddingResponse,
    DocumentItemEmbeddingSimilarityResult,
    DocumentItemEmbeddingSimilaritySearch,
    DocumentItemEmbeddingUpdate,
)
from services.auth_utils import get_current_user
from services.company_scope import resolve_company_id

router = APIRouter(
    prefix="/document-item-embeddings",
    tags=["document-item-embeddings"],
    responses={404: {"description": "Not found"}},
)


def _get_embedding_or_404(
    db: Session,
    embedding_id: int,
) -> DocumentItemEmbedding:
    row = (
        db.query(DocumentItemEmbedding)
        .filter(DocumentItemEmbedding.embedding_id == embedding_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document item embedding not found",
        )
    return row


def _apply_list_filters(
    q,
    *,
    tenant_id: Optional[int],
    company_id: Optional[int],
    source_document_type: Optional[str],
    source_document_no: Optional[str],
    source_line_no: Optional[int],
    item_id: Optional[int],
):
    company_id = resolve_company_id(company_id)
    if tenant_id is not None:
        q = q.filter(DocumentItemEmbedding.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(DocumentItemEmbedding.company_id == company_id)
    if source_document_type:
        q = q.filter(
            DocumentItemEmbedding.source_document_type == source_document_type
        )
    if source_document_no:
        q = q.filter(DocumentItemEmbedding.source_document_no == source_document_no)
    if source_line_no is not None:
        q = q.filter(DocumentItemEmbedding.source_line_no == source_line_no)
    if item_id is not None:
        q = q.filter(DocumentItemEmbedding.item_id == item_id)
    return q


@router.get("", response_model=List[DocumentItemEmbeddingResponse])
async def list_document_item_embeddings(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    source_document_type: Optional[str] = Query(None),
    source_document_no: Optional[str] = Query(None),
    source_line_no: Optional[int] = Query(None),
    item_id: Optional[int] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    q = _apply_list_filters(
        db.query(DocumentItemEmbedding),
        tenant_id=tenant_id,
        company_id=company_id,
        source_document_type=source_document_type,
        source_document_no=source_document_no,
        source_line_no=source_line_no,
        item_id=item_id,
    )
    return (
        q.order_by(DocumentItemEmbedding.embedding_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/{embedding_id}", response_model=DocumentItemEmbeddingResponse)
async def get_document_item_embedding(
    embedding_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _get_embedding_or_404(db, embedding_id)


@router.post(
    "",
    response_model=DocumentItemEmbeddingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_document_item_embedding(
    body: DocumentItemEmbeddingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = body.model_dump()
    data["company_id"] = resolve_company_id(data.get("company_id"))
    row = DocumentItemEmbedding(**data)
    try:
        db.add(row)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not create document item embedding: {exc.orig}",
        )
    db.refresh(row)
    return row


@router.put("/{embedding_id}", response_model=DocumentItemEmbeddingResponse)
async def update_document_item_embedding(
    embedding_id: int,
    body: DocumentItemEmbeddingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_embedding_or_404(db, embedding_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not update document item embedding: {exc.orig}",
        )
    db.refresh(row)
    return row


@router.delete("/{embedding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document_item_embedding(
    embedding_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_embedding_or_404(db, embedding_id)
    db.delete(row)
    db.commit()


@router.post(
    "/search/similar",
    response_model=List[DocumentItemEmbeddingSimilarityResult],
)
async def search_similar_document_item_embeddings(
    body: DocumentItemEmbeddingSimilaritySearch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Find nearest document item embeddings by cosine distance (<=> operator)."""
    distance = DocumentItemEmbedding.embedding.cosine_distance(body.embedding).label(
        "distance"
    )
    q = (
        db.query(DocumentItemEmbedding, distance)
        .filter(DocumentItemEmbedding.embedding.isnot(None))
    )
    q = _apply_list_filters(
        q,
        tenant_id=body.tenant_id,
        company_id=body.company_id,
        source_document_type=body.source_document_type,
        source_document_no=body.source_document_no,
        source_line_no=None,
        item_id=body.item_id,
    )

    rows = q.order_by(distance).limit(body.limit).all()

    return [
        DocumentItemEmbeddingSimilarityResult(
            embedding_id=row.embedding_id,
            tenant_id=row.tenant_id,
            company_id=row.company_id,
            source_document_type=row.source_document_type,
            source_document_no=row.source_document_no,
            source_line_no=row.source_line_no,
            item_id=row.item_id,
            fingerprint=row.fingerprint,
            embedding_text=row.embedding_text,
            embedding=list(row.embedding) if row.embedding is not None else None,
            created_at=row.created_at,
            distance=float(dist),
        )
        for row, dist in rows
    ]
