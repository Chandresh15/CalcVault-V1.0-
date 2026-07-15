"""
pdf_generator.py — CalcVault (Ramboll Edition)
==============================================
Branded PDF report generator for every calculation module.

Produces:  A4 portrait, blue header bar with "R" mark, subtitle,
           inputs table, formula block (with superscripts),
           results table (with the "primary" result highlighted),
           optional notes / warnings, footer with report ID + user.

Public API:
    generate_report(
        module_name:  str,
        module_icon:  str,
        inputs:       list[dict]  # [{'label','value','unit'}]
        results:      list[dict]  # [{'label','value','unit','primary'?}]
        formula:      str | None  # e.g. "H = (Q / (1000.8·C·d^2.63))^1.852 · L"
        notes:        str | None
        user_name:    str
        run_at:       datetime | None
    ) -> tuple[bytes, str]         # (pdf_bytes, report_id)

Depends only on reportlab + Pillow (already in requirements.txt).
"""

from __future__ import annotations
import io
import re
import random
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.pdfgen import canvas as _canvas
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, Table,
    TableStyle, KeepTogether,
)


# ---------------------------------------------------------------
# Ramboll palette + layout constants
# ---------------------------------------------------------------
RAMBOLL_BLUE  = colors.HexColor("#1F3F89")
RAMBOLL_CYAN  = colors.HexColor("#00A0DC")
INK           = colors.HexColor("#111827")
INK_MUTED     = colors.HexColor("#6B7280")
CARD_BG       = colors.HexColor("#F3F6FB")
CARD_LINE     = colors.HexColor("#D9E1EF")
GOOD_BG       = colors.HexColor("#EAF6FF")

PAGE_W, PAGE_H = A4                    # 210 × 297 mm
MARGIN_X       = 16 * mm
HEADER_H       = 26 * mm               # space reserved for the top blue band
FOOTER_H       = 14 * mm

BODY_TOP    = PAGE_H - HEADER_H - 6 * mm
BODY_BOTTOM = FOOTER_H + 4 * mm
BODY_H      = BODY_TOP - BODY_BOTTOM


# ---------------------------------------------------------------
# Paragraph styles  (leading = 1.35 × fontSize → no line clashes)
# ---------------------------------------------------------------
def _mkstyle(name: str, size: int, **kw) -> ParagraphStyle:
    return ParagraphStyle(
        name        = name,
        fontName    = kw.pop("fontName", "Helvetica"),
        fontSize    = size,
        leading     = kw.pop("leading",  round(size * 1.35, 1)),
        textColor   = kw.pop("textColor", INK),
        spaceBefore = kw.pop("spaceBefore", 0),
        spaceAfter  = kw.pop("spaceAfter",  0),
        alignment   = kw.pop("alignment",   TA_LEFT),
        **kw,
    )

STYLE_H1    = _mkstyle("H1",    16, fontName="Helvetica-Bold", spaceAfter=2)
STYLE_H2    = _mkstyle("H2",    12, fontName="Helvetica-Bold",
                       textColor=RAMBOLL_BLUE, spaceBefore=8, spaceAfter=4)
STYLE_BODY  = _mkstyle("Body",  10, spaceAfter=3)
STYLE_MUTE  = _mkstyle("Mute",   9, textColor=INK_MUTED)
STYLE_CELL  = _mkstyle("Cell",  10, leading=13)
STYLE_CELLB = _mkstyle("CellB", 10, fontName="Helvetica-Bold", leading=13)
STYLE_FORM  = _mkstyle("Form",  11, fontName="Courier",
                       textColor=RAMBOLL_BLUE, leading=15)
STYLE_NOTE  = _mkstyle("Note",   9, textColor=INK_MUTED, leading=12)


# ===============================================================
# Formula prettifier  →  ReportLab inline markup
# ===============================================================
_SUP_PATTERN  = re.compile(r"\^\(([^)]+)\)|\^(-?\d+(?:\.\d+)?(?:/\d+)?)")
_SUB_PATTERN  = re.compile(r"_([A-Za-z0-9])")
_MULT_PATTERN = re.compile(r"\*")

def prettify_formula(formula: str) -> str:
    """
    Turn engineer-style formula text into ReportLab-safe inline markup.
      ^(2/3)  → <super>2/3</super>
      ^1.852  → <super>1.852</super>
      d_h     → d<sub>h</sub>
      *       → ·  (middle dot)
    """
    if not formula:
        return ""
    s = formula
    s = _SUP_PATTERN.sub(
        lambda m: f"<super>{m.group(1) or m.group(2)}</super>", s)
    s = _SUB_PATTERN.sub(lambda m: f"<sub>{m.group(1)}</sub>", s)
    s = _MULT_PATTERN.sub("·", s)
    return s


# ===============================================================
# Small formatter for numeric values
# ===============================================================
def _fmt_value(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int,)):
        return f"{v}"
    if isinstance(v, float):
        if v == 0:
            return "0"
        # 4 sig-figs for < 1, else up to 4 decimals, trim trailing zeros
        s = f"{v:.4f}" if abs(v) >= 1 else f"{v:.4g}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"
    return str(v)


# ===============================================================
# Header / footer painter (called by PageTemplate)
# ===============================================================
def _draw_chrome(c: _canvas.Canvas, doc: BaseDocTemplate,
                 *, title: str, subtitle: str, report_id: str,
                 user_name: str, run_at: datetime) -> None:
    # ------ Header band ------------------------------------------------
    c.saveState()
    c.setFillColor(RAMBOLL_BLUE)
    c.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, stroke=0, fill=1)

    # Cyan accent underline
    c.setFillColor(RAMBOLL_CYAN)
    c.rect(0, PAGE_H - HEADER_H - 1.2, PAGE_W, 1.2, stroke=0, fill=1)

    # "R" mark in a white circle
    cx, cy, r = MARGIN_X + 8 * mm, PAGE_H - HEADER_H / 2, 7 * mm
    c.setFillColor(colors.white)
    c.circle(cx, cy, r, stroke=0, fill=1)
    c.setFillColor(RAMBOLL_BLUE)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(cx, cy - 5, "R")

    # Brand text
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(cx + r + 6, cy + 2, "RAMBOLL  ·  CalcVault")
    c.setFont("Helvetica", 9)
    c.drawString(cx + r + 6, cy - 9, "Engineering Calculation Report")

    # Right-hand meta
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(PAGE_W - MARGIN_X, cy + 4, title)
    c.setFont("Helvetica", 8)
    if subtitle:
        c.drawRightString(PAGE_W - MARGIN_X, cy - 6, subtitle)

    # ------ Footer -----------------------------------------------------
    c.setStrokeColor(CARD_LINE)
    c.setLineWidth(0.4)
    c.line(MARGIN_X, FOOTER_H, PAGE_W - MARGIN_X, FOOTER_H)

    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 8)
    c.drawString(MARGIN_X, FOOTER_H - 8,
                 f"Report {report_id}   ·   Generated by {user_name}")
    c.drawCentredString(PAGE_W / 2, FOOTER_H - 8,
                        run_at.strftime("%Y-%m-%d %H:%M"))
    c.drawRightString(PAGE_W - MARGIN_X, FOOTER_H - 8,
                      f"Page {doc.page}")

    c.restoreState()


# ===============================================================
# Table builders
# ===============================================================
def _kv_table(rows: List[Dict[str, Any]], highlight_primary: bool = False) -> Table:
    """
    3-column table: Label | Value | Unit
    Highlights row where row.get('primary') is True (results table).
    """
    data = [[
        Paragraph("<b>Parameter</b>", STYLE_CELL),
        Paragraph("<b>Value</b>",     STYLE_CELL),
        Paragraph("<b>Unit</b>",      STYLE_CELL),
    ]]
    for r in rows:
        data.append([
            Paragraph(r.get("label", ""), STYLE_CELL),
            Paragraph(f"<b>{_fmt_value(r.get('value'))}</b>", STYLE_CELLB),
            Paragraph(r.get("unit", "") or "", STYLE_CELL),
        ])

    tbl = Table(
        data,
        colWidths=[95 * mm, 45 * mm, 38 * mm],
        hAlign="LEFT",
    )
    style = [
        ("BACKGROUND",  (0, 0), (-1, 0), RAMBOLL_BLUE),
        ("TEXTCOLOR",   (0, 0), (-1, 0), colors.white),
        ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BOX",         (0, 0), (-1, -1), 0.5, CARD_LINE),
        ("INNERGRID",   (0, 0), (-1, -1), 0.3, CARD_LINE),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",  (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING",(0, 0), (-1, -1), 7),
    ]
    # Zebra + primary highlight
    for i, r in enumerate(rows, start=1):
        if highlight_primary and r.get("primary"):
            style.append(("BACKGROUND", (0, i), (-1, i), GOOD_BG))
            style.append(("LINEBEFORE", (0, i), (0, i), 3, RAMBOLL_CYAN))
        elif i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), CARD_BG))
    tbl.setStyle(TableStyle(style))
    return tbl


def _formula_block(formula: str) -> Table:
    p = Paragraph(prettify_formula(formula), STYLE_FORM)
    tbl = Table([[p]], colWidths=[178 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), CARD_BG),
        ("BOX",          (0, 0), (-1, -1), 0.5, CARD_LINE),
        ("LINEBEFORE",   (0, 0), (0, -1),  3,   RAMBOLL_BLUE),
        ("LEFTPADDING",  (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING",   (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 8),
    ]))
    return tbl


# ===============================================================
# Report ID
# ===============================================================
def _make_report_id(run_at: datetime) -> str:
    return (f"RPT-{run_at.strftime('%Y%m%d-%H%M%S')}-"
            f"{random.randint(0, 0xFFFF):04X}")


# ===============================================================
# Main entry
# ===============================================================
def generate_report(
    *,
    module_name : str,
    module_icon : str = "",
    inputs      : List[Dict[str, Any]],
    results     : List[Dict[str, Any]],
    formula     : Optional[str] = None,
    notes       : Optional[str] = None,
    user_name   : str = "—",
    run_at      : Optional[datetime] = None,
) -> Tuple[bytes, str]:
    """
    Build the PDF and return (bytes, report_id).
    """
    run_at    = run_at or datetime.now()
    report_id = _make_report_id(run_at)

    buf = io.BytesIO()
    doc = BaseDocTemplate(
        buf,
        pagesize     = A4,
        leftMargin   = MARGIN_X,
        rightMargin  = MARGIN_X,
        topMargin    = HEADER_H + 4 * mm,
        bottomMargin = FOOTER_H + 4 * mm,
        title        = f"{module_name} — CalcVault Report",
        author       = "Ramboll CalcVault",
    )

    body_frame = Frame(
        MARGIN_X, BODY_BOTTOM,
        PAGE_W - 2 * MARGIN_X, BODY_H,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="body",
    )

    subtitle = f"{report_id}   ·   {run_at.strftime('%Y-%m-%d %H:%M')}"
    tpl = PageTemplate(
        id="main",
        frames=[body_frame],
        onPage=lambda c, d: _draw_chrome(
            c, d,
            title     = "Calculation Report",
            subtitle  = subtitle,
            report_id = report_id,
            user_name = user_name,
            run_at    = run_at,
        ),
    )
    doc.addPageTemplates([tpl])

    # ------------------ Body flowables --------------------------
    story: List[Any] = []

    title_line = (f"{module_icon}  {module_name}".strip()
                  if module_icon else module_name)
    story.append(Paragraph(title_line, STYLE_H1))
    story.append(Paragraph(
        f"Run on {run_at.strftime('%A, %d %B %Y at %H:%M')} by "
        f"<b>{user_name}</b>.",
        STYLE_MUTE))

    if formula:
        story.append(Paragraph("Formula", STYLE_H2))
        story.append(_formula_block(formula))

    story.append(Paragraph("Inputs", STYLE_H2))
    if inputs:
        story.append(_kv_table(inputs))
    else:
        story.append(Paragraph("<i>No inputs recorded.</i>", STYLE_MUTE))

    story.append(Paragraph("Results", STYLE_H2))
    if results:
        # Keep the results table together on one page whenever possible
        story.append(KeepTogether(_kv_table(results, highlight_primary=True)))
    else:
        story.append(Paragraph("<i>No results.</i>", STYLE_MUTE))

    if notes:
        story.append(Spacer(1, 6))
        story.append(Paragraph("Notes", STYLE_H2))
        story.append(Paragraph(notes.replace("\n", "<br/>"), STYLE_NOTE))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "This report was generated by Ramboll CalcVault — internal use only. "
        "Formulas and reference data reflect standard hydraulic practice "
        "(CPHEEO, Hazen-Williams, Manning). Verify against project-specific "
        "codes and site conditions before construction.",
        STYLE_NOTE))

    doc.build(story)
    return buf.getvalue(), report_id


# ===============================================================
# Local smoke test  →  python pdf_generator.py
# ===============================================================
if __name__ == "__main__":
    pdf, rid = generate_report(
        module_name = "Pipe Head Loss (Hazen-Williams)",
        module_icon = "💧",
        inputs = [
            {"label": "Flow (Q)",     "value": 212,   "unit": "m³/hr"},
            {"label": "C-factor",     "value": 140,   "unit": "—"},
            {"label": "Pipe ID",      "value": 210.1, "unit": "mm"},
            {"label": "Pipe length",  "value": 131,   "unit": "m"},
        ],
        results = [
            {"label": "Head loss (H)",       "value": 1.5657, "unit": "m",
             "primary": True},
            {"label": "Gradient (j)",        "value": 0.01195,"unit": "m/m"},
            {"label": "Velocity in pipe",    "value": 1.700,  "unit": "m/s"},
            {"label": "Cross-sectional area","value": 0.0347, "unit": "m²"},
        ],
        formula = "H = (Q / (1000.8 · C · d_h^2.63))^1.852 · L",
        notes   = ("C-factor selected from Ramboll reference table.\n"
                   "d_h expressed in metres."),
        user_name = "Test Engineer",
    )
    out = f"_test_report_{rid}.pdf"
    with open(out, "wb") as f:
        f.write(pdf)
    print(f"✅ PDF written: {out}  ({len(pdf):,} bytes)")