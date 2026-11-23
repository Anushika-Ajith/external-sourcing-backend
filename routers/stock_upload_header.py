from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import StockUploadHeader, User
from schemas.stock_upload_header import (
    StockUploadHeaderCreate,
    StockUploadHeaderResponse,
    StockUploadHeaderUpdate,
)
from services.auth_utils import get_current_user
from services.company_scope import resolve_company_id

router = APIRouter(
    prefix="/stock-upload-headers",
    tags=["stock-upload-headers"],
    responses={404: {"description": "Not found"}},
)


def _get_stock_upload_header_or_404(
    db: Session, stock_upload_hdr_id: int
) -> StockUploadHeader:
    row = (
        db.query(StockUploadHeader)
        .filter(StockUploadHeader.stock_upload_hdr_id == stock_upload_hdr_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stock upload header not found",
        )
    return row


def _apply_list_filters(
    q,
    *,
    tenant_id: Optional[int],
    company_id: Optional[int],
    document_no: Optional[str],
    upload_source: Optional[str],
    status_value: Optional[str],
):
    company_id = resolve_company_id(company_id)
    if tenant_id is not None:
        q = q.filter(StockUploadHeader.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(StockUploadHeader.company_id == company_id)
    if document_no:
        q = q.filter(StockUploadHeader.document_no.ilike(f"%{document_no}%"))
    if upload_source:
        q = q.filter(StockUploadHeader.upload_source.ilike(f"%{upload_source}%"))
    if status_value:
        q = q.filter(StockUploadHeader.status == status_value)
    return q


@router.get("", response_model=List[StockUploadHeaderResponse])
async def list_stock_upload_headers(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    document_no: Optional[str] = Query(None),
    upload_source: Optional[str] = Query(None),
    status_value: Optional[str] = Query(None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    q = _apply_list_filters(
        db.query(StockUploadHeader),
        tenant_id=tenant_id,
        company_id=company_id,
        document_no=document_no,
        upload_source=upload_source,
        status_value=status_value,
    )
    return (
        q.order_by(StockUploadHeader.stock_upload_hdr_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get(
    "/{stock_upload_hdr_id}",
    response_model=StockUploadHeaderResponse,
)
async def get_stock_upload_header(
    stock_upload_hdr_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _get_stock_upload_header_or_404(db, stock_upload_hdr_id)


@router.post(
    "",
    response_model=StockUploadHeaderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_stock_upload_header(
    body: StockUploadHeaderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = body.model_dump()
    data["company_id"] = resolve_company_id(data.get("company_id"))
    data["created_by"] = current_user.user_id
    data["updated_by"] = current_user.user_id
    row = StockUploadHeader(**data)
    try:
        db.add(row)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not create stock upload header: {exc.orig}",
        )
    db.refresh(row)
    return row


@router.put(
    "/{stock_upload_hdr_id}",
    response_model=StockUploadHeaderResponse,
)
async def update_stock_upload_header(
    stock_upload_hdr_id: int,
    body: StockUploadHeaderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_stock_upload_header_or_404(db, stock_upload_hdr_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    row.updated_by = current_user.user_id
    row.updated_on = datetime.utcnow()

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not update stock upload header: {exc.orig}",
        )
    db.refresh(row)
    return row


@router.delete(
    "/{stock_upload_hdr_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_stock_upload_header(
    stock_upload_hdr_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_stock_upload_header_or_404(db, stock_upload_hdr_id)
    db.delete(row)
    db.commit()
