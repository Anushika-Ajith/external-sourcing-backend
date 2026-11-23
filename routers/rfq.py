from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session, joinedload, contains_eager
from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_, func, or_
from typing import List, Optional, Tuple
from datetime import datetime, timezone, date
import requests

from database import get_db
from models import RFQHeader, RFQLine, RFQVendor, User, VendorItem, VendorRate
from config import settings
from schemas.rfq import (
    RFQHeaderCreate,
    RFQHeaderUpdate,
    RFQHeaderResponse,
    RFQWorkflowStateResponse,
    PendingRFQCountResponse,
    SubmittedRFQCountResponse,
    RFQLineCreate,
    RFQLineUpdate,
    RFQLineResponse,
    RFQVendorCreate,
    RFQVendorUpdate,
    RFQVendorResponse,
    RFQVendorWorkflowStateResponse,
    RFQVendorMatchedItemRate,
    RFQVendorWorkflowPatchRequest,
    FreezedVendorRatesItemWiseResponse,
    FreezedRFQItemWiseResponse,
    FreezedVendorRatesAllRFQsResponse,
    FreezedVendorRateItem,
    FreezedVendorRateItemVendor,
)
from services.auth_utils import get_current_user
from services.next_numbering import get_next_rfq_number_from_nest
from services.company_scope import resolve_company_id

router = APIRouter(
    prefix="/rfq",
    tags=["rfq"],
    responses={404: {"description": "Not found"}},
)


def _parse_item_code_payload(payload: object) -> Optional[str]:
    if isinstance(payload, str):
        s = payload.strip()
        return s or None
    if isinstance(payload, dict):
        for key in ("itemCode", "item_code", "code", "erpCode", "erp_code", "value", "data"):
            val = payload.get(key)
            if val is None:
                continue
            if isinstance(val, dict):
                nested = val.get("itemCode") or val.get("item_code") or val.get("code")
                if nested:
                    return str(nested).strip()
            else:
                s = str(val).strip()
                if s:
                    return s
    return None


def _get_item_code_from_nest(item_id: int) -> Optional[str]:
    base_candidates: List[str] = []
    configured = (settings.nestjs_api_url or "").strip().rstrip("/")
    if configured:
        base_candidates.append(configured)
    # Item master may be on INTERNAL_API_URL (separate from Foundation).
    items_base = (settings.internal_api_url or "").strip().rstrip("/")
    if items_base and items_base not in base_candidates:
        base_candidates.append(items_base)

    for base in base_candidates:
        try:
            url = f"{base}/items/{int(item_id)}/item-code"
            print(f"[RFQ_WORKFLOW_STATE] item_code_request item_id={item_id} url={url}")
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            txt = (resp.text or "").strip()
            if not txt:
                continue
            try:
                payload = resp.json()
            except ValueError:
                return txt.strip('"')
            parsed = _parse_item_code_payload(payload)
            if parsed:
                return parsed
        except Exception:
            continue
    return None


def _resolve_vendor_item_for_rfq_line(
    db: Session,
    vendor_id: int,
    buyer_item_id: int,
    erp_code_from_nest: Optional[str],
) -> Tuple[Optional[VendorItem], Optional[str]]:
    """
    Match vendor_items row for an RFQ line.

    Primary: vendor_items.item_id (buyer/master catalog id), then Nest ERP code,
    then common ERP patterns like ITM{buyer_item_id}.
    """
    vendor_item = None
    erp_code = erp_code_from_nest

    vendor_item = (
        db.query(VendorItem)
        .filter(
            VendorItem.vendor_id == vendor_id,
            VendorItem.item_id == buyer_item_id,
        )
        .first()
    )

    if vendor_item is None and erp_code:
        vendor_item = (
            db.query(VendorItem)
            .filter(
                VendorItem.vendor_id == vendor_id,
                VendorItem.erp_code == erp_code,
            )
            .first()
        )

    if vendor_item is None:
        vendor_item = (
            db.query(VendorItem)
            .filter(
                VendorItem.vendor_id == vendor_id,
                or_(
                    VendorItem.erp_code == f"ITM{buyer_item_id}",
                    VendorItem.erp_code == f"ITM-{buyer_item_id}",
                    VendorItem.erp_code == str(buyer_item_id),
                ),
            )
            .first()
        )
        if vendor_item and not erp_code:
            erp_code = vendor_item.erp_code

    return vendor_item, erp_code


def _best_vendor_rate_for_rfq_line(
    db: Session,
    vendor_id: int,
    buyer_item_id: int,
    item_code_cache: dict[int, Optional[str]],
) -> Tuple[Optional[VendorRate], Optional[VendorItem]]:
    """
    Resolve vendor_items + latest vendor_rates for an RFQ line's buyer item_id.
    rfq_line.item_id is the buyer/master item id, NOT vendor_items.id.
    """
    if buyer_item_id not in item_code_cache:
        item_code_cache[buyer_item_id] = _get_item_code_from_nest(buyer_item_id)
    vendor_item, _ = _resolve_vendor_item_for_rfq_line(
        db, vendor_id, buyer_item_id, item_code_cache[buyer_item_id]
    )
    if vendor_item is None:
        return None, None
    best_rate = (
        db.query(VendorRate)
        .filter(VendorRate.item_id == vendor_item.id)
        .order_by(
            VendorRate.valid_from.desc().nullslast(),
            VendorRate.created_at.desc().nullslast(),
            VendorRate.id.desc(),
        )
        .first()
    )
    return best_rate, vendor_item


async def get_all_rfq_headers(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    document_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """
    Get all RFQ headers from rfq_header table.
    No authentication required.
    Optional filters: tenant_id, company_id, document_type, status
    """
    company_id = resolve_company_id(company_id)
    query = db.query(RFQHeader).options(
        joinedload(RFQHeader.lines),
        joinedload(RFQHeader.vendors)
    )

    if tenant_id:
        query = query.filter(RFQHeader.tenant_id == tenant_id)
    if company_id:
        query = query.filter(RFQHeader.company_id == company_id)
    if document_type:
        query = query.filter(RFQHeader.document_type == document_type)
    if status:
        query = query.filter(RFQHeader.status == status)

    rfq_headers = query.order_by(RFQHeader.rfq_date.desc()).offset(skip).limit(limit).all()
    return rfq_headers


@router.get("", response_model=List[RFQHeaderResponse])
async def get_rfq_headers(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    document_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get list of RFQ headers with optional filters
    """
    company_id = resolve_company_id(company_id)
    query = db.query(RFQHeader).options(
        joinedload(RFQHeader.lines),
        joinedload(RFQHeader.vendors)
    )

    if tenant_id:
        query = query.filter(RFQHeader.tenant_id == tenant_id)
    if company_id:
        query = query.filter(RFQHeader.company_id == company_id)
    if document_type:
        query = query.filter(RFQHeader.document_type == document_type)
    if status:
        query = query.filter(RFQHeader.status == status)

    rfq_headers = query.offset(skip).limit(limit).all()
    return rfq_headers


@router.get("/numbers", response_model=List[str])
async def get_rfq_numbers(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    document_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return distinct RFQ numbers from rfq_header.
    Optional filters: tenant_id, company_id, document_type, status.
    """
    company_id = resolve_company_id(company_id)
    query = db.query(RFQHeader.rfq_no).distinct()

    if tenant_id is not None:
        query = query.filter(RFQHeader.tenant_id == tenant_id)
    if company_id is not None:
        query = query.filter(RFQHeader.company_id == company_id)
    if document_type:
        query = query.filter(RFQHeader.document_type == document_type)
    if status:
        query = query.filter(RFQHeader.status == status)

    rows = query.order_by(RFQHeader.rfq_no.asc()).all()
    return [row.rfq_no for row in rows if row.rfq_no]


PENDING_RFQ_STATUS = "PENDING"


@router.get("/pending-count", response_model=PendingRFQCountResponse)
async def get_pending_rfq_count(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Count rows in rfq_header where status is PENDING.
    Optional filters: tenant_id, company_id (same semantics as GET /rfq).
    """
    company_id = resolve_company_id(company_id)
    q = db.query(func.count(RFQHeader.rfq_no)).filter(RFQHeader.status == PENDING_RFQ_STATUS)
    if tenant_id is not None:
        q = q.filter(RFQHeader.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(RFQHeader.company_id == company_id)
    pending_count = q.scalar() or 0
    return PendingRFQCountResponse(pending_count=pending_count)


SUBMITTED_RFQ_STATUS = "SUBMITTED"


@router.get("/submitted-count", response_model=SubmittedRFQCountResponse)
async def get_submitted_rfq_count(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Count rows in rfq_header where status is SUBMITTED.
    Optional filters: tenant_id, company_id (same semantics as GET /rfq).
    """
    company_id = resolve_company_id(company_id)
    q = db.query(func.count(RFQHeader.rfq_no)).filter(RFQHeader.status == SUBMITTED_RFQ_STATUS)
    if tenant_id is not None:
        q = q.filter(RFQHeader.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(RFQHeader.company_id == company_id)
    submitted_count = q.scalar() or 0
    return SubmittedRFQCountResponse(submitted_count=submitted_count)


@router.get(
    "/freezed-vendor-rates/{tenant_id}/{company_id}/{document_type}/item-wise",
    response_model=FreezedVendorRatesAllRFQsResponse,
)
async def list_freezed_vendor_rates_item_wise_legacy_path(
    tenant_id: int,
    company_id: int,
    document_type: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Backward-compatible alias for clients still calling the old path.
    Defined before generic composite-key routes to avoid route collisions.
    """
    company_id = resolve_company_id(company_id)
    return await list_freezed_vendor_rates_item_wise(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        db=db,
        current_user=current_user,
    )


def _all_rfq_headers_unscoped(
    db: Session,
    *,
    tenant_id: Optional[int],
    company_id: Optional[int],
    document_type: Optional[str],
    rfq_status: Optional[str],
    skip: int,
    limit: int,
) -> List[RFQHeader]:
    """All RFQ headers with optional filters (used for buyer role)."""
    company_id = resolve_company_id(company_id)
    q = db.query(RFQHeader).options(
        joinedload(RFQHeader.lines),
        joinedload(RFQHeader.vendors),
    )
    if tenant_id is not None:
        q = q.filter(RFQHeader.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(RFQHeader.company_id == company_id)
    if document_type:
        q = q.filter(RFQHeader.document_type == document_type)
    if rfq_status:
        q = q.filter(RFQHeader.status == rfq_status)
    return (
        q.order_by(RFQHeader.rfq_date.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/for-vendor", response_model=List[RFQHeaderResponse])
@router.get("/for-vendor/{vendor_id}", response_model=List[RFQHeaderResponse])
async def list_rfqs_where_vendor_is_invited(
    vendor_id: Optional[int] = None,
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    document_type: Optional[str] = Query(None),
    rfq_status: Optional[str] = Query(None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return RFQs. Optional `vendor_id` filters to invited vendor RFQs;
    without it, returns all RFQs (permissions API owns who can call this).
    """
    company_id = resolve_company_id(company_id)
    print(
        "[RFQ /for-vendor] request payload:",
        {
            "query": {
                "vendor_id": vendor_id,
                "tenant_id": tenant_id,
                "company_id": company_id,
                "document_type": document_type,
                "status": rfq_status,
                "skip": skip,
                "limit": limit,
            },
            "current_user": {
                "user_id": current_user.user_id,
                "role": current_user.role,
                "vendor_id": current_user.vendor_id,
            },
        },
    )

    # Default filters from token/login context when query params are omitted.
    if tenant_id is None:
        tenant_id = getattr(current_user, "token_tenant_id", None)
    if company_id is None:
        company_id = getattr(current_user, "token_company_id", None)

    # No role branching — permissions API owns access.
    if vendor_id is None:
        vendor_id = current_user.vendor_id

    if vendor_id is None:
        return _all_rfq_headers_unscoped(
            db,
            tenant_id=tenant_id,
            company_id=company_id,
            document_type=document_type,
            rfq_status=rfq_status,
            skip=skip,
            limit=limit,
        )

    if not document_type:
        document_type = "RFQ"

    effective_vendor_id = vendor_id

    # Join only the invited vendor row for this vendor_id, and populate
    # RFQHeader.vendors from that join (otherwise joinedload loads every invitee).
    q = (
        db.query(RFQHeader)
        .join(
            RFQVendor,
            and_(
                RFQHeader.tenant_id == RFQVendor.tenant_id,
                RFQHeader.company_id == RFQVendor.company_id,
                RFQHeader.document_type == RFQVendor.document_type,
                RFQHeader.rfq_no == RFQVendor.rfq_no,
                RFQHeader.version_no == RFQVendor.version_no,
                RFQVendor.vendor_id == effective_vendor_id,
            ),
        )
        .options(
            contains_eager(RFQHeader.vendors),
            joinedload(RFQHeader.lines),
        )
    )
    if tenant_id is not None:
        q = q.filter(RFQHeader.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(RFQHeader.company_id == company_id)
    if document_type:
        q = q.filter(RFQHeader.document_type == document_type)
    if rfq_status:
        q = q.filter(RFQHeader.status == rfq_status)

    return (
        q.order_by(RFQHeader.rfq_date.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/for-buyer")
async def list_all_rfqs_for_buyer(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    document_type: Optional[str] = Query(None),
    rfq_status: Optional[str] = Query(None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return all RFQs. Access is permissions-API driven.
    """
    company_id = resolve_company_id(company_id)

    if tenant_id is None:
        tenant_id = getattr(current_user, "token_tenant_id", None)
    if company_id is None:
        company_id = getattr(current_user, "token_company_id", None)

    return _all_rfq_headers_unscoped(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        rfq_status=rfq_status,
        skip=skip,
        limit=limit,
    )


def _rfq_workflow_state_row(db: Session, tenant_id: int, company_id: int, document_type: str, rfq_no: str, version_no: int):
    company_id = resolve_company_id(company_id)
    row = (
        db.query(RFQHeader, User.email)
        .outerjoin(User, User.user_id == RFQHeader.updated_by)
        .filter(
            RFQHeader.tenant_id == tenant_id,
            RFQHeader.company_id == company_id,
            RFQHeader.document_type == document_type,
            RFQHeader.rfq_no == rfq_no,
            RFQHeader.version_no == version_no,
        )
        .first()
    )
    return row


@router.get(
    "/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}/workflow-state",
    response_model=RFQWorkflowStateResponse,
)
async def get_rfq_workflow_state(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    row = _rfq_workflow_state_row(db, tenant_id, company_id, document_type, rfq_no, version_no)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RFQ header not found")
    h, editor_email = row
    return RFQWorkflowStateResponse(
        tenant_id=h.tenant_id,
        company_id=h.company_id,
        document_type=h.document_type,
        rfq_no=h.rfq_no,
        version_no=h.version_no,
        rfq_id=h.rfq_id,
        status=h.status,
        updated_by=h.updated_by,
        editor_email=editor_email,
        updated_at=h.updated_at,
    )


@router.get("/by-rfq-id/{rfq_id}/workflow-state", response_model=RFQWorkflowStateResponse)
async def get_rfq_workflow_state_by_rfq_id(
    rfq_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        db.query(RFQHeader, User.email)
        .outerjoin(User, User.user_id == RFQHeader.updated_by)
        .filter(RFQHeader.rfq_id == rfq_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RFQ header not found")
    h, editor_email = row
    return RFQWorkflowStateResponse(
        tenant_id=h.tenant_id,
        company_id=h.company_id,
        document_type=h.document_type,
        rfq_no=h.rfq_no,
        version_no=h.version_no,
        rfq_id=h.rfq_id,
        status=h.status,
        updated_by=h.updated_by,
        editor_email=editor_email,
        updated_at=h.updated_at,
    )


@router.get("/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}", response_model=RFQHeaderResponse)
async def get_rfq_header(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get a specific RFQ header by composite primary key
    """
    company_id = resolve_company_id(company_id)
    rfq_header = db.query(RFQHeader).options(
        joinedload(RFQHeader.lines),
        joinedload(RFQHeader.vendors)
    ).filter(
        RFQHeader.tenant_id == tenant_id,
        RFQHeader.company_id == company_id,
        RFQHeader.document_type == document_type,
        RFQHeader.rfq_no == rfq_no,
        RFQHeader.version_no == version_no
    ).first()

    if not rfq_header:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ header not found"
        )

    return rfq_header


@router.get("/by-rfq-id/{rfq_id}", response_model=RFQHeaderResponse)
async def get_rfq_header_by_id(
    rfq_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get a specific RFQ header by rfq_id (unique identifier)
    """
    rfq_header = db.query(RFQHeader).options(
        joinedload(RFQHeader.lines),
        joinedload(RFQHeader.vendors)
    ).filter(RFQHeader.rfq_id == rfq_id).first()

    if not rfq_header:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ header not found"
        )

    return rfq_header


@router.post("", response_model=RFQHeaderResponse, status_code=status.HTTP_201_CREATED)
async def create_rfq_header(
    rfq_data: RFQHeaderCreate,
    db: Session = Depends(get_db),
):
    """
    Create a new RFQ header with lines and vendors.
    No authentication required - created_by must be provided in the payload.
    rfq_no is obtained from NestJS next-numbering (POST .../RFQ/next); client rfq_no is ignored.
    """
    import logging
    logger = logging.getLogger(__name__)
    rfq_data.company_id = resolve_company_id(rfq_data.company_id)
    
    resolved_rfq_no = None
    try:
        # Validate created_by is provided
        if not rfq_data.created_by:
            error_msg = "created_by is required"
            logger.error(error_msg)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )

        created_by = rfq_data.created_by
        logger.info(f"Using created_by: {created_by}")

        try:
            resolved_rfq_no = get_next_rfq_number_from_nest(
                rfq_data.tenant_id,
                rfq_data.company_id,
                rfq_data.department_id,
            )
        except RuntimeError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(e),
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(e),
            )

        logger.info(f"Creating RFQ: {resolved_rfq_no}, version: {rfq_data.version_no}")

        # Check if RFQ already exists
        existing = db.query(RFQHeader).filter(
            RFQHeader.tenant_id == rfq_data.tenant_id,
            RFQHeader.company_id == rfq_data.company_id,
            RFQHeader.document_type == rfq_data.document_type,
            RFQHeader.rfq_no == resolved_rfq_no,
            RFQHeader.version_no == rfq_data.version_no
        ).first()

        if existing:
            error_msg = f"RFQ header with this composite key already exists: tenant_id={rfq_data.tenant_id}, company_id={rfq_data.company_id}, document_type={rfq_data.document_type}, rfq_no={resolved_rfq_no}, version_no={rfq_data.version_no}. Use a different rfq_no or increment version_no."
            logger.warning(error_msg)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )

        # Generate unique rfq_id if not provided
        # Get the maximum existing rfq_id and add 1
        max_rfq_id = db.query(func.max(RFQHeader.rfq_id)).scalar()
        next_rfq_id = (max_rfq_id or 0) + 1
        logger.info(f"Generated rfq_id: {next_rfq_id}")

        # Create RFQ header
        rfq_dict = rfq_data.dict(exclude={"lines", "vendors", "created_by"})
        rfq_dict["rfq_no"] = resolved_rfq_no
        rfq_dict["created_by"] = created_by
        rfq_dict["rfq_id"] = next_rfq_id  # Set the generated rfq_id
        rfq_header = RFQHeader(**rfq_dict)
        
        db.add(rfq_header)
        db.flush()  # Flush to get rfq_id if needed

        # Create lines
        if rfq_data.lines:
            logger.info(f"Creating {len(rfq_data.lines)} lines")
            for idx, line_data in enumerate(rfq_data.lines):
                try:
                    line_dict = line_data.dict(exclude={"created_by"})
                    line_dict.update({
                        "tenant_id": rfq_data.tenant_id,
                        "company_id": rfq_data.company_id,
                        "document_type": rfq_data.document_type,
                        "rfq_no": resolved_rfq_no,
                        "version_no": rfq_data.version_no,
                        "created_by": line_data.created_by if line_data.created_by else created_by,
                    })
                    logger.debug(f"Line {idx+1} dict: {line_dict}")
                    rfq_line = RFQLine(**line_dict)
                    db.add(rfq_line)
                except Exception as line_error:
                    logger.error(f"Error creating line {idx+1}: {str(line_error)}")
                    raise

        # Create vendors
        if rfq_data.vendors:
            logger.info(f"Creating {len(rfq_data.vendors)} vendors")
            for idx, vendor_data in enumerate(rfq_data.vendors):
                try:
                    vendor_dict = vendor_data.dict(exclude={"created_by"}, exclude_none=True)
                    vendor_dict.update({
                        "tenant_id": rfq_data.tenant_id,
                        "company_id": rfq_data.company_id,
                        "document_type": rfq_data.document_type,
                        "rfq_no": resolved_rfq_no,
                        "version_no": rfq_data.version_no,
                        "created_by": vendor_data.created_by if vendor_data.created_by else created_by,
                    })
                    if not vendor_dict.get("status"):
                        vendor_dict["status"] = "DRAFT"
                    if not vendor_dict.get("current_editor"):
                        vendor_dict["current_editor"] = "BUYER"
                    logger.debug(f"Vendor {idx+1} dict: {vendor_dict}")
                    rfq_vendor = RFQVendor(**vendor_dict)
                    db.add(rfq_vendor)
                except Exception as vendor_error:
                    logger.error(f"Error creating vendor {idx+1}: {str(vendor_error)}")
                    raise

        db.commit()
        db.refresh(rfq_header)

        # Reload with relationships
        rfq_header = db.query(RFQHeader).options(
            joinedload(RFQHeader.lines),
            joinedload(RFQHeader.vendors)
        ).filter(
            RFQHeader.tenant_id == rfq_data.tenant_id,
            RFQHeader.company_id == rfq_data.company_id,
            RFQHeader.document_type == rfq_data.document_type,
            RFQHeader.rfq_no == resolved_rfq_no,
            RFQHeader.version_no == rfq_data.version_no
        ).first()

        return rfq_header

    except IntegrityError as e:
        db.rollback()
        import logging
        logger = logging.getLogger(__name__)
        error_str = str(e.orig) if hasattr(e, 'orig') else str(e)
        logger.error(f"IntegrityError: {error_str}")
        logger.error(f"Full error: {e}")
        
        # Check for duplicate key violations
        if "duplicate key" in error_str.lower() or "unique" in error_str.lower():
            _no = resolved_rfq_no if resolved_rfq_no is not None else getattr(rfq_data, "rfq_no", None)
            error_msg = f"RFQ with this composite key already exists: tenant_id={rfq_data.tenant_id}, company_id={rfq_data.company_id}, document_type={rfq_data.document_type}, rfq_no={_no}, version_no={rfq_data.version_no}. Use a different rfq_no or increment version_no."
            logger.error(error_msg)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )
        # Check for foreign key violations
        if "foreign key" in error_str.lower() or "violates foreign key" in error_str.lower() or "fk_" in error_str.lower():
            error_msg = f"Foreign key constraint violation. Please verify that requester_id={rfq_data.requester_id}, department_id={rfq_data.department_id}, item_ids in lines, and vendor_id in vendors exist in the database. Error details: {error_str}"
            logger.error(error_msg)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )
        error_msg = f"Database constraint violation: {error_str}"
        logger.error(error_msg)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg
        )
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        db.rollback()
        import logging
        import traceback
        logger = logging.getLogger(__name__)
        error_detail = f"Error creating RFQ header: {str(e)}"
        logger.error(error_detail)
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_detail
        )


@router.post("/complete", response_model=RFQHeaderResponse, status_code=status.HTTP_201_CREATED)
async def create_complete_rfq(
    rfq_data: RFQHeaderCreate,
    db: Session = Depends(get_db),
):
    """
    Create a complete RFQ with header, lines, and vendors in a single transaction.
    This is an alias for the main create endpoint but makes it explicit that it creates everything.
    
    Creates:
    - RFQ Header
    - RFQ Lines (if provided)
    - RFQ Vendors (if provided)
    
    All in a single atomic transaction.
    No authentication required - created_by must be provided in the payload.
    """
    # Delegate to the main create endpoint
    return await create_rfq_header(rfq_data, db)


@router.put("/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}", response_model=RFQHeaderResponse)
@router.patch("/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}", response_model=RFQHeaderResponse)
async def update_rfq_header(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    rfq_update: RFQHeaderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update an existing RFQ header
    """
    company_id = resolve_company_id(company_id)
    rfq_header = db.query(RFQHeader).filter(
        RFQHeader.tenant_id == tenant_id,
        RFQHeader.company_id == company_id,
        RFQHeader.document_type == document_type,
        RFQHeader.rfq_no == rfq_no,
        RFQHeader.version_no == version_no
    ).first()

    if not rfq_header:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ header not found"
        )

    try:
        # Update fields
        update_data = rfq_update.dict(exclude_unset=True)
        for key, value in update_data.items():
            if hasattr(rfq_header, key):
                setattr(rfq_header, key, value)

        # Set updated_by if provided, otherwise use current user
        if rfq_update.updated_by:
            rfq_header.updated_by = rfq_update.updated_by
        else:
            rfq_header.updated_by = current_user.user_id

        rfq_header.updated_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(rfq_header)

        # Reload with relationships
        rfq_header = db.query(RFQHeader).options(
            joinedload(RFQHeader.lines),
            joinedload(RFQHeader.vendors)
        ).filter(
            RFQHeader.tenant_id == tenant_id,
            RFQHeader.company_id == company_id,
            RFQHeader.document_type == document_type,
            RFQHeader.rfq_no == rfq_no,
            RFQHeader.version_no == version_no
        ).first()

        return rfq_header

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating RFQ header: {str(e)}"
        )


@router.delete("/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rfq_header(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Delete an RFQ header (cascade will delete related lines and vendors)
    """
    company_id = resolve_company_id(company_id)
    rfq_header = db.query(RFQHeader).filter(
        RFQHeader.tenant_id == tenant_id,
        RFQHeader.company_id == company_id,
        RFQHeader.document_type == document_type,
        RFQHeader.rfq_no == rfq_no,
        RFQHeader.version_no == version_no
    ).first()

    if not rfq_header:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ header not found"
        )

    try:
        db.delete(rfq_header)
        db.commit()
        return None

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting RFQ header: {str(e)}"
        )


# ============================================================================
# RFQ LINE CRUD OPERATIONS
# ============================================================================

@router.get("/lines", response_model=List[RFQLineResponse])
async def get_rfq_lines(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    document_type: Optional[str] = Query(None),
    rfq_no: Optional[str] = Query(None),
    version_no: Optional[int] = Query(None),
    item_id: Optional[int] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get list of RFQ lines with optional filters
    """
    company_id = resolve_company_id(company_id)
    query = db.query(RFQLine)

    if tenant_id:
        query = query.filter(RFQLine.tenant_id == tenant_id)
    if company_id:
        query = query.filter(RFQLine.company_id == company_id)
    if document_type:
        query = query.filter(RFQLine.document_type == document_type)
    if rfq_no:
        query = query.filter(RFQLine.rfq_no == rfq_no)
    if version_no:
        query = query.filter(RFQLine.version_no == version_no)
    if item_id:
        query = query.filter(RFQLine.item_id == item_id)

    rfq_lines = query.order_by(RFQLine.line_no).offset(skip).limit(limit).all()
    return rfq_lines


@router.get("/lines/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}", response_model=List[RFQLineResponse])
async def get_rfq_lines_by_header(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get all lines for a specific RFQ header
    """
    # Verify RFQ header exists
    company_id = resolve_company_id(company_id)
    rfq_header = db.query(RFQHeader).filter(
        RFQHeader.tenant_id == tenant_id,
        RFQHeader.company_id == company_id,
        RFQHeader.document_type == document_type,
        RFQHeader.rfq_no == rfq_no,
        RFQHeader.version_no == version_no
    ).first()

    if not rfq_header:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ header not found"
        )

    rfq_lines = db.query(RFQLine).filter(
        RFQLine.tenant_id == tenant_id,
        RFQLine.company_id == company_id,
        RFQLine.document_type == document_type,
        RFQLine.rfq_no == rfq_no,
        RFQLine.version_no == version_no
    ).order_by(RFQLine.line_no).all()

    return rfq_lines


@router.get("/lines/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}/{line_no}", response_model=RFQLineResponse)
async def get_rfq_line(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    line_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get a specific RFQ line by composite primary key
    """
    company_id = resolve_company_id(company_id)
    rfq_line = db.query(RFQLine).filter(
        RFQLine.tenant_id == tenant_id,
        RFQLine.company_id == company_id,
        RFQLine.document_type == document_type,
        RFQLine.rfq_no == rfq_no,
        RFQLine.version_no == version_no,
        RFQLine.line_no == line_no
    ).first()

    if not rfq_line:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ line not found"
        )

    return rfq_line


@router.post("/lines/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}", response_model=RFQLineResponse, status_code=status.HTTP_201_CREATED)
async def create_rfq_line(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    line_data: RFQLineCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create a new RFQ line for a specific RFQ header
    """
    # Verify RFQ header exists
    company_id = resolve_company_id(company_id)
    rfq_header = db.query(RFQHeader).filter(
        RFQHeader.tenant_id == tenant_id,
        RFQHeader.company_id == company_id,
        RFQHeader.document_type == document_type,
        RFQHeader.rfq_no == rfq_no,
        RFQHeader.version_no == version_no
    ).first()

    if not rfq_header:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ header not found"
        )

    try:
        # Check if line already exists
        existing = db.query(RFQLine).filter(
            RFQLine.tenant_id == tenant_id,
            RFQLine.company_id == company_id,
            RFQLine.document_type == document_type,
            RFQLine.rfq_no == rfq_no,
            RFQLine.version_no == version_no,
            RFQLine.line_no == line_data.line_no
        ).first()

        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="RFQ line with this line_no already exists"
            )

        # Create RFQ line
        line_dict = line_data.dict()
        line_dict.update({
            "tenant_id": tenant_id,
            "company_id": company_id,
            "document_type": document_type,
            "rfq_no": rfq_no,
            "version_no": version_no,
        })
        # Use provided created_by or default to current user
        if not line_dict.get("created_by"):
            line_dict["created_by"] = current_user.user_id
        rfq_line = RFQLine(**line_dict)
        
        db.add(rfq_line)
        db.commit()
        db.refresh(rfq_line)

        return rfq_line

    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database constraint violation: {str(e)}"
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating RFQ line: {str(e)}"
        )


@router.post("/lines/bulk", response_model=List[RFQLineResponse], status_code=status.HTTP_201_CREATED)
async def create_rfq_lines_bulk(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    lines_data: List[RFQLineCreate],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create multiple RFQ lines at once
    """
    # Verify RFQ header exists
    company_id = resolve_company_id(company_id)
    rfq_header = db.query(RFQHeader).filter(
        RFQHeader.tenant_id == tenant_id,
        RFQHeader.company_id == company_id,
        RFQHeader.document_type == document_type,
        RFQHeader.rfq_no == rfq_no,
        RFQHeader.version_no == version_no
    ).first()

    if not rfq_header:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ header not found"
        )

    try:
        created_lines = []
        for line_data in lines_data:
            # Check if line already exists
            existing = db.query(RFQLine).filter(
                RFQLine.tenant_id == tenant_id,
                RFQLine.company_id == company_id,
                RFQLine.document_type == document_type,
                RFQLine.rfq_no == rfq_no,
                RFQLine.version_no == version_no,
                RFQLine.line_no == line_data.line_no
            ).first()

            if existing:
                continue  # Skip existing lines

            line_dict = line_data.dict()
            line_dict.update({
                "tenant_id": tenant_id,
                "company_id": company_id,
                "document_type": document_type,
                "rfq_no": rfq_no,
                "version_no": version_no,
            })
            # Use provided created_by or default to current user
            if not line_dict.get("created_by"):
                line_dict["created_by"] = current_user.user_id
            rfq_line = RFQLine(**line_dict)
            db.add(rfq_line)
            created_lines.append(rfq_line)

        db.commit()
        for line in created_lines:
            db.refresh(line)

        return created_lines

    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database constraint violation: {str(e)}"
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating RFQ lines: {str(e)}"
        )


@router.put("/lines/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}/{line_no}", response_model=RFQLineResponse)
@router.patch("/lines/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}/{line_no}", response_model=RFQLineResponse)
async def update_rfq_line(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    line_no: int,
    line_update: RFQLineUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update an existing RFQ line
    """
    company_id = resolve_company_id(company_id)
    rfq_line = db.query(RFQLine).filter(
        RFQLine.tenant_id == tenant_id,
        RFQLine.company_id == company_id,
        RFQLine.document_type == document_type,
        RFQLine.rfq_no == rfq_no,
        RFQLine.version_no == version_no,
        RFQLine.line_no == line_no
    ).first()

    if not rfq_line:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ line not found"
        )

    try:
        # Update fields
        update_data = line_update.dict(exclude_unset=True)
        for key, value in update_data.items():
            if hasattr(rfq_line, key) and key != "updated_by":
                setattr(rfq_line, key, value)

        # Set updated_by if provided, otherwise use current user
        if line_update.updated_by:
            rfq_line.updated_by = line_update.updated_by
        else:
            rfq_line.updated_by = current_user.user_id

        rfq_line.updated_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(rfq_line)

        return rfq_line

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating RFQ line: {str(e)}"
        )


@router.delete("/lines/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}/{line_no}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rfq_line(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    line_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Delete an RFQ line
    """
    company_id = resolve_company_id(company_id)
    rfq_line = db.query(RFQLine).filter(
        RFQLine.tenant_id == tenant_id,
        RFQLine.company_id == company_id,
        RFQLine.document_type == document_type,
        RFQLine.rfq_no == rfq_no,
        RFQLine.version_no == version_no,
        RFQLine.line_no == line_no
    ).first()

    if not rfq_line:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ line not found"
        )

    try:
        db.delete(rfq_line)
        db.commit()
        return None

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting RFQ line: {str(e)}"
        )


# ============================================================================
# RFQ VENDOR CRUD OPERATIONS
# ============================================================================

@router.get("/vendors", response_model=List[RFQVendorResponse])
async def get_rfq_vendors(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    document_type: Optional[str] = Query(None),
    rfq_no: Optional[str] = Query(None),
    version_no: Optional[int] = Query(None),
    vendor_id: Optional[int] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get list of RFQ vendors with optional filters
    """
    company_id = resolve_company_id(company_id)
    query = db.query(RFQVendor)

    if tenant_id:
        query = query.filter(RFQVendor.tenant_id == tenant_id)
    if company_id:
        query = query.filter(RFQVendor.company_id == company_id)
    if document_type:
        query = query.filter(RFQVendor.document_type == document_type)
    if rfq_no:
        query = query.filter(RFQVendor.rfq_no == rfq_no)
    if version_no:
        query = query.filter(RFQVendor.version_no == version_no)
    if vendor_id:
        query = query.filter(RFQVendor.vendor_id == vendor_id)

    rfq_vendors = query.order_by(RFQVendor.vendor_id).offset(skip).limit(limit).all()
    return rfq_vendors


@router.get("/vendors/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}", response_model=List[RFQVendorResponse])
async def get_rfq_vendors_by_header(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get all vendors for a specific RFQ header
    """
    # Verify RFQ header exists
    company_id = resolve_company_id(company_id)
    rfq_header = db.query(RFQHeader).filter(
        RFQHeader.tenant_id == tenant_id,
        RFQHeader.company_id == company_id,
        RFQHeader.document_type == document_type,
        RFQHeader.rfq_no == rfq_no,
        RFQHeader.version_no == version_no
    ).first()

    if not rfq_header:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ header not found"
        )

    rfq_vendors = db.query(RFQVendor).filter(
        RFQVendor.tenant_id == tenant_id,
        RFQVendor.company_id == company_id,
        RFQVendor.document_type == document_type,
        RFQVendor.rfq_no == rfq_no,
        RFQVendor.version_no == version_no
    ).order_by(RFQVendor.vendor_id).all()

    return rfq_vendors


@router.get("/vendors/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}/{vendor_id}", response_model=RFQVendorResponse)
async def get_rfq_vendor(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get a specific RFQ vendor by composite primary key
    """
    company_id = resolve_company_id(company_id)
    rfq_vendor = db.query(RFQVendor).filter(
        RFQVendor.tenant_id == tenant_id,
        RFQVendor.company_id == company_id,
        RFQVendor.document_type == document_type,
        RFQVendor.rfq_no == rfq_no,
        RFQVendor.version_no == version_no,
        RFQVendor.vendor_id == vendor_id
    ).first()

    if not rfq_vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ vendor not found"
        )

    return rfq_vendor


@router.get(
    "/vendors/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}/{vendor_id}/workflow-state",
    response_model=RFQVendorWorkflowStateResponse,
)
async def get_rfq_vendor_workflow_state(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    rfq_vendor = (
        db.query(RFQVendor)
        .filter(
            RFQVendor.tenant_id == tenant_id,
            RFQVendor.company_id == company_id,
            RFQVendor.document_type == document_type,
            RFQVendor.rfq_no == rfq_no,
            RFQVendor.version_no == version_no,
            RFQVendor.vendor_id == vendor_id,
        )
        .first()
    )

    if not rfq_vendor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RFQ vendor not found")

    lines = (
        db.query(RFQLine)
        .filter(
            RFQLine.tenant_id == tenant_id,
            RFQLine.company_id == company_id,
            RFQLine.document_type == document_type,
            RFQLine.rfq_no == rfq_no,
            RFQLine.version_no == version_no,
        )
        .order_by(RFQLine.line_no.asc())
        .all()
    )

    item_code_cache = {}
    matched_item_rates: List[RFQVendorMatchedItemRate] = []
    for line in lines:
        if line.item_id is None:
            matched_item_rates.append(
                RFQVendorMatchedItemRate(item_id=None, erp_code=None, vendor_item_id=None, rate=None, currency=None)
            )
            continue

        item_id = int(line.item_id)
        if item_id not in item_code_cache:
            item_code_cache[item_id] = _get_item_code_from_nest(item_id)
        erp_code_from_nest = item_code_cache[item_id]

        vendor_item, erp_code = _resolve_vendor_item_for_rfq_line(
            db, vendor_id, item_id, erp_code_from_nest
        )

        best_rate = None
        if vendor_item:
            best_rate = (
                db.query(VendorRate)
                .filter(
                    VendorRate.item_id == vendor_item.id,
                )
                .order_by(
                    VendorRate.valid_from.desc().nullslast(),
                    VendorRate.created_at.desc().nullslast(),
                    VendorRate.id.desc(),
                )
                .first()
            )

        matched_item_rates.append(
            RFQVendorMatchedItemRate(
                item_id=item_id,
                erp_code=erp_code,
                vendor_item_id=(vendor_item.id if vendor_item else None),
                rate=(best_rate.rate if best_rate else None),
                currency=(best_rate.currency if best_rate else None),
            )
        )

    return RFQVendorWorkflowStateResponse(
        tenant_id=rfq_vendor.tenant_id,
        company_id=rfq_vendor.company_id,
        document_type=rfq_vendor.document_type,
        rfq_no=rfq_vendor.rfq_no,
        version_no=rfq_vendor.version_no,
        vendor_id=rfq_vendor.vendor_id,
        status=rfq_vendor.status,
        current_editor=rfq_vendor.current_editor,
        updated_by=rfq_vendor.updated_by,
        updated_at=rfq_vendor.updated_at,
        matched_item_rates=matched_item_rates,
    )


@router.patch("/vendors/workflow", response_model=RFQVendorResponse)
async def patch_rfq_vendor_workflow_by_vendor_and_rfq_no(
    body: RFQVendorWorkflowPatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update `status` and/or `current_editor` for the `rfq_vendor` row identified by `vendor_id` and `rfq_no`.

    If several rows share the same `vendor_id` + `rfq_no` (e.g. different tenants or versions), include
    `tenant_id`, `company_id`, `document_type`, and optionally `version_no` in the body to select exactly one row.
    """
    if body.status is None and body.current_editor is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one of status or current_editor",
        )

    q = db.query(RFQVendor).filter(
        RFQVendor.vendor_id == body.vendor_id,
        RFQVendor.rfq_no == body.rfq_no,
    )
    if body.tenant_id is not None:
        q = q.filter(RFQVendor.tenant_id == body.tenant_id)
    q = q.filter(RFQVendor.company_id == resolve_company_id(body.company_id))
    if body.document_type is not None:
        q = q.filter(RFQVendor.document_type == body.document_type)
    if body.version_no is not None:
        q = q.filter(RFQVendor.version_no == body.version_no)

    matches = q.all()
    if not matches:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ vendor not found for the given vendor_id and rfq_no (and optional composite keys)",
        )
    if len(matches) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Multiple rfq_vendor rows match vendor_id and rfq_no. "
                "Include tenant_id, company_id, document_type, and optionally version_no in the request body."
            ),
        )

    rfq_vendor = matches[0]
    try:
        if body.status is not None:
            rfq_vendor.status = body.status
        if body.current_editor is not None:
            rfq_vendor.current_editor = body.current_editor
        if body.updated_by is not None:
            rfq_vendor.updated_by = body.updated_by
        else:
            rfq_vendor.updated_by = current_user.user_id
        rfq_vendor.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(rfq_vendor)
        return rfq_vendor
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating RFQ vendor workflow: {str(e)}",
        )


@router.get(
    "/reports/freezed-vendor-rates/{tenant_id}/{company_id}/{document_type}/item-wise",
    response_model=FreezedVendorRatesAllRFQsResponse,
)
async def list_freezed_vendor_rates_item_wise(
    tenant_id: int,
    company_id: int,
    document_type: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    freezed_vendor_rows = (
        db.query(RFQVendor.rfq_no, RFQVendor.version_no, RFQVendor.vendor_id)
        .filter(
            RFQVendor.tenant_id == tenant_id,
            RFQVendor.company_id == company_id,
            RFQVendor.document_type == document_type,
            RFQVendor.status == "FREEZED",
        )
        .order_by(RFQVendor.rfq_no.asc(), RFQVendor.version_no.asc(), RFQVendor.vendor_id.asc())
        .all()
    )
    rfq_vendor_map = {}
    for row in freezed_vendor_rows:
        key = (row.rfq_no, int(row.version_no))
        rfq_vendor_map.setdefault(key, []).append(int(row.vendor_id))

    item_code_cache: dict[int, Optional[str]] = {}
    rfqs_out: List[FreezedRFQItemWiseResponse] = []
    for (rfq_no, version_no), vendor_ids in rfq_vendor_map.items():
        lines = (
            db.query(RFQLine)
            .filter(
                RFQLine.tenant_id == tenant_id,
                RFQLine.company_id == company_id,
                RFQLine.document_type == document_type,
                RFQLine.rfq_no == rfq_no,
                RFQLine.version_no == version_no,
            )
            .order_by(RFQLine.line_no.asc())
            .all()
        )

        items_out: List[FreezedVendorRateItem] = []
        for line in lines:
            vendors_out: List[FreezedVendorRateItemVendor] = []
            for vid in vendor_ids:
                rate_val = None
                currency_val = None
                if line.item_id is not None:
                    best_rate, _ = _best_vendor_rate_for_rfq_line(
                        db, int(vid), int(line.item_id), item_code_cache
                    )
                    if best_rate:
                        rate_val = best_rate.rate
                        currency_val = best_rate.currency
                vendors_out.append(
                    FreezedVendorRateItemVendor(
                        vendor_id=int(vid),
                        rate=rate_val,
                        currency=currency_val,
                    )
                )

            items_out.append(
                FreezedVendorRateItem(
                    item_id=line.item_id,
                    item_description=line.item_description,
                    uom=line.uom,
                    rfq_line_rate=line.estimated_amount,
                    vendors=vendors_out,
                )
            )

        rfqs_out.append(
            FreezedRFQItemWiseResponse(
                tenant_id=tenant_id,
                company_id=company_id,
                document_type=document_type,
                rfq_no=rfq_no,
                version_no=version_no,
                items=items_out,
            )
        )

    return FreezedVendorRatesAllRFQsResponse(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        rfqs=rfqs_out,
    )


@router.post(
    "/generate-pcd/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}",
    response_model=RFQHeaderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_pcd(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Snapshot a source RFQ into a new header version: inserts rfq_header / rfq_line /
    rfq_vendor using the same path `document_type` (e.g. 'RFQ') and `rfq_no`, with
    `version_no` set to (source version + 1) so the composite primary key stays unique.

    The line/vendor content mirrors what
    GET /rfq/freezed-vendor-rates/{tenant_id}/{company_id}/{document_type}/item-wise
    returns for this RFQ:
      * one rfq_line per source line (item_id, uom, quantity, estimated_rate/amount, etc.)
      * one rfq_vendor per FREEZED vendor on the source RFQ.
    """
    company_id = resolve_company_id(company_id)
    source_header = (
        db.query(RFQHeader)
        .filter(
            RFQHeader.tenant_id == tenant_id,
            RFQHeader.company_id == company_id,
            RFQHeader.document_type == document_type,
            RFQHeader.rfq_no == rfq_no,
            RFQHeader.version_no == version_no,
        )
        .first()
    )
    if not source_header:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Source RFQ not found: tenant_id={tenant_id}, company_id={company_id}, "
                f"document_type={document_type}, rfq_no={rfq_no}, version_no={version_no}"
            ),
        )

    target_version_no = version_no + 1
    existing_snapshot = (
        db.query(RFQHeader)
        .filter(
            RFQHeader.tenant_id == tenant_id,
            RFQHeader.company_id == company_id,
            RFQHeader.document_type == document_type,
            RFQHeader.rfq_no == rfq_no,
            RFQHeader.version_no == target_version_no,
        )
        .first()
    )
    if existing_snapshot:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"RFQ snapshot version already exists for rfq_no={rfq_no}, "
                f"version_no={target_version_no} "
                f"(tenant_id={tenant_id}, company_id={company_id}, document_type={document_type})."
            ),
        )

    freezed_vendor_rows = (
        db.query(RFQVendor.vendor_id)
        .filter(
            RFQVendor.tenant_id == tenant_id,
            RFQVendor.company_id == company_id,
            RFQVendor.document_type == document_type,
            RFQVendor.rfq_no == rfq_no,
            RFQVendor.version_no == version_no,
            RFQVendor.status == "FREEZED",
        )
        .order_by(RFQVendor.vendor_id.asc())
        .all()
    )
    vendor_ids = sorted({int(r.vendor_id) for r in freezed_vendor_rows})
    if not vendor_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No FREEZED vendor responses found for this RFQ; snapshot cannot be generated.",
        )

    source_lines = (
        db.query(RFQLine)
        .filter(
            RFQLine.tenant_id == tenant_id,
            RFQLine.company_id == company_id,
            RFQLine.document_type == document_type,
            RFQLine.rfq_no == rfq_no,
            RFQLine.version_no == version_no,
        )
        .order_by(RFQLine.line_no.asc())
        .all()
    )
    if not source_lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source RFQ has no lines; snapshot cannot be generated.",
        )

    max_rfq_id = db.query(func.max(RFQHeader.rfq_id)).scalar()
    next_rfq_id = (max_rfq_id or 0) + 1

    hce = getattr(source_header, "current_editor", None)
    if hce is None or (isinstance(hce, str) and not hce.strip()):
        hce = "BUYER"
    elif isinstance(hce, str):
        hce = hce.strip()

    snapshot_header = RFQHeader(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        rfq_no=rfq_no,
        version_no=target_version_no,
        rfq_id=next_rfq_id,
        rfq_date=source_header.rfq_date,
        department_id=source_header.department_id,
        requester_id=source_header.requester_id,
        status="DRAFT",
        priority=source_header.priority,
        source_type=source_header.source_type,
        remarks=source_header.remarks,
        document_number=source_header.document_number,
        current_editor=hce,
        created_by=current_user.user_id,
    )

    try:
        db.add(snapshot_header)
        db.flush()

        for src_line in source_lines:
            db.add(
                RFQLine(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    document_type=document_type,
                    rfq_no=rfq_no,
                    version_no=target_version_no,
                    line_no=src_line.line_no,
                    item_id=src_line.item_id,
                    item_description=src_line.item_description,
                    uom=src_line.uom,
                    quantity=src_line.quantity,
                    estimated_rate=src_line.estimated_rate,
                    estimated_amount=src_line.estimated_amount,
                    required_date=src_line.required_date,
                    remarks=src_line.remarks,
                    created_by=current_user.user_id,
                )
            )

        for vid in vendor_ids:
            db.add(
                RFQVendor(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    document_type=document_type,
                    rfq_no=rfq_no,
                    version_no=target_version_no,
                    vendor_id=vid,
                    status="DRAFT",
                    current_editor="BUYER",
                    created_by=current_user.user_id,
                )
            )

        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database constraint violation while generating RFQ snapshot: {str(e)}",
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating RFQ snapshot: {str(e)}",
        )

    new_snapshot = (
        db.query(RFQHeader)
        .options(joinedload(RFQHeader.lines), joinedload(RFQHeader.vendors))
        .filter(
            RFQHeader.tenant_id == tenant_id,
            RFQHeader.company_id == company_id,
            RFQHeader.document_type == document_type,
            RFQHeader.rfq_no == rfq_no,
            RFQHeader.version_no == target_version_no,
        )
        .first()
    )
    return new_snapshot


@router.post("/vendors/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}", response_model=RFQVendorResponse, status_code=status.HTTP_201_CREATED)
async def create_rfq_vendor(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    vendor_data: RFQVendorCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create a new RFQ vendor for a specific RFQ header
    """
    # Verify RFQ header exists
    company_id = resolve_company_id(company_id)
    rfq_header = db.query(RFQHeader).filter(
        RFQHeader.tenant_id == tenant_id,
        RFQHeader.company_id == company_id,
        RFQHeader.document_type == document_type,
        RFQHeader.rfq_no == rfq_no,
        RFQHeader.version_no == version_no
    ).first()

    if not rfq_header:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ header not found"
        )

    try:
        # Check if vendor already exists
        existing = db.query(RFQVendor).filter(
            RFQVendor.tenant_id == tenant_id,
            RFQVendor.company_id == company_id,
            RFQVendor.document_type == document_type,
            RFQVendor.rfq_no == rfq_no,
            RFQVendor.version_no == version_no,
            RFQVendor.vendor_id == vendor_data.vendor_id
        ).first()

        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="RFQ vendor with this vendor_id already exists"
            )

        # Create RFQ vendor
        vendor_dict = vendor_data.dict(exclude_none=True)
        vendor_dict.update({
            "tenant_id": tenant_id,
            "company_id": company_id,
            "document_type": document_type,
            "rfq_no": rfq_no,
            "version_no": version_no,
        })
        if not vendor_dict.get("status"):
            vendor_dict["status"] = "DRAFT"
        if not vendor_dict.get("current_editor"):
            vendor_dict["current_editor"] = "BUYER"
        # Use provided created_by or default to current user
        if not vendor_dict.get("created_by"):
            vendor_dict["created_by"] = current_user.user_id
        rfq_vendor = RFQVendor(**vendor_dict)
        
        db.add(rfq_vendor)
        db.commit()
        db.refresh(rfq_vendor)

        return rfq_vendor

    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database constraint violation: {str(e)}"
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating RFQ vendor: {str(e)}"
        )


@router.post("/vendors/bulk", response_model=List[RFQVendorResponse], status_code=status.HTTP_201_CREATED)
async def create_rfq_vendors_bulk(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    vendors_data: List[RFQVendorCreate],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create multiple RFQ vendors at once
    """
    # Verify RFQ header exists
    company_id = resolve_company_id(company_id)
    rfq_header = db.query(RFQHeader).filter(
        RFQHeader.tenant_id == tenant_id,
        RFQHeader.company_id == company_id,
        RFQHeader.document_type == document_type,
        RFQHeader.rfq_no == rfq_no,
        RFQHeader.version_no == version_no
    ).first()

    if not rfq_header:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ header not found"
        )

    try:
        created_vendors = []
        for vendor_data in vendors_data:
            # Check if vendor already exists
            existing = db.query(RFQVendor).filter(
                RFQVendor.tenant_id == tenant_id,
                RFQVendor.company_id == company_id,
                RFQVendor.document_type == document_type,
                RFQVendor.rfq_no == rfq_no,
                RFQVendor.version_no == version_no,
                RFQVendor.vendor_id == vendor_data.vendor_id
            ).first()

            if existing:
                continue  # Skip existing vendors

            vendor_dict = vendor_data.dict(exclude_none=True)
            vendor_dict.update({
                "tenant_id": tenant_id,
                "company_id": company_id,
                "document_type": document_type,
                "rfq_no": rfq_no,
                "version_no": version_no,
            })
            if not vendor_dict.get("status"):
                vendor_dict["status"] = "DRAFT"
            if not vendor_dict.get("current_editor"):
                vendor_dict["current_editor"] = "BUYER"
            # Use provided created_by or default to current user
            if not vendor_dict.get("created_by"):
                vendor_dict["created_by"] = current_user.user_id
            rfq_vendor = RFQVendor(**vendor_dict)
            db.add(rfq_vendor)
            created_vendors.append(rfq_vendor)

        db.commit()
        for vendor in created_vendors:
            db.refresh(vendor)

        return created_vendors

    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database constraint violation: {str(e)}"
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating RFQ vendors: {str(e)}"
        )


@router.put("/vendors/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}/{vendor_id}", response_model=RFQVendorResponse)
@router.patch("/vendors/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}/{vendor_id}", response_model=RFQVendorResponse)
async def update_rfq_vendor(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    vendor_id: int,
    vendor_update: RFQVendorUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update an existing RFQ vendor (mainly for audit fields)
    Note: vendor_id cannot be changed as it's part of the primary key
    """
    company_id = resolve_company_id(company_id)
    rfq_vendor = db.query(RFQVendor).filter(
        RFQVendor.tenant_id == tenant_id,
        RFQVendor.company_id == company_id,
        RFQVendor.document_type == document_type,
        RFQVendor.rfq_no == rfq_no,
        RFQVendor.version_no == version_no,
        RFQVendor.vendor_id == vendor_id
    ).first()

    if not rfq_vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ vendor not found"
        )

    try:
        # Update fields
        update_data = vendor_update.dict(exclude_unset=True)
        for key, value in update_data.items():
            if hasattr(rfq_vendor, key) and key != "updated_by":
                setattr(rfq_vendor, key, value)

        # Set updated_by if provided, otherwise use current user
        if vendor_update.updated_by:
            rfq_vendor.updated_by = vendor_update.updated_by
        else:
            rfq_vendor.updated_by = current_user.user_id

        rfq_vendor.updated_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(rfq_vendor)

        return rfq_vendor

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating RFQ vendor: {str(e)}"
        )


@router.patch(
    "/vendors/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}/{vendor_id}/freeze",
    response_model=RFQVendorResponse,
)
async def freeze_rfq_vendor(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Set `rfq_vendor.status` to FREEZED for the given RFQ header + vendor (same composite key as other vendor routes).
    """
    company_id = resolve_company_id(company_id)
    rfq_vendor = db.query(RFQVendor).filter(
        RFQVendor.tenant_id == tenant_id,
        RFQVendor.company_id == company_id,
        RFQVendor.document_type == document_type,
        RFQVendor.rfq_no == rfq_no,
        RFQVendor.version_no == version_no,
        RFQVendor.vendor_id == vendor_id,
    ).first()

    if not rfq_vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ vendor not found",
        )

    try:
        rfq_vendor.status = "FREEZED"
        rfq_vendor.updated_by = current_user.user_id
        rfq_vendor.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(rfq_vendor)
        return rfq_vendor
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error freezing RFQ vendor: {str(e)}",
        )


@router.delete("/vendors/{tenant_id}/{company_id}/{document_type}/{rfq_no}/{version_no}/{vendor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rfq_vendor(
    tenant_id: int,
    company_id: int,
    document_type: str,
    rfq_no: str,
    version_no: int,
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Delete an RFQ vendor
    """
    company_id = resolve_company_id(company_id)
    rfq_vendor = db.query(RFQVendor).filter(
        RFQVendor.tenant_id == tenant_id,
        RFQVendor.company_id == company_id,
        RFQVendor.document_type == document_type,
        RFQVendor.rfq_no == rfq_no,
        RFQVendor.version_no == version_no,
        RFQVendor.vendor_id == vendor_id
    ).first()

    if not rfq_vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFQ vendor not found"
        )

    try:
        db.delete(rfq_vendor)
        db.commit()
        return None

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting RFQ vendor: {str(e)}"
        )

