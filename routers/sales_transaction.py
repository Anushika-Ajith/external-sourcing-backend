from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import Product, PurchaseOrderLine, SalesTransaction, User
from schemas.sales_adc_spike import (
    CreatePoFromSalesAdcSpikesRequest,
    CreatePoFromSalesAdcSpikesResponse,
    SalesAdcSpikeListResponse,
    SalesAdcSpikeResponse,
)
from schemas.sales_transaction import (
    SalesTransactionCreate,
    SalesTransactionResponse,
    SalesTransactionUpdate,
)
from schemas.sales_product_match import (
    SalesProductMatchConfirmRequest,
    SalesProductMatchConfirmResponse,
)
from schemas.sales_dashboard_summary import SalesDashboardSummaryResponse
from schemas.sales_transaction_trends import SalesTransactionTrendResponse
from services.auth_utils import get_current_user
from services.sales_product_match_service import confirm_sales_line_product_match
from services.company_scope import resolve_company_id
from services.sales_adc_spike_po_create import create_purchase_order_from_adc_spike_products
from services.whatsapp_auth import require_whatsapp_api_key
from services.whatsapp_notify_service import notify_po_created_whatsapp
from services.sales_dashboard_summary import build_sales_dashboard_summary
from services.sales_transaction_adc import PERIOD_DAYS, detect_sales_adc_spikes
from services.sales_transaction_trends import list_sales_trends_by_product

router = APIRouter(
    prefix="/sales-transactions",
    tags=["sales-transactions"],
    responses={404: {"description": "Not found"}},
)


def _request_client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    if request.client and request.client.host:
        return request.client.host[:45]
    return None


def _get_sales_transaction_or_404(
    db: Session, sales_transaction_id: int
) -> SalesTransaction:
    row = (
        db.query(SalesTransaction)
        .filter(SalesTransaction.sales_transaction_id == sales_transaction_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sales transaction not found",
        )
    return row


def _ensure_product_exists(db: Session, product_id: int) -> None:
    exists = (
        db.query(Product.product_id)
        .filter(Product.product_id == product_id)
        .first()
    )
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Product with id {product_id} not found",
        )


def _apply_list_filters(
    q,
    *,
    tenant_id: Optional[int],
    company_id: Optional[int],
    product_id: Optional[int],
    product_name: Optional[str],
    category: Optional[str],
    transaction_date_from: Optional[date],
    transaction_date_to: Optional[date],
):
    company_id = resolve_company_id(company_id)
    if tenant_id is not None:
        q = q.filter(SalesTransaction.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(SalesTransaction.company_id == company_id)
    if product_id is not None:
        q = q.filter(SalesTransaction.product_id == product_id)
    if product_name:
        q = q.filter(SalesTransaction.product_name.ilike(f"%{product_name}%"))
    if category:
        q = q.filter(SalesTransaction.category.ilike(f"%{category}%"))
    if transaction_date_from is not None:
        q = q.filter(SalesTransaction.transaction_date >= transaction_date_from)
    if transaction_date_to is not None:
        q = q.filter(SalesTransaction.transaction_date <= transaction_date_to)
    return q


@router.get(
    "/whatsapp/by-phone",
    response_model=List[SalesTransactionResponse],
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def list_sales_transactions_for_whatsapp_phone(
    mobile: Optional[str] = Query(
        None, description="WhatsApp sender phone (optional; unused for filtering)"
    ),
    product_id: Optional[int] = Query(None),
    product_name: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    transaction_date_from: Optional[date] = Query(None),
    transaction_date_to: Optional[date] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """
    WhatsApp bot path (MVP): return sales_transactions with WhatsApp API key only.

    Vendor/phone registration checks are disabled for now.
    Optional filters still apply when provided; otherwise returns all rows
    (paginated). Auth: X-WhatsApp-API-Key. No user JWT.
    """
    _ = mobile  # reserved for future per-user scoping

    total = int(
        db.query(func.count(SalesTransaction.sales_transaction_id)).scalar() or 0
    )

    q = db.query(SalesTransaction)
    if product_id is not None:
        q = q.filter(SalesTransaction.product_id == product_id)
    if product_name:
        q = q.filter(SalesTransaction.product_name.ilike(f"%{product_name}%"))
    if category:
        q = q.filter(SalesTransaction.category.ilike(f"%{category}%"))
    if transaction_date_from is not None:
        q = q.filter(SalesTransaction.transaction_date >= transaction_date_from)
    if transaction_date_to is not None:
        q = q.filter(SalesTransaction.transaction_date <= transaction_date_to)

    rows = (
        q.order_by(
            SalesTransaction.transaction_date.desc(),
            SalesTransaction.sales_transaction_id.desc(),
        )
        .offset(skip)
        .limit(limit)
        .all()
    )
    try:
        conn_info = db.execute(
            text(
                "select current_database(), current_schema(), "
                "coalesce(host(inet_server_addr()), 'local-socket'), inet_server_port()"
            )
        ).first()
        db_label = (
            f"db={conn_info[0]} schema={conn_info[1]} "
            f"server={conn_info[2]}:{conn_info[3]}"
        )
    except Exception as exc:  # diagnostics only; never break the request
        db_label = f"db=unknown ({exc.__class__.__name__})"
    print(
        f"[SALES-WHATSAPP] total_rows={total} returned={len(rows)} "
        f"skip={skip} limit={limit} mobile={mobile or '-'} {db_label}"
    )
    return rows


@router.get(
    "/whatsapp/dashboard-summary",
    response_model=SalesDashboardSummaryResponse,
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def sales_dashboard_summary_for_whatsapp(
    mobile: Optional[str] = Query(
        None, description="WhatsApp sender phone (optional; unused for filtering)"
    ),
    focus: Optional[str] = Query(
        None, description="Optional focus question for insight wording"
    ),
    product_id: Optional[int] = Query(None),
    product_name: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    transaction_date_from: Optional[date] = Query(None),
    transaction_date_to: Optional[date] = Query(None),
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Pre-aggregate sales_transactions in Postgres for the WhatsApp Chart.js dashboard.

    Returns KPIs, monthly/daily series, top products, category share, and compact
    date×product×category records — no raw transaction dump. Auth: X-WhatsApp-API-Key.

    Matches /whatsapp/by-phone scoping: no forced company filter unless query params set.
    """
    _ = mobile  # reserved for future per-user scoping
    summary = build_sales_dashboard_summary(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        product_id=product_id,
        product_name=product_name,
        category=category,
        transaction_date_from=transaction_date_from,
        transaction_date_to=transaction_date_to,
        focus_question=focus,
    )
    print(
        f"[SALES-WHATSAPP-SUMMARY] rowCount={summary.get('rowCount')} "
        f"records={len(summary.get('records') or [])} "
        f"monthly={len(summary.get('monthly') or [])} "
        f"mobile={mobile or '-'}"
    )
    return summary


@router.get("/trends/by-product", response_model=List[SalesTransactionTrendResponse])
async def list_sales_transaction_trends_by_product(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    category: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    _ = current_user
    return list_sales_trends_by_product(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        category=category,
        skip=skip,
        limit=limit,
    )


@router.get("/count")
async def count_sales_transactions(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    product_id: Optional[int] = Query(None),
    product_name: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    transaction_date_from: Optional[date] = Query(None),
    transaction_date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    _ = current_user
    q = _apply_list_filters(
        db.query(func.count(SalesTransaction.sales_transaction_id)),
        tenant_id=tenant_id,
        company_id=company_id,
        product_id=product_id,
        product_name=product_name,
        category=category,
        transaction_date_from=transaction_date_from,
        transaction_date_to=transaction_date_to,
    )
    return {"total": int(q.scalar() or 0)}


@router.get("/adc-spikes", response_model=SalesAdcSpikeListResponse)
async def list_sales_adc_spikes(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    category: Optional[str] = Query(None),
    only_huge: bool = Query(
        False,
        description="When true, only products with growth ≥ huge_growth_pct (default 25%)",
    ),
    only_sustained: bool = Query(
        False,
        description="When true, require 90-day sustained growth (not a one-month spike)",
    ),
    min_growth_pct: float = Query(
        0.0,
        ge=0.0,
        description="Minimum growth % vs previous period ADC",
    ),
    huge_growth_pct: float = Query(25.0, ge=0.0, le=500.0),
    period_days: int = Query(PERIOD_DAYS, ge=7, le=60),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    ADC-based demand spikes from sales_transactions.

    Compares consecutive 30-day windows: previous ADC, current ADC, growth %,
    and whether growth is sustained across three periods when 90+ days exist.
    """
    company_id = resolve_company_id(company_id)
    _ = current_user
    items = detect_sales_adc_spikes(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        category=category,
        only_huge=only_huge,
        only_sustained=only_sustained,
        min_growth_pct=min_growth_pct,
        huge_growth_pct=huge_growth_pct,
        period_days=period_days,
        limit=limit,
    )
    anchor = items[0].data_anchor_date if items else None
    if anchor is None:
        anchor_row = db.query(func.max(SalesTransaction.transaction_date))
        if tenant_id is not None:
            anchor_row = anchor_row.filter(SalesTransaction.tenant_id == tenant_id)
        if company_id is not None:
            anchor_row = anchor_row.filter(SalesTransaction.company_id == company_id)
        anchor = anchor_row.scalar()

    return SalesAdcSpikeListResponse(
        count=len(items),
        period_days=period_days,
        data_anchor_date=anchor,
        items=[SalesAdcSpikeResponse(**item.__dict__) for item in items],
    )


@router.post(
    "/adc-spikes/create-purchase-order",
    response_model=CreatePoFromSalesAdcSpikesResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_purchase_order_from_adc_sales_spikes(
    body: CreatePoFromSalesAdcSpikesRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create one PO with lines for selected ADC spike products (suggested qty per line)."""
    vendor_id = body.vendor_id
    if vendor_id is None:
        if current_user.vendor_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="vendor_id is required when the user is not linked to a vendor",
            )
        vendor_id = int(current_user.vendor_id)

    header = create_purchase_order_from_adc_spike_products(
        db,
        product_ids=body.product_ids,
        tenant_id=body.tenant_id,
        company_id=resolve_company_id(body.company_id),
        vendor_id=int(vendor_id),
        user_id=int(current_user.user_id),
        department_id=body.department_id,
        document_type=body.document_type,
        version_no=body.version_no,
        quantities=body.quantities,
    )
    line_count = (
        db.query(PurchaseOrderLine)
        .filter(PurchaseOrderLine.po_id == header.po_id)
        .count()
    )

    # After PO is committed: fire-and-forget WhatsApp notify (never blocks / fails the API).
    try:
        notify_po_created_whatsapp(
            po_number=header.po_number,
            po_id=int(header.po_id) if header.po_id is not None else None,
            line_count=line_count,
        )
    except Exception as notify_exc:
        # Belt-and-suspenders — notify_po_created_whatsapp already swallows errors.
        print(f"[PO-WHATSAPP] unexpected notify error (ignored): {notify_exc}")

    return CreatePoFromSalesAdcSpikesResponse(
        po_id=header.po_id,
        po_number=header.po_number,
        line_count=line_count,
        po_total_amount=header.po_total_amount,
        net_total_amount=header.net_total_amount,
        message=f"Purchase order {header.po_number} created with {line_count} line(s).",
    )


@router.get("", response_model=List[SalesTransactionResponse])
async def list_sales_transactions(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    product_id: Optional[int] = Query(None),
    product_name: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    transaction_date_from: Optional[date] = Query(None),
    transaction_date_to: Optional[date] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    q = _apply_list_filters(
        db.query(SalesTransaction),
        tenant_id=tenant_id,
        company_id=company_id,
        product_id=product_id,
        product_name=product_name,
        category=category,
        transaction_date_from=transaction_date_from,
        transaction_date_to=transaction_date_to,
    )
    return (
        q.order_by(SalesTransaction.sales_transaction_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.delete("")
async def delete_all_sales_transactions(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete all sales transactions for the scoped tenant/company."""
    _ = current_user
    company_id = resolve_company_id(company_id)
    q = _apply_list_filters(
        db.query(SalesTransaction),
        tenant_id=tenant_id,
        company_id=company_id,
        product_id=None,
        product_name=None,
        category=None,
        transaction_date_from=None,
        transaction_date_to=None,
    )
    deleted = q.delete(synchronize_session=False)
    db.commit()
    return {"deleted": int(deleted or 0)}


@router.get(
    "/{sales_transaction_id}",
    response_model=SalesTransactionResponse,
)
async def get_sales_transaction(
    sales_transaction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _get_sales_transaction_or_404(db, sales_transaction_id)


@router.post(
    "",
    response_model=SalesTransactionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sales_transaction(
    body: SalesTransactionCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _ensure_product_exists(db, body.product_id)
    data = body.model_dump()
    data["company_id"] = resolve_company_id(data.get("company_id"))
    data["created_by"] = current_user.user_id
    data["created_ip"] = _request_client_ip(request)
    row = SalesTransaction(**data)
    try:
        db.add(row)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not create sales transaction: {exc.orig}",
        )
    db.refresh(row)
    return row


@router.put(
    "/{sales_transaction_id}",
    response_model=SalesTransactionResponse,
)
async def update_sales_transaction(
    sales_transaction_id: int,
    body: SalesTransactionUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_sales_transaction_or_404(db, sales_transaction_id)
    updates = body.model_dump(exclude_unset=True)
    if "product_id" in updates:
        _ensure_product_exists(db, updates["product_id"])
    for key, value in updates.items():
        setattr(row, key, value)
    row.modified_by = current_user.user_id
    row.modified_on = datetime.utcnow()
    row.modified_ip = _request_client_ip(request)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not update sales transaction: {exc.orig}",
        )
    db.refresh(row)
    return row


@router.post(
    "/match/confirm",
    response_model=SalesProductMatchConfirmResponse,
)
async def confirm_sales_product_match(
    body: SalesProductMatchConfirmRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Apply user-selected catalog product for an ambiguous sales upload line.

    Used when a line was marked yellow (multiple matches) or red (manual pick).
    """
    tenant_id = int(getattr(current_user, "token_tenant_id", None) or body.tenantId)
    company_id = resolve_company_id(
        getattr(current_user, "token_company_id", None) or body.companyId
    )
    result = confirm_sales_line_product_match(
        db,
        line_index=int(body.lineIndex),
        selected_product_id=int(body.selectedProductId),
        sales_line=body.salesLine,
        tenant_id=tenant_id,
        company_id=company_id,
        created_by=current_user.user_id,
        created_ip=_request_client_ip(request),
        create_alias=body.createAlias,
    )
    return SalesProductMatchConfirmResponse(**result)


@router.delete(
    "/{sales_transaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_sales_transaction(
    sales_transaction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_sales_transaction_or_404(db, sales_transaction_id)
    db.delete(row)
    db.commit()
