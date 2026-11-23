"""Schemas for 3-way match invoice review queue."""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, ConfigDict


class ThreeWayMatchPoQueueItem(BaseModel):
    """One PO row in the invoice review 3-way match queue."""

    model_config = ConfigDict(extra="ignore")

    id: str
    po_number: str
    po_id: str
    vendor_id: str
    invoice_no: str
    invoice_count: int
    grn_no: str
    grn_count: int
    invoice_date: str
    grn_date: str
    invoice_amount: str
    currency_code: str
    status: str


class ThreeWayMatchQueueResponse(BaseModel):
    items: List[ThreeWayMatchPoQueueItem]
    total: int
    take: int
    skip: int
