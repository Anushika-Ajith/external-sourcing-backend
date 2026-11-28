"""WhatsApp + API routes for project_progress (NBCC / works MIS)."""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from schemas.project_progress_dashboard import ProjectProgressDashboardSummaryResponse
from services.project_progress_dashboard_summary import (
    build_project_progress_dashboard_summary,
)
from services.whatsapp_auth import require_whatsapp_api_key

router = APIRouter(
    prefix="/project-progress",
    tags=["project-progress"],
    responses={404: {"description": "Not found"}},
)


@router.get(
    "/whatsapp/dashboard-summary",
    response_model=ProjectProgressDashboardSummaryResponse,
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def project_progress_dashboard_summary_for_whatsapp(
    mobile: Optional[str] = Query(
        None, description="WhatsApp sender phone (optional; unused for filtering)"
    ),
    focus: Optional[str] = Query(
        None, description="Optional focus question for insight wording"
    ),
    district_name: Optional[str] = Query(None),
    project_status: Optional[str] = Query(None),
    delay_category: Optional[str] = Query(None),
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Pre-aggregate project_progress for the WhatsApp Chart.js dashboard.

    Auth: X-WhatsApp-API-Key. No user JWT.
    """
    _ = mobile
    summary = build_project_progress_dashboard_summary(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        district_name=district_name,
        project_status=project_status,
        delay_category=delay_category,
        focus_question=focus,
    )
    print(
        f"[PROJECT-WHATSAPP-SUMMARY] rowCount={summary.get('rowCount')} "
        f"records={len(summary.get('records') or [])} "
        f"districts={len(summary.get('byDistrict') or [])} "
        f"mobile={mobile or '-'}"
    )
    return summary
