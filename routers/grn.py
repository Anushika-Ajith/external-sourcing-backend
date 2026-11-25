"""GRN line attachments: returned / damaged goods photos."""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import User, Vendor
from services.auth_utils import get_current_user
from services.company_scope import resolve_company_id
from services.grn_list_service import list_grn_headers
from services.grn_return_photo_service import (
    GrnLineKey,
    IncomingPhoto,
    add_return_photos,
    remove_return_photo,
)
from services.vendor_phone_lookup import find_vendor_by_phone
from services.whatsapp_auth import require_whatsapp_api_key

router = APIRouter(
    prefix="/grn",
    tags=["grn"],
    responses={404: {"description": "Not found"}},
)


class WhatsAppGrnLineSummary(BaseModel):
    line_no: int
    item_name: str
    item_no: Optional[str] = None
    qty: Optional[float] = None
    uom: Optional[str] = None
    unit_price: Optional[float] = None
    line_amount: Optional[float] = None


class WhatsAppGrnListItem(BaseModel):
    grn_number: str
    version_no: int = 1
    document_type: str = "GRN"
    po_number: Optional[str] = None
    vendor_id: Optional[int] = None
    vendor_name: Optional[str] = None
    grn_date: Optional[str] = None
    status: Optional[str] = None
    inspection_status: Optional[str] = None
    line_count: int = 0
    lines: List[WhatsAppGrnLineSummary] = Field(default_factory=list)


def _vendor_names_by_id(db: Session, vendor_ids: List[int]) -> Dict[int, str]:
    ids = sorted({int(v) for v in vendor_ids if v})
    if not ids:
        return {}
    rows = db.query(Vendor).filter(Vendor.vendor_id.in_(ids)).all()
    out: Dict[int, str] = {}
    for row in rows:
        if row.vendor_id is None:
            continue
        name = (row.company_name or "").strip() or f"Vendor {row.vendor_id}"
        out[int(row.vendor_id)] = name
    return out


@router.get(
    "/whatsapp/by-phone",
    response_model=List[WhatsAppGrnListItem],
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def list_grns_for_whatsapp_phone(
    mobile: str = Query(..., description="WhatsApp sender phone / mobile"),
    tenant_id: Optional[int] = Query(1),
    company_id: Optional[int] = Query(3),
    document_type: str = Query("GRN"),
    scope: str = Query(
        "auto",
        description=(
            "auto = vendor GRNs if phone matches a vendor, else all in scope; "
            "all = all GRNs in tenant/company; "
            "vendor = only that vendor's GRNs"
        ),
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    WhatsApp list of GRNs via existing ERP ``list_grn_headers``
    (``/grn-header/with-lines``). Compact chat summary with line qtys.
    """
    company_id = resolve_company_id(company_id)
    tenant = int(tenant_id or 1)
    vendor = find_vendor_by_phone(db, mobile)
    scope_norm = (scope or "auto").strip().lower()
    vendor_id: Optional[int] = None
    if scope_norm == "vendor":
        if not vendor:
            return []
        vendor_id = int(vendor.vendor_id)
    elif scope_norm != "all" and vendor:
        vendor_id = int(vendor.vendor_id)

    try:
        result = list_grn_headers(
            tenant_id=tenant,
            company_id=company_id,
            document_type=document_type or "GRN",
            vendor_id=vendor_id,
            page=page,
            limit=limit,
            include_lines=True,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    items_raw = list(result.get("items") or [])
    vendor_names = _vendor_names_by_id(
        db,
        [int(r.get("vendor_id") or 0) for r in items_raw],
    )

    out: List[WhatsAppGrnListItem] = []
    for row in items_raw:
        grn_number = str(row.get("grn_number") or "").strip()
        if not grn_number:
            continue
        lines_raw = row.get("lines") if isinstance(row.get("lines"), list) else []
        lines = [
            WhatsAppGrnLineSummary(
                line_no=int(line.get("line_no") or 0),
                item_name=str(line.get("item_name") or "Line"),
                item_no=line.get("item_no"),
                qty=line.get("qty"),
                uom=line.get("uom"),
                unit_price=line.get("unit_price"),
                line_amount=line.get("line_amount"),
            )
            for line in lines_raw
            if isinstance(line, dict)
        ]
        vid = int(row.get("vendor_id") or 0) or None
        grn_date = row.get("grn_date")
        out.append(
            WhatsAppGrnListItem(
                grn_number=grn_number,
                version_no=int(row.get("version_no") or 1),
                document_type=str(row.get("document_type") or "GRN"),
                po_number=str(row.get("po_number") or "") or None,
                vendor_id=vid,
                vendor_name=vendor_names.get(vid) if vid else None,
                grn_date=str(grn_date)[:10] if grn_date is not None else None,
                status=str(row.get("status") or "") or None,
                inspection_status=str(row.get("inspection_status") or "") or None,
                line_count=len(lines),
                lines=lines,
            )
        )
    return out


@router.post("/line/return-photos")
async def upload_grn_line_return_photos(
    tenant_id: int = Form(..., alias="tenantId"),
    company_id: int = Form(..., alias="companyId"),
    document_type: str = Form("GRN", alias="documentType"),
    grn_number: str = Form(..., alias="grnNumber"),
    version_no: int = Form(1, alias="versionNo"),
    line_no: int = Form(..., alias="lineNo"),
    condition: str = Form(
        ...,
        description="RETURNED, DAMAGED, or SAMPLE (store-keeper count / label photos)",
    ),
    remarks: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
):
    """Attach photos of returned/damaged goods to a GRN line and read the label text from each."""
    photos = [
        IncomingPhoto(file_name=f.filename or "photo.jpg", content=await f.read())
        for f in files
    ]
    key = GrnLineKey(tenant_id, company_id, document_type, grn_number, version_no, line_no)
    return await run_in_threadpool(
        add_return_photos,
        key=key,
        condition=condition,
        photos=photos,
        remarks=remarks,
        uploaded_by=getattr(current_user, "user_id", None),
    )


@router.post(
    "/whatsapp/line/return-photos",
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def upload_grn_line_return_photos_for_whatsapp(
    tenant_id: int = Form(..., alias="tenantId"),
    company_id: int = Form(..., alias="companyId"),
    document_type: str = Form("GRN", alias="documentType"),
    grn_number: str = Form(..., alias="grnNumber"),
    version_no: int = Form(1, alias="versionNo"),
    line_no: int = Form(..., alias="lineNo"),
    condition: str = Form(
        "SAMPLE",
        description="RETURNED, DAMAGED, or SAMPLE (default SAMPLE for count/label scan)",
    ),
    remarks: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """
    Same as UI Count Inspect / return photos:
    `POST /grn/line/return-photos` (OCR label text on each image).
    Auth: X-WhatsApp-API-Key.
    """
    _ = db
    company_id = resolve_company_id(company_id)
    photos = [
        IncomingPhoto(file_name=f.filename or "photo.jpg", content=await f.read())
        for f in files
    ]
    key = GrnLineKey(
        tenant_id,
        company_id,
        document_type or "GRN",
        grn_number,
        version_no,
        line_no,
    )
    return await run_in_threadpool(
        add_return_photos,
        key=key,
        condition=(condition or "SAMPLE").strip().upper() or "SAMPLE",
        photos=photos,
        remarks=remarks,
        uploaded_by=1,
    )


@router.delete("/line/return-photos/{photo_id}")
async def delete_grn_line_return_photo(
    photo_id: str,
    tenant_id: int = Query(..., alias="tenantId"),
    company_id: int = Query(..., alias="companyId"),
    document_type: str = Query("GRN", alias="documentType"),
    grn_number: str = Query(..., alias="grnNumber"),
    version_no: int = Query(1, alias="versionNo"),
    line_no: int = Query(..., alias="lineNo"),
    current_user: User = Depends(get_current_user),
):
    key = GrnLineKey(tenant_id, company_id, document_type, grn_number, version_no, line_no)
    return await run_in_threadpool(remove_return_photo, key=key, photo_id=photo_id)
