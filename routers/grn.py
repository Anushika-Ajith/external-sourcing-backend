"""GRN line attachments: returned / damaged goods photos."""

from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from database import get_db
from models import User
from services.auth_utils import get_current_user
from services.company_scope import resolve_company_id
from services.grn_return_photo_service import (
    GrnLineKey,
    IncomingPhoto,
    add_return_photos,
    remove_return_photo,
)
from services.whatsapp_auth import require_whatsapp_api_key

router = APIRouter(
    prefix="/grn",
    tags=["grn"],
    responses={404: {"description": "Not found"}},
)


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
