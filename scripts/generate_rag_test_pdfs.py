"""Generate deterministic, text-extractable Chinese PDFs for the RAG test corpus."""
from __future__ import annotations

import html
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "docs" / "rag_sources"
OUTPUT_DIR = ROOT / "data" / "documents"
FONT_PATH = Path("/usr/share/fonts/truetype/arphic/uming.ttc")
FONT_NAME = "HommeyCJK"
REQUIRED_GLYPHS = "案例32：台风，员工“酒店600元”未知/FAQ（）%+-.ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

DOCUMENTS = {
    "00_policy_authority_2026.txt": "00_policy_authority_2026.pdf",
    "09_international_travel_policy_2026.txt": "09_international_travel_policy_2026.pdf",
    "10_exception_approval_2026.txt": "10_exception_approval_2026.pdf",
    "11_rag_validation_scenarios_2026.txt": "11_rag_validation_scenarios_2026.pdf",
}

HEADING_RE = re.compile(r"^(?:[一二三四五六七八九十]+、|附录|案例\d+[:：])")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "CJKTitle",
            parent=base["Title"],
            fontName=FONT_NAME,
            fontSize=19,
            leading=27,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#17365D"),
            spaceAfter=8 * mm,
            wordWrap="CJK",
        ),
        "heading": ParagraphStyle(
            "CJKHeading",
            parent=base["Heading2"],
            fontName=FONT_NAME,
            fontSize=13,
            leading=19,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#1F4E79"),
            spaceBefore=4 * mm,
            spaceAfter=2 * mm,
            wordWrap="CJK",
        ),
        "body": ParagraphStyle(
            "CJKBody",
            parent=base["BodyText"],
            fontName=FONT_NAME,
            fontSize=10.5,
            leading=17,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#222222"),
            spaceAfter=2.2 * mm,
            wordWrap="CJK",
        ),
        "bullet": ParagraphStyle(
            "CJKBullet",
            parent=base["BodyText"],
            fontName=FONT_NAME,
            fontSize=10.5,
            leading=17,
            leftIndent=6 * mm,
            firstLineIndent=-3 * mm,
            spaceAfter=1.5 * mm,
            wordWrap="CJK",
        ),
        "meta": ParagraphStyle(
            "CJKMeta",
            parent=base["BodyText"],
            fontName=FONT_NAME,
            fontSize=9,
            leading=14,
            textColor=colors.HexColor("#555555"),
            spaceAfter=1 * mm,
            wordWrap="CJK",
        ),
    }


def _story(text: str):
    styles = _styles()
    story = []
    first_content = True
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "---PAGE---":
            story.append(PageBreak())
            continue
        if not line:
            story.append(Spacer(1, 1.5 * mm))
            continue
        safe = html.escape(line)
        if first_content:
            story.append(Paragraph(safe, styles["title"]))
            first_content = False
        elif HEADING_RE.match(line):
            story.append(Paragraph(safe, styles["heading"]))
        elif line.startswith("- "):
            story.append(Paragraph(html.escape(line[2:]), styles["bullet"], bulletText="•"))
        elif line.startswith(("文档编号：", "版本：", "生效日期：", "适用范围：", "制度归口：", "制度层级：", "用途：", "说明：")):
            story.append(Paragraph(safe, styles["meta"]))
        elif line.startswith("【"):
            story.append(Paragraph(f"<b>{safe}</b>", styles["body"]))
        else:
            story.append(Paragraph(safe, styles["body"]))
    return story


def generate(source_name: str, output_name: str) -> Path:
    source = SOURCE_DIR / source_name
    output = OUTPUT_DIR / output_name
    text = source.read_text(encoding="utf-8").strip()
    title = text.splitlines()[0].strip()
    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=20 * mm,
        title=title,
        author="Hommey RAG Test Corpus",
        subject="Business travel policy test data",
    )
    # The RAG parser uses the first extracted line as the page title.  Avoid
    # canvas headers/footers because ReportLab writes them before flowables in
    # the PDF content stream, which would corrupt that title metadata.
    doc.build(_story(text))
    return output


def main() -> None:
    if not FONT_PATH.is_file():
        raise SystemExit(f"Chinese font not found: {FONT_PATH}")
    font = TTFont(FONT_NAME, str(FONT_PATH), subfontIndex=0)
    missing = [char for char in REQUIRED_GLYPHS if ord(char) not in font.face.charWidths]
    if missing:
        raise SystemExit(f"PDF font is missing required glyphs: {''.join(dict.fromkeys(missing))}")
    pdfmetrics.registerFont(font)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for source_name, output_name in DOCUMENTS.items():
        output = generate(source_name, output_name)
        print(f"generated {output.relative_to(ROOT)} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
