from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import PurchaseOrderLine, Stock, User
from schemas.stock import StockCreate, StockResponse, StockUpdate
from schemas.stock_spike import (
    CreatePoFromSalesSpikesRequest,
    CreatePoFromSalesSpikesResponse,
    StockSpikeAnalysisResponse,
    StockSpikeDetectionListResponse,
)
from services.auth_utils import get_current_user
from services.company_scope import resolve_company_id
from services.stock_pipeline_qty import (
    compute_pipeline_qty_by_product_codes,
    enrich_stock_quantities,
)
from services.stock_sales_spike import analyze_stock_spike, detect_stock_sales_spikes
from services.stock_spike_po_create import create_purchase_order_from_spike_stocks

router = APIRouter(
    prefix="/stock",
    tags=["stock"],
    responses={404: {"description": "Not found"}},
)


def _get_stock_or_404(db: Session, stock_id: int) -> Stock:
    row = db.query(Stock).filter(Stock.stock_id == stock_id).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stock record not found",
        )
    return row


def _apply_list_filters(
    q,
    *,
    tenant_id: Optional[int],
    company_id: Optional[int],
    product_code: Optional[str],
    barcode: Optional[str],
    category: Optional[str],
    brand: Optional[str],
    preferred_supplier: Optional[str],
    search: Optional[str] = None,
    below_reorder_level: bool = False,
):
    company_id = resolve_company_id(company_id)
    if tenant_id is not None:
        q = q.filter(Stock.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(Stock.company_id == company_id)
    if product_code:
        q = q.filter(Stock.product_code.ilike(f"%{product_code}%"))
    if barcode:
        q = q.filter(Stock.barcode.ilike(f"%{barcode}%"))
    if category:
        q = q.filter(Stock.category.ilike(f"%{category}%"))
    if brand:
        q = q.filter(Stock.brand.ilike(f"%{brand}%"))
    if preferred_supplier:
        q = q.filter(Stock.preferred_supplier.ilike(f"%{preferred_supplier}%"))
    if search and search.strip():
        term = f"%{search.strip()}%"
        q = q.filter(
            or_(
                Stock.product_name.ilike(term),
                Stock.product_code.ilike(term),
                Stock.barcode.ilike(term),
                Stock.brand.ilike(term),
                Stock.category.ilike(term),
                Stock.preferred_supplier.ilike(term),
            )
        )
    # below_reorder_level uses stock + pipeline and is applied after enrichment.
    return q


def _stock_to_response(stock: Stock, pipeline_map: dict) -> StockResponse:
    product_code = (stock.product_code or "").strip()
    pipeline = pipeline_map.get(product_code)
    if pipeline is None:
        pipeline = pipeline_map.get(product_code.upper(), Decimal("0"))
    if not isinstance(pipeline, Decimal):
        pipeline = Decimal(str(pipeline or 0))
    extras = enrich_stock_quantities(
        available_stock=stock.available_stock,
        reorder_level=stock.reorder_level,
        pipeline_qty=pipeline,
    )
    return StockResponse(
        stock_id=stock.stock_id,
        product_code=stock.product_code,
        barcode=stock.barcode,
        product_name=stock.product_name,
        category=stock.category,
        brand=stock.brand,
        unit=stock.unit,
        available_stock=stock.available_stock,
        reorder_level=stock.reorder_level,
        reorder_qty=stock.reorder_qty,
        last_week_sales=stock.last_week_sales,
        weekly_trend=stock.weekly_trend,
        suggested_qty=stock.suggested_qty,
        preferred_supplier=stock.preferred_supplier,
        tenant_id=stock.tenant_id,
        company_id=stock.company_id,
        created_by=stock.created_by,
        updated_by=stock.updated_by,
        created_at=stock.created_at,
        updated_at=stock.updated_at,
        pipeline_qty=extras["pipeline_qty"],
        stock_with_pipeline=extras["stock_with_pipeline"],
        needs_reorder=bool(extras["needs_reorder"]),
    )


@router.get("", response_model=List[StockResponse])
async def list_stock(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    product_code: Optional[str] = Query(None),
    barcode: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    brand: Optional[str] = Query(None),
    preferred_supplier: Optional[str] = Query(None),
    search: Optional[str] = Query(
        None,
        description="Case-insensitive match across name, code, barcode, brand, category, supplier",
    ),
    below_reorder_level: bool = Query(
        False,
        description=(
            "When true, only products where available_stock + pipeline_qty "
            "< reorder_level"
        ),
    ),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    q = _apply_list_filters(
        db.query(Stock),
        tenant_id=tenant_id,
        company_id=resolve_company_id(company_id),
        product_code=product_code,
        barcode=barcode,
        category=category,
        brand=brand,
        preferred_supplier=preferred_supplier,
        search=search,
        below_reorder_level=below_reorder_level,
    )
    ordered = q.order_by(Stock.stock_id.desc())

    if below_reorder_level:
        candidate_limit = min(max(skip + limit, limit), 1000)
        rows = ordered.limit(candidate_limit).all()
    else:
        rows = ordered.offset(skip).limit(limit).all()

    pipeline_map = compute_pipeline_qty_by_product_codes(
        db,
        tenant_id=tenant_id if tenant_id is not None else (rows[0].tenant_id if rows else 1),
        company_id=company_id,
        product_codes=[row.product_code for row in rows],
    )

    if below_reorder_level:
        enriched = [_stock_to_response(row, pipeline_map) for row in rows]
        filtered = [row for row in enriched if row.needs_reorder]
        return filtered[skip : skip + limit]

    return [_stock_to_response(row, pipeline_map) for row in rows]


def _spike_to_response(analysis) -> StockSpikeAnalysisResponse:
    return StockSpikeAnalysisResponse(**analysis.__dict__)


@router.delete("")
async def delete_all_stock(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete all stock records for the scoped tenant/company (testing helper)."""
    _ = current_user
    q = _apply_list_filters(
        db.query(Stock),
        tenant_id=tenant_id,
        company_id=resolve_company_id(company_id),
        product_code=None,
        barcode=None,
        category=None,
        brand=None,
        preferred_supplier=None,
        search=None,
        below_reorder_level=False,
    )
    deleted = q.delete(synchronize_session=False)
    db.commit()
    return {"deleted": int(deleted or 0)}


@router.get("/sales-spikes", response_model=StockSpikeDetectionListResponse)
async def list_sales_spike_products(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    category: Optional[str] = Query(None),
    brand: Optional[str] = Query(None),
    only_huge: bool = Query(
        True,
        description="When true, only products with unusually high last_week_sales",
    ),
    only_sustained: bool = Query(
        True,
        description="When true, require PELT/IQR-backed sustained demand (not one-off)",
    ),
    limit: int = Query(50, ge=1, le=500),
    lookback_weeks: int = Query(12, ge=4, le=52),
    ewma_alpha: float = Query(0.35, ge=0.05, le=1.0),
    pelt_penalty: float = Query(3.0, ge=0.5, le=50.0),
    iqr_k: float = Query(1.5, ge=0.5, le=5.0),
    min_uplift_ratio: float = Query(1.25, ge=1.0, le=10.0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List products with large, sustained sales spikes.

    Uses EWMA on reconstructed weekly sales (from last_week_sales + weekly_trend),
    PELT change-point detection for sustained lifts, and IQR to drop one-off outliers.
    """
    company_id = resolve_company_id(company_id)
    items = detect_stock_sales_spikes(
        db,
        tenant_id=tenant_id,
        company_id=resolve_company_id(company_id),
        category=category,
        brand=brand,
        only_huge=only_huge,
        only_sustained=only_sustained,
        limit=limit,
        lookback_weeks=lookback_weeks,
        ewma_alpha=ewma_alpha,
        pelt_penalty=pelt_penalty,
        iqr_k=iqr_k,
        min_uplift_ratio=min_uplift_ratio,
    )
    responses = [_spike_to_response(a) for a in items]
    return StockSpikeDetectionListResponse(count=len(responses), items=responses)


@router.post(
    "/sales-spikes/create-purchase-order",
    response_model=CreatePoFromSalesSpikesResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_purchase_order_from_sales_spikes(
    body: CreatePoFromSalesSpikesRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create one PO with lines for selected stock products (suggested/reorder qty per line).
    PO number comes from Nest next-numbering (PURCHASE_ORDER).
    """
    vendor_id = body.vendor_id
    if vendor_id is None:
        if current_user.vendor_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="vendor_id is required when the user is not linked to a vendor",
            )
        vendor_id = int(current_user.vendor_id)

    header = create_purchase_order_from_spike_stocks(
        db,
        stock_ids=body.stock_ids,
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

    vendor_name = None
    try:
        from models import Vendor

        vendor_row = db.query(Vendor).filter(Vendor.vendor_id == int(vendor_id)).first()
        if vendor_row is not None:
            vendor_name = vendor_row.company_name
    except Exception:
        vendor_name = None
    try:
        from services.whatsapp_notify_service import notify_po_created_whatsapp

        notify_po_created_whatsapp(
            po_number=header.po_number,
            po_id=int(header.po_id) if header.po_id is not None else None,
            vendor_name=vendor_name,
            line_count=line_count,
        )
    except Exception as notify_exc:
        print(f"[PO-WHATSAPP] unexpected notify error (ignored): {notify_exc}")

    return CreatePoFromSalesSpikesResponse(
        po_id=header.po_id,
        po_number=header.po_number,
        line_count=line_count,
        po_total_amount=header.po_total_amount,
        net_total_amount=header.net_total_amount,
        message=f"Purchase order {header.po_number} created with {line_count} line(s).",
        approval_status=header.approval_status,
        document_type=header.document_type,
        version_no=header.version_no,
        tenant_id=header.tenant_id,
        company_id=header.company_id,
    )


@router.get("/{stock_id}/spike-analysis", response_model=StockSpikeAnalysisResponse)
async def get_stock_spike_analysis(
    stock_id: int,
    lookback_weeks: int = Query(12, ge=4, le=52),
    ewma_alpha: float = Query(0.35, ge=0.05, le=1.0),
    pelt_penalty: float = Query(3.0, ge=0.5, le=50.0),
    iqr_k: float = Query(1.5, ge=0.5, le=5.0),
    min_uplift_ratio: float = Query(1.25, ge=1.0, le=10.0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_stock_or_404(db, stock_id)
    analysis = analyze_stock_spike(
        stock_id=row.stock_id,
        product_code=row.product_code,
        product_name=row.product_name,
        category=row.category,
        brand=row.brand,
        last_week_sales=row.last_week_sales,
        weekly_trend=row.weekly_trend,
        available_stock=row.available_stock,
        suggested_qty=row.suggested_qty,
        lookback_weeks=lookback_weeks,
        ewma_alpha=ewma_alpha,
        pelt_penalty=pelt_penalty,
        iqr_k=iqr_k,
        min_uplift_ratio=min_uplift_ratio,
    )
    return _spike_to_response(analysis)


def _enrich_stock_row(db: Session, stock: Stock) -> StockResponse:
    tenant_id = stock.tenant_id or 1
    company_id = resolve_company_id(stock.company_id)
    pipeline_map = compute_pipeline_qty_by_product_codes(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        product_codes=[stock.product_code],
    )
    return _stock_to_response(stock, pipeline_map)


@router.get("/{stock_id}", response_model=StockResponse)
async def get_stock(
    stock_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _enrich_stock_row(db, _get_stock_or_404(db, stock_id))


@router.post("", response_model=StockResponse, status_code=status.HTTP_201_CREATED)
async def create_stock(
    body: StockCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = body.model_dump()
    data["company_id"] = resolve_company_id(data.get("company_id"))
    data["created_by"] = current_user.user_id
    data["updated_by"] = current_user.user_id
    row = Stock(**data)
    try:
        db.add(row)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not create stock record: {exc.orig}",
        )
    db.refresh(row)
    return _enrich_stock_row(db, row)


@router.put("/{stock_id}", response_model=StockResponse)
async def update_stock(
    stock_id: int,
    body: StockUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_stock_or_404(db, stock_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    row.updated_by = current_user.user_id
    row.updated_at = datetime.utcnow()

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not update stock record: {exc.orig}",
        )
    db.refresh(row)
    return _enrich_stock_row(db, row)


@router.delete("/{stock_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_stock(
    stock_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_stock_or_404(db, stock_id)
    db.delete(row)
    db.commit()
