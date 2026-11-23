from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import StockUploadDetail, User
from schemas.stock_upload_detail import (
    StockUploadDetailCreate,
    StockUploadDetailResponse,
    StockUploadDetailUpdate,
)
from services.auth_utils import get_current_user
from services.company_scope import resolve_company_id

router = APIRouter(
    prefix="/stock-upload-details",
    tags=["stock-upload-details"],
    responses={404: {"description": "Not found"}},
)


def _get_stock_upload_detail_or_404(
    db: Session, stock_upload_dtl_id: int
) -> StockUploadDetail:
    row = (
        db.query(StockUploadDetail)
        .filter(StockUploadDetail.stock_upload_dtl_id == stock_upload_dtl_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stock upload detail not found",
        )
    return row


def _apply_list_filters(
    q,
    *,
    tenant_id: Optional[int],
    company_id: Optional[int],
    stock_upload_hdr_id: Optional[int],
    line_no: Optional[int],
    supplier_product_id: Optional[int],
):
    company_id = resolve_company_id(company_id)
    if tenant_id is not None:
        q = q.filter(StockUploadDetail.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(StockUploadDetail.company_id == company_id)
    if stock_upload_hdr_id is not None:
        q = q.filter(StockUploadDetail.stock_upload_hdr_id == stock_upload_hdr_id)
    if line_no is not None:
        q = q.filter(StockUploadDetail.line_no == line_no)
    if supplier_product_id is not None:
        q = q.filter(StockUploadDetail.supplier_product_id == supplier_product_id)
    return q


@router.get("", response_model=List[StockUploadDetailResponse])
async def list_stock_upload_details(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    stock_upload_hdr_id: Optional[int] = Query(None),
    line_no: Optional[int] = Query(None),
    supplier_product_id: Optional[int] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    q = _apply_list_filters(
        db.query(StockUploadDetail),
        tenant_id=tenant_id,
        company_id=company_id,
        stock_upload_hdr_id=stock_upload_hdr_id,
        line_no=line_no,
        supplier_product_id=supplier_product_id,
    )
    return (
        q.order_by(StockUploadDetail.stock_upload_dtl_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get(
    "/{stock_upload_dtl_id}",
    response_model=StockUploadDetailResponse,
)
async def get_stock_upload_detail(
    stock_upload_dtl_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _get_stock_upload_detail_or_404(db, stock_upload_dtl_id)


@router.post(
    "",
    response_model=StockUploadDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_stock_upload_detail(
    body: StockUploadDetailCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = body.model_dump()
    data["company_id"] = resolve_company_id(data.get("company_id"))
    data["created_by"] = current_user.user_id
    data["updated_by"] = current_user.user_id
    row = StockUploadDetail(**data)
    try:
        db.add(row)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not create stock upload detail: {exc.orig}",
        )
    db.refresh(row)
    return row


@router.put(
    "/{stock_upload_dtl_id}",
    response_model=StockUploadDetailResponse,
)
async def update_stock_upload_detail(
    stock_upload_dtl_id: int,
    body: StockUploadDetailUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_stock_upload_detail_or_404(db, stock_upload_dtl_id)
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
            detail=f"Could not update stock upload detail: {exc.orig}",
        )
    db.refresh(row)
    return row


@router.delete(
    "/{stock_upload_dtl_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_stock_upload_detail(
    stock_upload_dtl_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_stock_upload_detail_or_404(db, stock_upload_dtl_id)
    db.delete(row)
    db.commit()
