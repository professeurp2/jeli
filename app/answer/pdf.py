"""PDF files Jeli makes (translated documents): a clean A4 layout with headings, paragraphs,
bullet lists and page numbers. Uses the Bitstream Vera fonts shipped with reportlab, which cover
English, French and the other Latin-script languages Jeli translates into."""

import io
import os
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

FONTS = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
_registered = False


def _fonts() -> None:
    global _registered
    if _registered:
        return
    for name, file in (("Vera", "Vera.ttf"), ("Vera-Bold", "VeraBd.ttf"), ("Vera-Italic", "VeraIt.ttf")):
        pdfmetrics.registerFont(TTFont(name, os.path.join(FONTS, file)))
    pdfmetrics.registerFontFamily("Vera", normal="Vera", bold="Vera-Bold", italic="Vera-Italic", boldItalic="Vera-Bold")
    _registered = True


def _clean(text: str) -> str:
    """Escape for reportlab's markup, and drop what the font cannot draw (emoji)."""
    return escape("".join(ch for ch in text if ord(ch) < 0x2FFF or ch in "€"))


INK = colors.HexColor("#1b1c1f")
MUTED = colors.HexColor("#6b6d74")
ACCENT = colors.HexColor("#b07d00")


def build_pdf(title: str, note: str, blocks: list[tuple[str, str]]) -> bytes:
    """blocks: (kind, text) with kind "heading", "paragraph" or "bullet", in order."""
    _fonts()
    styles = {
        "title": ParagraphStyle("title", fontName="Vera-Bold", fontSize=18, leading=23, textColor=INK, spaceAfter=6),
        "note": ParagraphStyle("note", fontName="Vera-Italic", fontSize=9, leading=12, textColor=MUTED, spaceAfter=14),
        "heading": ParagraphStyle("heading", fontName="Vera-Bold", fontSize=12.5, leading=16, textColor=ACCENT, spaceBefore=10, spaceAfter=4),
        "paragraph": ParagraphStyle("paragraph", fontName="Vera", fontSize=10.5, leading=15, textColor=INK, spaceAfter=6, alignment=TA_LEFT),
    }
    story = [Paragraph(_clean(title), styles["title"]), Paragraph(_clean(note), styles["note"])]
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            items = [ListItem(Paragraph(_clean(text), styles["paragraph"]), leftIndent=14) for text in bullets]
            story.append(ListFlowable(items, bulletType="bullet", start="•", leftIndent=14, bulletFontName="Vera"))
            bullets.clear()

    for kind, text in blocks:
        if not text.strip():
            continue
        if kind == "bullet":
            bullets.append(text)
            continue
        flush()
        story.append(Paragraph(_clean(text), styles["heading" if kind == "heading" else "paragraph"]))
    flush()

    def footer(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont("Vera", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(2 * cm, 1.2 * cm, _plain(title)[:90])
        canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"{document.page}")
        canvas.restoreState()

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
        title=_plain(title), author="Jeli",
    )
    story.insert(2, Spacer(1, 2))
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def _plain(text: str) -> str:
    return "".join(ch for ch in text if ord(ch) < 0x2FFF)
