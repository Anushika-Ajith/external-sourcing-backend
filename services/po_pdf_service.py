"""Generate a Purchase Order PDF and attach it to the PO header."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from models import PurchaseOrderHeader, PurchaseOrderLine

logger = logging.getLogger(__name__)

PO_ATTACHMENT_DIR = Path("uploads") / "purchase_orders"


def _fmt(value: Any, *, money: bool = False) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if money:
            return f"{value:,.2f}"
        return format(value, "f").rstrip("0").rstrip(".") or "0"
    if money:
        try:
            return f"{Decimal(str(value)):,.2f}"
        except Exception:
            return str(value)
    return str(value)


def _safe_filename(po_number: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (po_number or "PO").strip()) or "PO"
    return cleaned[:80]


def build_purchase_order_pdf_bytes(
    header: PurchaseOrderHeader,
    lines: Sequence[PurchaseOrderLine],
) -> bytes:
    """Render a PO PDF (header details + line items + totals) to bytes."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"Purchase Order {header.po_number}",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "PoTitle",
        parent=styles["Heading1"],
        fontSize=18,
        alignment=TA_CENTER,
        spaceAfter=4,
        textColor=colors.HexColor("#0f1f4b"),
    )
    subtitle_style = ParagraphStyle(
        "PoSubtitle",
        parent=styles["Normal"],
        fontSize=10,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#475569"),
        spaceAfter=12,
    )
    section_style = ParagraphStyle(
        "PoSection",
        parent=styles["Heading2"],
        fontSize=11,
        textColor=colors.HexColor("#0f1f4b"),
        spaceBefore=10,
        spaceAfter=6,
    )
    label_style = ParagraphStyle(
        "PoLabel",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#64748b"),
        leading=10,
    )
    value_style = ParagraphStyle(
        "PoValue",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#0f172a"),
        leading=12,
    )
    cell_style = ParagraphStyle(
        "PoCell",
        parent=styles["Normal"],
        fontSize=8,
        leading=10,
        alignment=TA_LEFT,
    )
    cell_right = ParagraphStyle(
        "PoCellRight",
        parent=cell_style,
        alignment=TA_RIGHT,
    )
    footer_style = ParagraphStyle(
        "PoFooter",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#64748b"),
        alignment=TA_CENTER,
    )

    story: List[Any] = []
    story.append(Paragraph("PURCHASE ORDER", title_style))
    story.append(
        Paragraph(
            f"{_fmt(header.po_number)} · Version {_fmt(header.version_no)}",
            subtitle_style,
        )
    )

    currency = _fmt(header.currency_code) if header.currency_code else "INR"
    meta_rows = [
        [
            Paragraph("PO Date", label_style),
            Paragraph(_fmt(header.po_date), value_style),
            Paragraph("Status", label_style),
            Paragraph(_fmt(header.status), value_style),
        ],
        [
            Paragraph("Approval", label_style),
            Paragraph(_fmt(header.approval_status), value_style),
            Paragraph("Document Type", label_style),
            Paragraph(_fmt(header.document_type), value_style),
        ],
        [
            Paragraph("Vendor", label_style),
            Paragraph(
                f"{_fmt(header.vendor_name_snapshot)} (ID {_fmt(header.vendor_id)})",
                value_style,
            ),
            Paragraph("Currency", label_style),
            Paragraph(currency, value_style),
        ],
        [
            Paragraph("Expected Delivery", label_style),
            Paragraph(_fmt(header.expected_delivery_date), value_style),
            Paragraph("Source", label_style),
            Paragraph(_fmt(header.source_type), value_style),
        ],
        [
            Paragraph("Incoterms", label_style),
            Paragraph(_fmt(header.incoterms), value_style),
            Paragraph("Delivery Terms", label_style),
            Paragraph(_fmt(header.delivery_terms), value_style),
        ],
        [
            Paragraph("Tenant / Company", label_style),
            Paragraph(
                f"{_fmt(header.tenant_id)} / {_fmt(header.company_id)}",
                value_style,
            ),
            Paragraph("Editor", label_style),
            Paragraph(_fmt(header.current_editor), value_style),
        ],
    ]
    meta_rows.append(
        [
            Paragraph("Document No.", label_style),
            Paragraph(_fmt(header.document_number), value_style),
            Paragraph("PO ID", label_style),
            Paragraph(_fmt(header.po_id), value_style),
        ]
    )

    meta_table = Table(meta_rows, colWidths=[32 * mm, 58 * mm, 32 * mm, 58 * mm])
    meta_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(meta_table)

    if header.remarks:
        story.append(Paragraph("Remarks", section_style))
        story.append(Paragraph(_fmt(header.remarks), value_style))

    story.append(Paragraph("Line Items", section_style))

    header_row = [
        Paragraph("<b>#</b>", cell_style),
        Paragraph("<b>Item</b>", cell_style),
        Paragraph("<b>Description</b>", cell_style),
        Paragraph("<b>UOM</b>", cell_style),
        Paragraph("<b>Qty</b>", cell_right),
        Paragraph("<b>Unit Price</b>", cell_right),
        Paragraph("<b>Tax %</b>", cell_right),
        Paragraph("<b>Line Total</b>", cell_right),
    ]
    data: List[List[Any]] = [header_row]

    sorted_lines = sorted(lines, key=lambda ln: getattr(ln, "line_no", 0) or 0)
    for line in sorted_lines:
        item_label = _fmt(line.item_no) if line.item_no else _fmt(line.item_id)
        data.append(
            [
                Paragraph(_fmt(line.line_no), cell_style),
                Paragraph(item_label, cell_style),
                Paragraph(_fmt(line.description), cell_style),
                Paragraph(_fmt(line.uom), cell_style),
                Paragraph(_fmt(line.qty_ordered), cell_right),
                Paragraph(
                    _fmt(
                        line.unit_price
                        if line.unit_price is not None
                        else line.po_item_rate,
                        money=True,
                    ),
                    cell_right,
                ),
                Paragraph(_fmt(line.tax_percent), cell_right),
                Paragraph(_fmt(line.total_amount, money=True), cell_right),
            ]
        )

    if len(data) == 1:
        data.append(
            [
                Paragraph("—", cell_style),
                Paragraph("No lines", cell_style),
                Paragraph("—", cell_style),
                Paragraph("—", cell_style),
                Paragraph("—", cell_right),
                Paragraph("—", cell_right),
                Paragraph("—", cell_right),
                Paragraph("—", cell_right),
            ]
        )

    col_widths = [10 * mm, 22 * mm, 55 * mm, 14 * mm, 18 * mm, 24 * mm, 16 * mm, 24 * mm]
    lines_table = Table(data, colWidths=col_widths, repeatRows=1)
    lines_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f1f4b")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BACKGROUND", (0, 1), (-1, -1), colors.white),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(lines_table)
    story.append(Spacer(1, 8 * mm))

    totals = [
        ["Subtotal / PO Total", _fmt(header.po_total_amount, money=True)],
        ["Tax Total", _fmt(header.tax_total_amount, money=True)],
        ["Discount", _fmt(header.discount_amount, money=True)],
        ["Net Total", _fmt(header.net_total_amount, money=True)],
    ]
    totals_table = Table(
        [
            [
                Paragraph(f"<b>{label}</b>", cell_style),
                Paragraph(f"<b>{value}</b> {currency}", cell_right),
            ]
            for label, value in totals
        ],
        colWidths=[40 * mm, 45 * mm],
        hAlign="RIGHT",
    )
    totals_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -2), colors.HexColor("#f8fafc")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e2e8f0")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(totals_table)
    story.append(Spacer(1, 10 * mm))
    story.append(
        Paragraph(
            "Generated automatically when the purchase order was created.",
            footer_style,
        )
    )

    doc.build(story)
    return buffer.getvalue()


def persist_po_pdf(file_bytes: bytes, po_id: int, po_number: str) -> str:
    """Write PDF bytes under uploads/purchase_orders/{po_id}/ and return relative path."""
    dest_dir = PO_ATTACHMENT_DIR / str(po_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_safe_filename(po_number)}.pdf"
    dest = dest_dir / filename
    # Avoid clobbering an existing file with the same name.
    if dest.exists():
        dest = dest_dir / f"{_safe_filename(po_number)}-{po_id}.pdf"
    dest.write_bytes(file_bytes)
    return str(dest)


def generate_and_attach_po_pdf(
    header: PurchaseOrderHeader,
    lines: Optional[Iterable[PurchaseOrderLine]] = None,
    *,
    overwrite: bool = False,
) -> Optional[str]:
    """
    Build a PO PDF and set header.attachment_path / attachment_count.

    Returns the saved path, or None if skipped/failed.
    Does not commit — caller owns the DB transaction.
    """
    if header is None or getattr(header, "po_id", None) is None:
        return None

    existing = (getattr(header, "attachment_path", None) or "").strip()
    if existing and not overwrite:
        logger.info(
            "Skipping PO PDF generation for po_id=%s — attachment already set",
            header.po_id,
        )
        return existing

    line_list: List[PurchaseOrderLine]
    if lines is None:
        line_list = list(getattr(header, "po_lines", None) or [])
    else:
        line_list = list(lines)

    try:
        pdf_bytes = build_purchase_order_pdf_bytes(header, line_list)
        path = persist_po_pdf(pdf_bytes, int(header.po_id), str(header.po_number or "PO"))
        header.attachment_path = path
        header.attachment_count = max(int(header.attachment_count or 0), 0) + 1
        logger.info(
            "Generated PO PDF for po_id=%s po_number=%s path=%s",
            header.po_id,
            header.po_number,
            path,
        )
        return path
    except Exception:
        logger.exception(
            "Failed to generate PO PDF for po_id=%s po_number=%s",
            getattr(header, "po_id", None),
            getattr(header, "po_number", None),
        )
        return None
