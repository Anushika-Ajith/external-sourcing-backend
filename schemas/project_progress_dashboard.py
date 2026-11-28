"""WhatsApp Chart.js project-progress dashboard summary (pre-aggregated)."""

from __future__ import annotations

from typing import List, Optional, Tuple

from pydantic import BaseModel, Field


class ProjectDashboardKpi(BaseModel):
    label: str
    value: str


class ProjectDashboardCategory(BaseModel):
    name: str
    value: float


class ProjectDashboardRecord(BaseModel):
    projectCode: str
    nameOfWork: str
    district: str
    status: str
    delayCategory: str
    valueCr: float
    plannedProgress: float
    actualProgress: float


class ProjectProgressDashboardSummaryResponse(BaseModel):
    """Matches whatsapp-backend ProjectDashboardData (+ rowCount)."""

    generatedAt: Optional[str] = None
    rowCount: int = 0
    kpis: List[ProjectDashboardKpi] = Field(default_factory=list)
    byDistrict: List[Tuple[str, float]] = Field(default_factory=list)
    byStatus: List[ProjectDashboardCategory] = Field(default_factory=list)
    byDelayCategory: List[ProjectDashboardCategory] = Field(default_factory=list)
    topProjects: List[Tuple[str, float]] = Field(default_factory=list)
    table: List[Tuple[str, float, str]] = Field(default_factory=list)
    insights: List[str] = Field(default_factory=list)
    districtsList: List[str] = Field(default_factory=list)
    records: List[ProjectDashboardRecord] = Field(default_factory=list)
