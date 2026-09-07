# -*- coding: utf-8 -*-
"""Convert docs/工程实践报告_ResearchPilot_框架.md into an editable .docx.

Pure-stdlib, offline OOXML writer (no python-docx required).
Conventions understood from the markdown source:

  <!-- COVER:START --> ... <!-- COVER:END -->  -> cover page block
      first line = main title (centered, large)
      lines "CENTER:..." -> centered paragraph
      other lines        -> left-aligned field lines
  # / ## / ### / #### headings  -> H1..H4 with literal numbering kept
  > blockquote lines            -> grey italic writing-guidance paragraphs
  | pipe tables                 -> real Word tables (2nd row "---" is header sep)
  - bullets                     -> bullet paragraphs
  ``` code fences               -> monospace grey-shaded paragraphs
  **bold** inline               -> bold runs
  <!-- ... -->                  -> skipped entirely (author notes)

Run:  python scripts/build_report_docx.py
Output: docs/工程实践报告_ResearchPilot.docx
"""
from __future__ import annotations

import sys
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "工程实践报告_ResearchPilot_框架.md"
OUT = ROOT / "docs" / "工程实践报告_ResearchPilot.docx"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NSDECL = (
    'xmlns:w="%s" xmlns:r="%s" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
    % (W, R)
)

ET.register_namespace("w", W)
ET.register_namespace("r", R)
ET.register_namespace("wp", "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing")
ET.register_namespace("a", "http://schemas.openxmlformats.org/drawingml/2006/main")
ET.register_namespace("pic", "http://schemas.openxmlformats.org/drawingml/2006/picture")


def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def split_bold(text: str):
    """Yield (text, bold) segments from **bold** markdown."""
    parts = text.split("**")
    for i, part in enumerate(parts):
        if part:
            yield part, (i % 2 == 1)


def run_xml(text: str, *, bold: bool = False, italic: bool = False, color: str = "000000",
            sz: int = 24, east: str = "宋体", ascii_f: str = "Times New Roman",
            mono: bool = False) -> str:
    rpr = ["<w:rFonts"]
    if mono:
        rpr.append(' w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas" w:eastAsia="%s"/>' % east)
    else:
        rpr.append(' w:ascii="%s" w:hAnsi="%s" w:eastAsia="%s"/>' % (ascii_f, ascii_f, east))
    if bold:
        rpr.append("<w:b/>")
    if italic:
        rpr.append("<w:i/>")
    rpr.append("<w:color w:val=\"%s\"/>" % color)
    rpr.append("<w:sz w:val=\"%d\"/><w:szCs w:val=\"%d\"/>" % (sz, sz))
    out = ["<w:r><w:rPr>", "".join(rpr), "</w:rPr>"]
    for txt, b in split_bold(text):
        run = "<w:r>"
        if b:
            run += "<w:rPr><w:b/><w:sz w:val=\"%d\"/><w:szCs w:val=\"%d\"/>" % (sz, sz)
            run += "<w:rFonts w:ascii=\"%s\" w:hAnsi=\"%s\" w:eastAsia=\"%s\"/>" % (ascii_f, ascii_f, east)
            run += "</w:rPr>"
        run += "<w:t xml:space=\"preserve\">%s</w:t></w:r>" % esc(txt)
        out.append(run)
    out.append("</w:r>")
    return "".join(out)


def para_xml(runs: str, *, jc: str | None = None, before: int = 0, after: int = 120,
             indent: dict | None = None, shade: str | None = None,
             page_break_after: bool = False, style: str | None = None,
             outline_lvl: int | None = None) -> str:
    ppr = ["<w:pPr>"]
    if style:
        ppr.append("<w:pStyle w:val=\"%s\"/>" % style)
    if outline_lvl is not None:
        ppr.append("<w:outlineLvl w:val=\"%d\"/>" % outline_lvl)
    if jc:
        ppr.append("<w:jc w:val=\"%s\"/>" % jc)
    ppr.append("<w:spacing w:before=\"%d\" w:after=\"%d\" w:line=\"300\" w:lineRule=\"auto\"/>" % (before, after))
    if indent:
        ppr.append("<w:ind %s/>" % " ".join('%s="%d"' % (k, v) for k, v in indent.items()))
    if shade:
        ppr.append("<w:shd w:val=\"clear\" w:color=\"auto\" w:fill=\"%s\"/>" % shade)
    ppr.append("<w:keepLines/>")
    ppr.append("</w:pPr>")
    xml = "<w:p>%s%s" % ("".join(ppr), runs)
    if page_break_after:
        xml += "<w:r><w:br w:type=\"page\"/></w:r>"
    xml += "</w:p>"
    return xml


def heading_xml(level: int, text: str) -> str:
    spec = {
        1: dict(sz=32, before=260, after=140, east="黑体"),
        2: dict(sz=28, before=200, after=100, east="黑体"),
        3: dict(sz=24, before=140, after=80, east="黑体"),
        4: dict(sz=24, before=100, after=60, east="黑体"),
    }[level]
    runs = run_xml(text, bold=True, color="000000", sz=spec["sz"], east=spec["east"])
    return para_xml(runs, before=spec["before"], after=spec["after"],
                    style="Heading%d" % level, outline_lvl=level - 1)


def guidance_xml(text: str) -> str:
    runs = run_xml(text, italic=True, color="808080", sz=21)
    return para_xml(runs, after=80, indent={"left": 240})


def bullet_xml(text: str) -> str:
    runs = run_xml("•  ", sz=24) + run_xml(text, sz=24)
    return para_xml(runs, after=60, indent={"left": 480, "hanging": 240})


def code_para_xml(text: str) -> str:
    runs = run_xml(text, sz=18, mono=True)
    return para_xml(runs, after=0, indent={"left": "420"}, shade="F2F2F2")


def cell_xml(text: str, header: bool = False, width: str = "auto") -> str:
    tcpr = "<w:tcPr><w:tcW w:w=\"0\" w:type=\"auto\"/><w:vAlign w:val=\"center\"/></w:tcPr>"
    if header:
        runs = run_xml(text, bold=True, sz=21)
        p = para_xml(runs, after=40, before=40, shade="D9D9D9")
    else:
        runs = run_xml(text, sz=21)
        p = para_xml(runs, after=40, before=40)
    return "<w:tc>%s%s</w:tc>" % (tcpr, p)


def table_xml(rows: list[list[str]]) -> str:
    border = ('<w:tblBorders>'
              '<w:top w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
              '<w:left w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
              '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
              '<w:right w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
              '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
              '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
              '</w:tblBorders>')
    tblpr = "<w:tblPr><w:tblW w:w=\"5000\" w:type=\"pct\"/>%s<w:tblLayout w:type=\"autofit\"/></w:tblPr>" % border
    out = ["<w:tbl>", tblpr]
    for idx, row in enumerate(rows):
        out.append("<w:tr>")
        for c in row:
            out.append(cell_xml(c, header=(idx == 0)))
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def is_table_sep(line: str) -> bool:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return all(set(c) <= set("-: ") and c for c in cells)


def build_body(lines: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip()
        stripped = line.strip()

        # html comments (single or multi line) -> skip
        if stripped.startswith("<!--"):
            if "-->" in stripped:
                i += 1
                continue
            i += 1
            while i < n and "-->" not in lines[i]:
                i += 1
            i += 1
            continue

        # headings
        if stripped.startswith("#### "):
            out.append(heading_xml(4, stripped[5:].strip())); i += 1; continue
        if stripped.startswith("### "):
            out.append(heading_xml(3, stripped[4:].strip())); i += 1; continue
        if stripped.startswith("## "):
            out.append(heading_xml(2, stripped[3:].strip())); i += 1; continue
        if stripped.startswith("# "):
            out.append(heading_xml(1, stripped[2:].strip())); i += 1; continue

        # code fence
        if stripped.startswith("```"):
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                out.append(code_para_xml(lines[i].rstrip() if lines[i].strip() else " "))
                i += 1
            i += 1
            continue

        # guidance
        if stripped.startswith(">"):
            txt = stripped.lstrip(">").strip()
            if txt:
                out.append(guidance_xml(txt))
            i += 1
            continue

        # tables
        if stripped.startswith("|"):
            rows: list[list[str]] = []
            while i < n and lines[i].strip().startswith("|"):
                row = lines[i].strip()
                if is_table_sep(row):
                    i += 1
                    continue
                cells = [c.strip() for c in row.strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            # skip a following blank line
            while i < n and not lines[i].strip():
                i += 1
            if rows:
                out.append(table_xml(rows))
                out.append(para_xml("", after=80))
            continue

        # blank line
        if not stripped:
            i += 1
            continue

        # bullets
        if stripped.startswith("- "):
            out.append(bullet_xml(stripped[2:].strip()))
            i += 1
            continue

        # regular paragraph (may wrap several lines until blank / new block)
        LIST_ITEM = re.compile(r"^(\d+\.|（\d+）|\(\d+\))\s")
        first_is_item = bool(LIST_ITEM.match(stripped))
        para_lines = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if not nxt or nxt.startswith("#") or nxt.startswith(">") or nxt.startswith("|") \
                    or nxt.startswith("- ") or nxt.startswith("```") or nxt.startswith("<!--") \
                    or LIST_ITEM.match(nxt):
                break
            para_lines.append(nxt)
            i += 1
        if first_is_item:
            out.append(para_xml(run_xml("".join(para_lines), sz=24), after=60,
                                indent={"left": 480, "hanging": 240}))
        else:
            out.append(para_xml(run_xml("".join(para_lines), sz=24), after=120))
    return out


def parse(lines: list[str]):
    cover: list[str] = []
    body: list[str] = []
    in_cover = False
    for ln in lines:
        s = ln.strip()
        if s == "<!-- COVER:START -->":
            in_cover = True
            continue
        if s == "<!-- COVER:END -->":
            in_cover = False
            continue
        if in_cover:
            cover.append(ln)
        else:
            body.append(ln)
    return cover, body


def build_cover(cover_lines: list[str]) -> str:
    items = [ln.rstrip() for ln in cover_lines if ln.strip()]
    if not items:
        return ""
    parts = [heading_cover(items[0])]
    for ln in items[1:]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("CENTER:"):
            parts.append(para_xml(run_xml(s[7:].strip(), sz=24, east="宋体"),
                                  jc="center", after=60))
        else:
            parts.append(para_xml(run_xml(s, sz=24, east="宋体"), after=200, indent={"left": 1560}))
    parts.append(para_xml("", after=0, page_break_after=True))
    return "".join(parts)


def heading_cover(text: str) -> str:
    runs = run_xml(text, bold=True, sz=44, east="黑体", ascii_f="Arial")
    return para_xml(runs, jc="center", before=1200, after=300)


def make_document_xml() -> str:
    lines = SRC.read_text(encoding="utf-8").splitlines()
    cover_lines, body_lines = parse(lines)

    body_xml: list[str] = []
    body_xml.append(build_cover(cover_lines))
    # TOC placeholder page
    body_xml.append(heading_xml(1, "目  录"))
    body_xml.append(guidance_xml("在 Word 中自动生成目录：点击此处后，使用菜单 引用 → 目录 → 自动目录；成稿前删除本灰色提示行。"))
    body_xml.append(para_xml("", after=0, page_break_after=True))
    body_xml.extend(build_body(body_lines))
    # trailing empty paragraph then section properties
    sect = ('<w:sectPr>'
            '<w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1440" w:right="1800" w:bottom="1440" w:left="1800" '
            'w:header="851" w:footer="992" w:gutter="0"/>'
            '<w:cols w:space="425"/>'
            '<w:docGrid w:type="lines" w:linePitch="312"/>'
            '</w:sectPr>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document %s>'
            '<w:body>%s%s</w:body></w:document>'
            % (NSDECL, "".join(body_xml), sect))


def styles_xml() -> str:
    def style(style_id: str, name: str, *, default: bool = False, outline: int | None = None,
              sz: int = 24, bold: bool = False, before: int = 0, after: int = 100,
              east: str = "宋体") -> str:
        p = ["<w:style w:type=\"paragraph\" w:styleId=\"%s\"%s>" % (style_id, ' w:default="1"' if default else "")]
        p.append("<w:name w:val=\"%s\"/>" % name)
        if not default:
            p.append("<w:basedOn w:val=\"Normal\"/><w:next w:val=\"Normal\"/><w:qFormat/>")
        if outline is not None:
            p.append("<w:uiPriority w:val=\"%d\"/>" % (9 - (outline or 0)))
        p.append("<w:pPr><w:spacing w:before=\"%d\" w:after=\"%d\" w:line=\"300\" w:lineRule=\"auto\"/>" % (before, after))
        if outline is not None:
            p.append("<w:outlineLvl w:val=\"%d\"/>" % outline)
        p.append("<w:keepNext/></w:pPr>")
        p.append("<w:rPr>")
        p.append('<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="%s"/>' % east)
        if bold:
            p.append("<w:b/>")
        p.append("<w:color w:val=\"000000\"/>")
        p.append("<w:sz w:val=\"%d\"/><w:szCs w:val=\"%d\"/></w:rPr>" % (sz, sz))
        p.append("</w:style>")
        return "".join(p)

    heads = {
        "Heading1": ("heading 1", 0, 32, 280, 160),
        "Heading2": ("heading 2", 1, 28, 220, 120),
        "Heading3": ("heading 3", 2, 24, 160, 90),
        "Heading4": ("heading 4", 3, 24, 120, 70),
    }
    body = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:styles xmlns:w="%s">' % W,
    ]
    body.append(style("Normal", "Normal", default=True, after=120))
    for sid, (name, lvl, sz, before, after) in heads.items():
        body.append(style(sid, name, outline=lvl, sz=sz, bold=True, before=before, after=after, east="黑体"))
    body.append("</w:styles>")
    return "".join(body)


def document_rels_xml() -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
            '</Relationships>')


def write_docx(doc_xml: str, styles: str) -> None:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        '</Relationships>'
    )
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/_rels/document.xml.rels", document_rels_xml())
        z.writestr("word/styles.xml", styles)
        z.writestr("word/document.xml", doc_xml)


def validate(xml_text: str) -> None:
    ET.fromstring(xml_text)  # raises if malformed


def main() -> None:
    if not SRC.exists():
        sys.exit("source markdown missing: %s" % SRC)
    doc = make_document_xml()
    validate(doc)
    st = styles_xml()
    validate(st)
    write_docx(doc, st)
    print("OK -> %s (%d bytes)" % (OUT, OUT.stat().st_size))


if __name__ == "__main__":
    main()
