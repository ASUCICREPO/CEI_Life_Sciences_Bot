from __future__ import annotations

import textwrap


def pdf_code(title: str, sections_json: str, charts_json: str) -> str:
    """Return a Python script that generates a richly formatted PDF via reportlab."""
    header = (
        "import json\n"
        f"title = {title!r}\n"
        f"sections = json.loads({sections_json!r})\n"
        f"charts_b64 = json.loads({charts_json!r})\n"
    )
    body = textwrap.dedent("""\
        import json, base64, io, re
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.lib.colors import HexColor
        from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Image as RLImage,
            HRFlowable, ListFlowable, ListItem, KeepTogether,
        )

        C_TITLE   = HexColor("#1a3a5c")
        C_H2      = HexColor("#2563a8")
        C_H3      = HexColor("#0e7490")
        C_H4      = HexColor("#475569")
        C_BODY    = HexColor("#1e293b")
        C_CAPTION = HexColor("#64748b")
        C_HR      = HexColor("#cbd5e1")

        base = getSampleStyleSheet()

        def make(name, parent="Normal", **kw):
            return ParagraphStyle(name, parent=base[parent], **kw)

        S = {
            "title":   make("DocTitle",  "Title",
                            fontSize=22, textColor=C_TITLE, spaceAfter=14,
                            alignment=TA_CENTER, fontName="Helvetica-Bold"),
            "h1":      make("H1",        "Heading1",
                            fontSize=18, textColor=C_TITLE, spaceBefore=16, spaceAfter=6,
                            fontName="Helvetica-Bold", alignment=TA_LEFT),
            "h2":      make("H2",        "Heading2",
                            fontSize=15, textColor=C_H2, spaceBefore=12, spaceAfter=4,
                            fontName="Helvetica-Bold", alignment=TA_LEFT),
            "h3":      make("H3",        "Heading3",
                            fontSize=13, textColor=C_H3, spaceBefore=10, spaceAfter=3,
                            fontName="Helvetica-BoldOblique", alignment=TA_LEFT),
            "h4":      make("H4",        "Normal",
                            fontSize=11, textColor=C_H4, spaceBefore=8, spaceAfter=2,
                            fontName="Helvetica-Oblique", alignment=TA_LEFT),
            "body":    make("Body",      "Normal",
                            fontSize=11, textColor=C_BODY, leading=16,
                            alignment=TA_JUSTIFY, fontName="Helvetica"),
            "bullet":  make("Bullet",    "Normal",
                            fontSize=11, textColor=C_BODY, leading=15,
                            leftIndent=18, bulletIndent=6, fontName="Helvetica"),
            "caption": make("Caption",   "Normal",
                            fontSize=9, textColor=C_CAPTION, alignment=TA_CENTER,
                            spaceAfter=6, fontName="Helvetica-Oblique"),
        }

        def md_inline(text):
            text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            text = re.sub(r"[*][*][*](.+?)[*][*][*]", r"<b><i>\\1</i></b>", text)
            text = re.sub(r"___(.+?)___",              r"<b><i>\\1</i></b>", text)
            text = re.sub(r"[*][*](.+?)[*][*]",       r"<b>\\1</b>", text)
            text = re.sub(r"__(.+?)__",                r"<b>\\1</b>", text)
            text = re.sub(r"[*](.+?)[*]",              r"<i>\\1</i>", text)
            text = re.sub(r"_(.+?)_",                  r"<i>\\1</i>", text)
            text = re.sub(r"~~(.+?)~~",                r"<u>\\1</u>", text)
            text = re.sub(r"`(.+?)`", r"<font name='Courier'>\\1</font>", text)
            return text

        HEADING_RE = re.compile(r"^(#{1,6})\\s+(.+)$")

        def classify(line):
            m = HEADING_RE.match(line)
            if m:
                level = len(m.group(1))
                key = {1: "h1", 2: "h2", 3: "h3"}.get(level, "h4")
                return key, md_inline(m.group(2))
            if line.strip() in ("---", "***", "___"):
                return "hr", None
            if re.match(r"^[-*+]\\s+", line):
                return "bullet", md_inline(line[2:].strip())
            if re.match(r"^\\d+[.]\\s+", line):
                return "numbered", md_inline(re.sub(r"^\\d+[.]\\s+", "", line))
            return "body", md_inline(line)

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=letter,
            rightMargin=inch, leftMargin=inch,
            topMargin=inch, bottomMargin=inch,
        )
        story = []

        story.append(Paragraph(title, S["title"]))
        story.append(HRFlowable(width="100%", thickness=1.5, color=C_H2, spaceAfter=16))

        for sec in sorted(sections, key=lambda s: s.get("order", 0)):
            if sec.get("heading"):
                story.append(Paragraph(md_inline(sec["heading"]), S["h2"]))

            content = sec.get("content", "")
            bullet_items = []
            numbered_items = []

            def flush_lists():
                global bullet_items, numbered_items
                if bullet_items:
                    story.append(ListFlowable(
                        [ListItem(Paragraph(t, S["bullet"]), bulletColor=C_H2) for t in bullet_items],
                        bulletType="bullet", leftIndent=18, spaceBefore=4, spaceAfter=4,
                    ))
                    bullet_items = []
                if numbered_items:
                    story.append(ListFlowable(
                        [ListItem(Paragraph(t, S["bullet"])) for t in numbered_items],
                        bulletType="1", leftIndent=18, spaceBefore=4, spaceAfter=4,
                    ))
                    numbered_items = []

            for line in content.splitlines():
                stripped = line.strip()
                if not stripped:
                    flush_lists()
                    story.append(Spacer(1, 0.08 * inch))
                    continue
                kind, text = classify(stripped)
                if kind == "hr":
                    flush_lists()
                    story.append(HRFlowable(width="100%", thickness=0.5, color=C_HR,
                                            spaceBefore=6, spaceAfter=6))
                elif kind == "bullet":
                    if numbered_items:
                        flush_lists()
                    bullet_items.append(text)
                elif kind == "numbered":
                    if bullet_items:
                        flush_lists()
                    numbered_items.append(text)
                else:
                    flush_lists()
                    story.append(Paragraph(text, S.get(kind, S["body"])))
                    story.append(Spacer(1, 0.04 * inch))

            flush_lists()

            for chart_title in sec.get("charts", []):
                b64 = charts_b64.get(chart_title)
                if b64:
                    img_buf = io.BytesIO(base64.b64decode(b64))
                    story.append(KeepTogether([
                        RLImage(img_buf, width=5.5 * inch, height=3.3 * inch),
                        Paragraph(chart_title, S["caption"]),
                        Spacer(1, 0.1 * inch),
                    ]))

        doc.build(story)
        buf.seek(0)
        print("PDF_B64:" + base64.b64encode(buf.read()).decode())
    """)
    return header + body
