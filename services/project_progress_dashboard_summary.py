"""Pre-aggregate project_progress in Postgres for the WhatsApp Chart.js dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import ProjectProgress

_TOP_PROJECTS = 10
_TABLE_PROJECTS = 25
_MAX_RECORDS = 5000


def _round2(n: float) -> float:
    return round(float(n) * 100) / 100


def _en_in(n: float) -> str:
    v = _round2(n)
    if abs(v - int(v)) < 1e-9:
        return f"{int(v):,}"
    return f"{v:,.2f}"


def build_project_progress_dashboard_summary(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
    district_name: Optional[str] = None,
    project_status: Optional[str] = None,
    delay_category: Optional[str] = None,
    focus_question: Optional[str] = None,
) -> Dict[str, Any]:
    base = db.query(ProjectProgress)
    if tenant_id is not None:
        base = base.filter(ProjectProgress.tenant_id == tenant_id)
    if company_id is not None:
        base = base.filter(ProjectProgress.company_id == company_id)
    if district_name:
        base = base.filter(ProjectProgress.district_name.ilike(f"%{district_name}%"))
    if project_status:
        base = base.filter(ProjectProgress.project_status.ilike(f"%{project_status}%"))
    if delay_category:
        base = base.filter(ProjectProgress.delay_category.ilike(f"%{delay_category}%"))

    row_count = int(
        base.with_entities(func.count(ProjectProgress.project_progress_id)).scalar()
        or 0
    )
    if row_count == 0:
        return {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "rowCount": 0,
            "kpis": [],
            "byDistrict": [],
            "byStatus": [],
            "byDelayCategory": [],
            "topProjects": [],
            "table": [],
            "insights": [],
            "districtsList": [],
            "records": [],
        }

    totals = base.with_entities(
        func.coalesce(func.sum(ProjectProgress.value_cr), 0),
        func.avg(ProjectProgress.planned_physical_progress),
        func.avg(ProjectProgress.actual_physical_progress),
        func.count(func.distinct(func.coalesce(ProjectProgress.district_name, ""))),
        func.count(func.distinct(func.coalesce(ProjectProgress.delay_category, ""))),
    ).one()
    total_value = float(totals[0] or 0)
    avg_planned = float(totals[1] or 0) if totals[1] is not None else 0.0
    avg_actual = float(totals[2] or 0) if totals[2] is not None else 0.0
    district_count = int(totals[3] or 0)
    delay_count = int(totals[4] or 0)

    district_expr = func.coalesce(ProjectProgress.district_name, "Unknown")
    district_rows = (
        base.with_entities(
            district_expr,
            func.coalesce(func.sum(ProjectProgress.value_cr), 0),
        )
        .group_by(district_expr)
        .order_by(func.sum(ProjectProgress.value_cr).desc())
        .all()
    )
    by_district: List[Tuple[str, float]] = [
        (str(r[0] or "Unknown"), _round2(float(r[1] or 0))) for r in district_rows
    ]
    districts_list = [d[0] for d in by_district]

    status_expr = func.coalesce(ProjectProgress.project_status, "Unknown")
    status_rows = (
        base.with_entities(
            status_expr,
            func.count(ProjectProgress.project_progress_id),
        )
        .group_by(status_expr)
        .order_by(func.count(ProjectProgress.project_progress_id).desc())
        .all()
    )
    by_status = [
        {"name": str(r[0] or "Unknown"), "value": float(r[1] or 0)} for r in status_rows
    ]

    delay_expr = func.coalesce(ProjectProgress.delay_category, "Unknown")
    delay_rows = (
        base.with_entities(
            delay_expr,
            func.count(ProjectProgress.project_progress_id),
        )
        .group_by(delay_expr)
        .order_by(func.count(ProjectProgress.project_progress_id).desc())
        .all()
    )
    by_delay = [
        {"name": str(r[0] or "Unknown"), "value": float(r[1] or 0)} for r in delay_rows
    ]

    project_label = func.coalesce(
        ProjectProgress.project_code,
        func.left(func.coalesce(ProjectProgress.name_of_work, "Unknown"), 40),
    )
    project_rows = (
        base.with_entities(
            project_label,
            func.coalesce(func.sum(ProjectProgress.value_cr), 0),
        )
        .group_by(project_label)
        .order_by(func.sum(ProjectProgress.value_cr).desc())
        .all()
    )
    top_projects: List[Tuple[str, float]] = [
        (str(r[0] or "Unknown"), _round2(float(r[1] or 0)))
        for r in project_rows[:_TOP_PROJECTS]
    ]
    table: List[Tuple[str, float, str]] = []
    for r in project_rows[:_TABLE_PROJECTS]:
        value = _round2(float(r[1] or 0))
        share = f"{(value / total_value * 100):.1f}%" if total_value else "0.0%"
        table.append((str(r[0] or "Unknown"), value, share))

    record_rows = (
        base.order_by(
            ProjectProgress.value_cr.desc().nullslast(),
            ProjectProgress.project_progress_id.desc(),
        )
        .limit(_MAX_RECORDS)
        .all()
    )
    records = [
        {
            "projectCode": str(r.project_code or ""),
            "nameOfWork": str(r.name_of_work or "")[:120],
            "district": str(r.district_name or "Unknown"),
            "status": str(r.project_status or "Unknown"),
            "delayCategory": str(r.delay_category or "Unknown"),
            "valueCr": _round2(float(r.value_cr or 0)),
            "plannedProgress": _round2(float(r.planned_physical_progress or 0)),
            "actualProgress": _round2(float(r.actual_physical_progress or 0)),
        }
        for r in record_rows
    ]

    top_district = by_district[0] if by_district else None
    top_delay = by_delay[0] if by_delay else None
    top_project = top_projects[0] if top_projects else None
    lagging = max(0.0, avg_planned - avg_actual)

    insights: List[str] = []
    focus = (focus_question or "").strip()
    if focus:
        insights.append(f"Focus: {focus[:180]}")
    if top_project and total_value:
        insights.append(
            f"Highest-value project: {top_project[0]} at {_en_in(top_project[1])} Cr "
            f"({(top_project[1] / total_value * 100):.1f}% of portfolio value)."
        )
    if top_district:
        insights.append(
            f"Top district by value: {top_district[0]} with {_en_in(top_district[1])} Cr."
        )
    if top_delay:
        insights.append(
            f"Most common delay category: {top_delay['name']} "
            f"({int(top_delay['value'])} project(s))."
        )
    insights.append(
        f"Portfolio: {row_count:,} projects, {_en_in(total_value)} Cr total value, "
        f"avg planned {_en_in(avg_planned)}% vs avg actual {_en_in(avg_actual)}% "
        f"(gap {_en_in(lagging)} pts)."
    )

    kpis = [
        {"label": "Projects", "value": f"{row_count:,}"},
        {"label": "Total value (Cr)", "value": _en_in(total_value)},
        {"label": "Districts", "value": str(district_count)},
        {"label": "Delay categories", "value": str(delay_count)},
        {"label": "Avg planned %", "value": _en_in(avg_planned)},
        {"label": "Avg actual %", "value": _en_in(avg_actual)},
    ]

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "rowCount": row_count,
        "kpis": kpis,
        "byDistrict": by_district,
        "byStatus": by_status,
        "byDelayCategory": by_delay,
        "topProjects": top_projects,
        "table": table,
        "insights": insights,
        "districtsList": districts_list,
        "records": records,
    }
