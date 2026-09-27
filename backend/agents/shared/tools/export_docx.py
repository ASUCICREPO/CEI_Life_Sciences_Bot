from __future__ import annotations

import textwrap


def docx_code(title: str, sections_json: str, charts_json: str) -> str:
    """Return a Python script that generates a richly formatted DOCX via python-docx."""
    header = (
        "import json\n"
        f"title = {title!r}\n"
        f"sections = json.loads({sections_json!r})\n"
        f"charts_b64 = json.loads({charts_json!r})\n"
    )
    body = textwrap.dedent("""\
        import json, base64, io, re
        from docx import Document
        from docx.shared import Inches, Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement

        C_TITLE = RGBColor(0x1a, 0x3a, 0x5c)
        C_H1    = RGBColor(0x1a, 0x3a, 0x5c)
        C_H2    = RGBColor(0x25, 0x63, 0xa8)
        C_H3    = RGBColor(0x0e, 0x74, 0x90)
        C_H4    = RGBColor(0x47, 0x55, 0x69)

        doc = Document()

        def set_heading_color(para, color):
            for run in para.runs:
                run.font.color.rgb = color

        def add_hr(doc):
            p = doc.add_paragraph()
            pPr = p._p.get_or_add_pPr()
            pBdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "6")
            bottom.set(qn("w:space"), "1")
            bottom.set(qn("w:color"), "CBD5E1")
            pBdr.append(bottom)
            pPr.append(pBdr)
            return p

        TOKEN_RE = re.compile(
            r"([*]{3}(?P<bi>.+?)[*]{3}"
            r"|[*]{2}(?P<b>.+?)[*]{2}"
            r"|[*](?P<i>.+?)[*]"
            r"|___(?P<bi2>.+?)___"
            r"|__(?P<b2>.+?)__"
            r"|_(?P<i2>.+?)_"
            r"|~~(?P<u>.+?)~~"
            r"|`(?P<code>.+?)`"
            r"|(?P<plain>[^*_~`]+)"
            r")"
        )

        def parse_inline(para, text):
            for m in TOKEN_RE.finditer(text):
                run = para.add_run()
                if m.group("bi") or m.group("bi2"):
                    run.text = m.group("bi") or m.group("bi2")
                    run.bold = True
                    run.italic = True
                elif m.group("b") or m.group("b2"):
                    run.text = m.group("b") or m.group("b2")
                    run.bold = True
                elif m.group("i") or m.group("i2"):
                    run.text = m.group("i") or m.group("i2")
                    run.italic = True
                elif m.group("u"):
                    run.text = m.group("u")
                    run.underline = True
                elif m.group("code"):
                    run.text = m.group("code")
                    run.font.name = "Courier New"
                    run.font.size = Pt(10)
                elif m.group("plain"):
                    run.text = m.group("plain")

        HEADING_RE = re.compile(r"^(#{1,6})\\s+(.+)$")

        def add_content_line(doc, line):
            stripped = line.strip()
            if not stripped:
                return
            if stripped in ("---", "***", "___"):
                add_hr(doc)
                return
            m = HEADING_RE.match(stripped)
            if m:
                level = min(len(m.group(1)), 4)
                para = doc.add_heading("", level=level)
                parse_inline(para, m.group(2))
                colors = {1: C_H1, 2: C_H2, 3: C_H3, 4: C_H4}
                set_heading_color(para, colors.get(level, C_H4))
                return
            if re.match(r"^[-*+]\\s+", stripped):
                para = doc.add_paragraph(style="List Bullet")
                parse_inline(para, stripped[2:].strip())
                return
            if re.match(r"^\\d+[.]\\s+", stripped):
                para = doc.add_paragraph(style="List Number")
                parse_inline(para, re.sub(r"^\\d+[.]\\s+", "", stripped))
                return
            para = doc.add_paragraph()
            para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            parse_inline(para, stripped)

        title_para = doc.add_heading(title, level=0)
        title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_heading_color(title_para, C_TITLE)
        add_hr(doc)

        for sec in sorted(sections, key=lambda s: s.get("order", 0)):
            if sec.get("heading"):
                h = doc.add_heading("", level=2)
                parse_inline(h, sec["heading"])
                set_heading_color(h, C_H2)
            content = sec.get("content", "")
            for line in content.splitlines():
                add_content_line(doc, line)
            for chart_title in sec.get("charts", []):
                b64 = charts_b64.get(chart_title)
                if b64:
                    img_buf = io.BytesIO(base64.b64decode(b64))
                    doc.add_picture(img_buf, width=Inches(5.5))
                    cap = doc.add_paragraph(chart_title)
                    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    for run in cap.runs:
                        run.italic = True
                        run.font.color.rgb = RGBColor(0x64, 0x74, 0x8b)

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        print("DOCX_B64:" + base64.b64encode(buf.read()).decode())
    """)
    return header + body
