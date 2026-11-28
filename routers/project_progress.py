"""WhatsApp + API routes for project_progress (NBCC / works MIS)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import ProjectProgress
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

_DATASET_COLUMNS = (
    "sl_no",
    "project_code",
    "work_type",
    "project_status",
    "district_name",
    "block_name",
    "name_of_work",
    "client",
    "project_type",
    "value_cr",
    "project_start_date",
    "scheduled_duration",
    "scheduled_completion_date",
    "project_months_over",
    "days_left_scheduled",
    "planned_physical_progress",
    "actual_physical_progress",
    "planned_vs_actual_progress",
    "actual_completion_date",
    "delay_category",
)


def _cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


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


@router.get(
    "/whatsapp/dataset",
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def project_progress_dataset_for_whatsapp(
    mobile: Optional[str] = Query(
        None, description="WhatsApp sender phone (optional; unused for filtering)"
    ),
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    limit: int = Query(20000, ge=1, le=50000),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Full project_progress rows for OpenAI Code Interpreter on cache miss.

    Auth: X-WhatsApp-API-Key. No user JWT.
    """
    _ = mobile
    q = db.query(ProjectProgress)
    if tenant_id is not None:
        q = q.filter(ProjectProgress.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(ProjectProgress.company_id == company_id)

    total = int(q.with_entities(func.count(ProjectProgress.project_progress_id)).scalar() or 0)
    rows_orm = (
        q.order_by(
            ProjectProgress.sl_no.asc().nullslast(),
            ProjectProgress.project_progress_id.asc(),
        )
        .limit(limit)
        .all()
    )

    rows: List[Dict[str, Any]] = []
    for r in rows_orm:
        rows.append({col: _cell(getattr(r, col, None)) for col in _DATASET_COLUMNS})

    print(
        f"[PROJECT-WHATSAPP-DATASET] total={total} returned={len(rows)} "
        f"limit={limit} mobile={mobile or '-'}"
    )
    return {
        "rowCount": total,
        "returned": len(rows),
        "columns": list(_DATASET_COLUMNS),
        "rows": rows,
    }
