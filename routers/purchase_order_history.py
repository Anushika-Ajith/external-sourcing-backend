from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from database import get_db
from models import (
    PurchaseOrderHeaderHistory,
    PurchaseOrderLineHistory,
    User,
)
from schemas.purchase_order_history import (
    PurchaseOrderHeaderHistoryCreate,
    PurchaseOrderHeaderHistoryResponse,
    PurchaseOrderHeaderHistoryUpdate,
    PurchaseOrderLineHistoryCreate,
    PurchaseOrderLineHistoryResponse,
    PurchaseOrderLineHistoryUpdate,
)
from services.auth_utils import get_current_user
from services.company_scope import resolve_company_id

router = APIRouter(
    prefix="/purchase-order-history",
    tags=["purchase-order-history"],
    responses={404: {"description": "Not found"}},
)


# ---------------------------------------------------------------------------
# Header history
# ---------------------------------------------------------------------------


@router.get("/headers", response_model=List[PurchaseOrderHeaderHistoryResponse])
async def list_purchase_order_header_history(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    document_type: Optional[str] = Query(None),
    po_number: Optional[str] = Query(None),
    version_no: Optional[int] = Query(None),
    po_id: Optional[int] = Query(None),
    history_action: Optional[str] = Query(None),
    revision_no: Optional[int] = Query(None),
    current_revision: Optional[bool] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    q = db.query(PurchaseOrderHeaderHistory)
    if tenant_id is not None:
        q = q.filter(PurchaseOrderHeaderHistory.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(PurchaseOrderHeaderHistory.company_id == company_id)
    if document_type:
        q = q.filter(PurchaseOrderHeaderHistory.document_type == document_type)
    if po_number:
        q = q.filter(PurchaseOrderHeaderHistory.po_number == po_number)
    if version_no is not None:
        q = q.filter(PurchaseOrderHeaderHistory.version_no == version_no)
    if po_id is not None:
        q = q.filter(PurchaseOrderHeaderHistory.po_id == po_id)
    if history_action:
        q = q.filter(PurchaseOrderHeaderHistory.history_action == history_action)
    if revision_no is not None:
        q = q.filter(PurchaseOrderHeaderHistory.revision_no == revision_no)
    if current_revision is not None:
        q = q.filter(PurchaseOrderHeaderHistory.current_revision == current_revision)

    _ = current_user
    return (
        q.order_by(PurchaseOrderHeaderHistory.history_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get(
    "/headers/{history_id}",
    response_model=PurchaseOrderHeaderHistoryResponse,
)
async def get_purchase_order_header_history(
    history_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(PurchaseOrderHeaderHistory)
        .filter(PurchaseOrderHeaderHistory.history_id == history_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PO header history not found",
        )
    return row


@router.get(
    "/headers/by-po-id/{po_id}",
    response_model=List[PurchaseOrderHeaderHistoryResponse],
)
async def list_header_history_by_po_id(
    po_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(PurchaseOrderHeaderHistory)
        .filter(PurchaseOrderHeaderHistory.po_id == po_id)
        .order_by(PurchaseOrderHeaderHistory.history_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.post(
    "/headers",
    response_model=PurchaseOrderHeaderHistoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_purchase_order_header_history(
    body: PurchaseOrderHeaderHistoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = body.model_dump()
    if data.get("history_created_by") is None:
        data["history_created_by"] = current_user.user_id
    row = PurchaseOrderHeaderHistory(**data)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put(
    "/headers/{history_id}",
    response_model=PurchaseOrderHeaderHistoryResponse,
)
async def update_purchase_order_header_history(
    history_id: int,
    body: PurchaseOrderHeaderHistoryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(PurchaseOrderHeaderHistory)
        .filter(PurchaseOrderHeaderHistory.history_id == history_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PO header history not found",
        )
    data = body.model_dump(exclude_unset=True)
    if data and data.get("updated_by") is None:
        data["updated_by"] = current_user.user_id
    for field, value in data.items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/headers/{history_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_purchase_order_header_history(
    history_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(PurchaseOrderHeaderHistory)
        .filter(PurchaseOrderHeaderHistory.history_id == history_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PO header history not found",
        )
    db.delete(row)
    db.commit()


# ---------------------------------------------------------------------------
# Line history
# ---------------------------------------------------------------------------


@router.get("/lines", response_model=List[PurchaseOrderLineHistoryResponse])
async def list_purchase_order_line_history(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    document_type: Optional[str] = Query(None),
    po_number: Optional[str] = Query(None),
    version_no: Optional[int] = Query(None),
    po_id: Optional[int] = Query(None),
    line_id: Optional[int] = Query(None),
    line_no: Optional[int] = Query(None),
    history_action: Optional[str] = Query(None),
    revision_no: Optional[int] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    q = db.query(PurchaseOrderLineHistory)
    if tenant_id is not None:
        q = q.filter(PurchaseOrderLineHistory.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(PurchaseOrderLineHistory.company_id == company_id)
    if document_type:
        q = q.filter(PurchaseOrderLineHistory.document_type == document_type)
    if po_number:
        q = q.filter(PurchaseOrderLineHistory.po_number == po_number)
    if version_no is not None:
        q = q.filter(PurchaseOrderLineHistory.version_no == version_no)
    if po_id is not None:
        q = q.filter(PurchaseOrderLineHistory.po_id == po_id)
    if line_id is not None:
        q = q.filter(PurchaseOrderLineHistory.line_id == line_id)
    if line_no is not None:
        q = q.filter(PurchaseOrderLineHistory.line_no == line_no)
    if history_action:
        q = q.filter(PurchaseOrderLineHistory.history_action == history_action)
    if revision_no is not None:
        q = q.filter(PurchaseOrderLineHistory.revision_no == revision_no)
    return (
        q.order_by(PurchaseOrderLineHistory.history_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get(
    "/lines/{history_id}",
    response_model=PurchaseOrderLineHistoryResponse,
)
async def get_purchase_order_line_history(
    history_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(PurchaseOrderLineHistory)
        .filter(PurchaseOrderLineHistory.history_id == history_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PO line history not found",
        )
    return row


@router.get(
    "/lines/by-line-id/{line_id}",
    response_model=List[PurchaseOrderLineHistoryResponse],
)
async def list_line_history_by_line_id(
    line_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(PurchaseOrderLineHistory)
        .filter(PurchaseOrderLineHistory.line_id == line_id)
        .order_by(PurchaseOrderLineHistory.history_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get(
    "/lines/by-po-id/{po_id}",
    response_model=List[PurchaseOrderLineHistoryResponse],
)
async def list_line_history_by_po_id(
    po_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(PurchaseOrderLineHistory)
        .filter(PurchaseOrderLineHistory.po_id == po_id)
        .order_by(
            PurchaseOrderLineHistory.line_no.asc(),
            PurchaseOrderLineHistory.history_id.desc(),
        )
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.post(
    "/lines",
    response_model=PurchaseOrderLineHistoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_purchase_order_line_history(
    body: PurchaseOrderLineHistoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = body.model_dump()
    if data.get("history_created_by") is None:
        data["history_created_by"] = current_user.user_id
    row = PurchaseOrderLineHistory(**data)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put(
    "/lines/{history_id}",
    response_model=PurchaseOrderLineHistoryResponse,
)
async def update_purchase_order_line_history(
    history_id: int,
    body: PurchaseOrderLineHistoryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(PurchaseOrderLineHistory)
        .filter(PurchaseOrderLineHistory.history_id == history_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PO line history not found",
        )
    data = body.model_dump(exclude_unset=True)
    if data and data.get("updated_by") is None:
        data["updated_by"] = current_user.user_id
    for field, value in data.items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/lines/{history_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_purchase_order_line_history(
    history_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(PurchaseOrderLineHistory)
        .filter(PurchaseOrderLineHistory.history_id == history_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PO line history not found",
        )
    db.delete(row)
    db.commit()
