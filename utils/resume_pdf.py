"""ATS-safe PDF renderer for per-job tailored resumes.

The renderer uses a plain single-column layout with standard fonts, no tables,
no icons, and no graphics so common ATS parsers can read the document cleanly.
Generated PDFs are written to generated_resumes/, which is gitignored.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)


def _safe_name(value: str, fallback: str = "resume") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("_")
    return value[:80] or fallback


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _styles():
    base = getSampleStyleSheet()
    return {
        "name": ParagraphStyle(
            "KDName",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=15,
            leading=17,
            spaceAfter=2,
            alignment=TA_LEFT,
        ),
        "headline": ParagraphStyle(
            "KDHeadline",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9.5,
            leading=11,
            spaceAfter=3,
        ),
        "contact": ParagraphStyle(
            "KDContact",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=10,
            spaceAfter=7,
        ),
        "section": ParagraphStyle(
            "KDSection",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9.5,
            leading=11,
            spaceBefore=6,
            spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "KDBody",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.7,
            leading=11.2,
            spaceAfter=3,
        ),
        "job": ParagraphStyle(
            "KDJob",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=11,
            spaceBefore=3,
            spaceAfter=1,
        ),
        "meta": ParagraphStyle(
            "KDMeta",
            parent=base["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=8.2,
            leading=10,
            spaceAfter=2,
        ),
        "bullet": ParagraphStyle(
            "KDBullet",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=10.8,
            leftIndent=10,
            firstLineIndent=0,
            spaceAfter=1.5,
        ),
    }


def render_tailored_resume_pdf(
    tailored: dict,
    profile: dict,
    job: dict,
    base_resume_text: str = "",
    output_dir: str = "generated_resumes",
) -> str:
    """Render a tailored ATS resume and return its local PDF path."""
    personal = profile.get("personal", {})
    full = tailored.get("full_resume") or {}

    company = _safe_name(job.get("company", "company"), "company")
    title = _safe_name(job.get("title", "role"), "role")
    job_id = _safe_name(str(job.get("id", "job")), "job")[:24]
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{company}_{title}_{job_id}.pdf"

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER,
        leftMargin=0.55 * inch,
        rightMargin=0.55 * inch,
        topMargin=0.48 * inch,
        bottomMargin=0.48 * inch,
        title=f"Tailored Resume - {job.get('company', '')} - {job.get('title', '')}",
        author=f"{personal.get('first_name', '')} {personal.get('last_name', '')}".strip(),
    )
    s = _styles()
    story = []

    name = " ".join(
        part for part in [personal.get("first_name", ""), personal.get("last_name", "")] if part
    ).strip()
    display_name = personal.get("display_name") or name
    if display_name:
        story.append(Paragraph(_clean(display_name).upper(), s["name"]))

    headline = full.get("headline") or " | ".join(
        profile.get("preferences", {}).get("roles", [])[:4]
    )
    if headline:
        story.append(Paragraph(_clean(headline), s["headline"]))

    contact_parts = [
        personal.get("location", ""),
        personal.get("phone", ""),
        personal.get("email", ""),
        personal.get("linkedin", ""),
        personal.get("portfolio", ""),
    ]
    contact = " | ".join(str(x).strip() for x in contact_parts if str(x or "").strip())
    if contact:
        story.append(Paragraph(_clean(contact), s["contact"]))

    summary = full.get("summary") or tailored.get("tailored_summary", "")
    if summary:
        story.append(Paragraph("PROFESSIONAL SUMMARY", s["section"]))
        story.append(Paragraph(_clean(summary), s["body"]))

    skills = full.get("skills") or tailored.get("keywords_to_include") or []
    if skills:
        story.append(Paragraph("CORE EXPERTISE", s["section"]))
        story.append(Paragraph(_clean(" | ".join(str(x) for x in skills[:24])), s["body"]))

    experience = full.get("experience") or []
    if experience:
        story.append(Paragraph("PROFESSIONAL EXPERIENCE", s["section"]))
        for item in experience:
            if not isinstance(item, dict):
                continue
            title_text = item.get("title", "")
            company_text = item.get("company", "")
            heading = " - ".join(x for x in [title_text, company_text] if x)
            meta = " | ".join(
                x for x in [item.get("location", ""), item.get("dates", "")] if x
            )
            block = []
            if heading:
                block.append(Paragraph(_clean(heading), s["job"]))
            if meta:
                block.append(Paragraph(_clean(meta), s["meta"]))
            bullets = [str(b).strip() for b in (item.get("bullets") or []) if str(b).strip()]
            if bullets:
                block.append(
                    ListFlowable(
                        [
                            ListItem(Paragraph(_clean(b), s["bullet"]), leftIndent=10)
                            for b in bullets[:7]
                        ],
                        bulletType="bullet",
                        start="circle",
                        leftIndent=8,
                        bulletFontName="Helvetica",
                        bulletFontSize=5,
                        bulletOffsetY=1,
                        spaceAfter=2,
                    )
                )
            story.append(KeepTogether(block))

    # If the LLM could not produce structured experience, still produce a useful
    # ATS document instead of silently falling back to the untailored PDF.
    if not experience:
        highlights = tailored.get("tailored_bullets") or []
        if highlights:
            story.append(Paragraph("RELEVANT EXPERIENCE HIGHLIGHTS", s["section"]))
            story.append(
                ListFlowable(
                    [ListItem(Paragraph(_clean(b), s["bullet"]), leftIndent=10) for b in highlights[:8]],
                    bulletType="bullet",
                    leftIndent=8,
                    bulletFontSize=5,
                )
            )
        if base_resume_text:
            story.append(Paragraph("ADDITIONAL CAREER HISTORY", s["section"]))
            # Preserve content for ATS parsing, but cap fallback length to keep the
            # document usable if source extraction is messy.
            chunks = [x.strip() for x in re.split(r"\n{2,}", base_resume_text[:10000]) if x.strip()]
            for chunk in chunks:
                story.append(Paragraph(_clean(chunk).replace("\n", "<br/>"), s["body"]))

    education = full.get("education") or []
    certifications = full.get("certifications") or []
    if education:
        story.append(Paragraph("EDUCATION", s["section"]))
        for item in education:
            if isinstance(item, dict):
                text = " | ".join(
                    x for x in [item.get("school", ""), item.get("degree", ""), item.get("date", "")] if x
                )
            else:
                text = str(item)
            if text:
                story.append(Paragraph(_clean(text), s["body"]))

    if certifications:
        story.append(Paragraph("CERTIFICATIONS", s["section"]))
        for item in certifications:
            if isinstance(item, dict):
                text = " | ".join(
                    x for x in [item.get("name", ""), item.get("issuer", ""), item.get("date", "")] if x
                )
            else:
                text = str(item)
            if text:
                story.append(Paragraph(_clean(text), s["body"]))

    additional = full.get("additional") or []
    if additional:
        story.append(Paragraph("ADDITIONAL", s["section"]))
        for item in additional:
            story.append(Paragraph(_clean(item), s["body"]))

    story.append(Spacer(1, 2))
    doc.build(story)
    return str(output_path)
