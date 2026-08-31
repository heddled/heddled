"""Word, Excel and PowerPoint files, without a library to make them.

A model cannot emit a .docx — it emits text. So an assistant writes markdown or
rows, and this turns that into the file somebody actually wanted.

All three formats are zip archives of XML, which is why there is no dependency
here: `zipfile` and a handful of templates do it, and Heddled's five
third-party packages each earn their place with a comment. Adding four more so
an agent can write a report would be a poor trade — python-docx and python-pptx
both pull lxml, and python-pptx pulls Pillow as well.

What this makes is plain: headings, paragraphs, lists and tables in Word; a
sheet of rows in Excel; title-and-bullets slides in PowerPoint. Not a Word
feature clone, and it does not pretend to be — the point is that the person who
asked for a report gets something they can open and edit, rather than a .txt.

Reading is the other direction and mostly the same trick: .docx and .xlsx are
unzipped and their text pulled out with the stdlib XML parser. PDF is the one
that genuinely needs a library, so it is optional and says so when missing.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

#: What each extension is called when talking to a person.
KINDS = {
    ".docx": "Word document",
    ".xlsx": "Excel spreadsheet",
    ".pptx": "PowerPoint deck",
}

WRITABLE = tuple(KINDS)

#: Always readable — both are zip archives of XML, handled with the stdlib.
_ALWAYS_READABLE = (".docx", ".xlsx")


def readable_suffixes() -> tuple[str, ...]:
    """What can actually be read *here*, not what is understood in principle.

    PDF needs a package that may not be installed, and a listing that marks a
    file readable when opening it will refuse is worse than one that admits the
    limit — the operator finds out from the file list rather than from a failed
    turn.
    """
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return _ALWAYS_READABLE
    return _ALWAYS_READABLE + (".pdf",)


class DocumentError(ValueError):
    """Refused, in words the agent can act on."""


# --------------------------------------------------------------- markdown in

def _blocks(markdown: str) -> list[tuple[str, object]]:
    """The small subset of markdown that maps onto a document.

    Headings, paragraphs, bullets, numbers and pipe tables. Everything else is
    a paragraph, because a document that quietly drops a line is worse than one
    that renders it plainly.
    """
    out: list[tuple[str, object]] = []
    lines = str(markdown or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            out.append(("heading", (len(heading.group(1)), heading.group(2).strip())))
            i += 1
            continue

        # A pipe table, with its separator row skipped.
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                out.append(("table", rows))
            continue

        item = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)$", line)
        if item:
            ordered = not item.group(1) in ("-", "*", "+")
            items = []
            while i < len(lines):
                m = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)$", lines[i].rstrip())
                if not m:
                    break
                items.append(m.group(2).strip())
                i += 1
            out.append(("list", (ordered, items)))
            continue

        # A paragraph runs until a blank line.
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^(#{1,6}\s|\s*([-*+]|\d+[.)])\s|\s*\|)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(("para", " ".join(para)))
    return out


def _plain(text: str) -> str:
    """Strip the markers a document renders structurally rather than literally."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", str(text))
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)


def _zip(parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in parts.items():
            z.writestr(name, body)
    return buf.getvalue()


RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '{items}</Relationships>')


# ----------------------------------------------------------------- word

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(markdown: str) -> bytes:
    body = []
    for kind, value in _blocks(markdown):
        if kind == "heading":
            level, text = value
            body.append(
                f'<w:p><w:pPr><w:pStyle w:val="Heading{min(level, 6)}"/></w:pPr>'
                f'<w:r><w:t xml:space="preserve">{escape(_plain(text))}</w:t></w:r></w:p>')
        elif kind == "para":
            body.append(
                f'<w:p><w:r><w:t xml:space="preserve">{escape(_plain(value))}</w:t>'
                f'</w:r></w:p>')
        elif kind == "list":
            ordered, items = value
            for n, item in enumerate(items, 1):
                # A real numbering definition needs numbering.xml and a style
                # part; a bullet or a number in the text is honest, editable and
                # a fraction of the machinery.
                marker = f"{n}. " if ordered else "• "
                body.append(
                    f'<w:p><w:pPr><w:ind w:left="360"/></w:pPr><w:r>'
                    f'<w:t xml:space="preserve">{escape(marker + _plain(item))}</w:t>'
                    f'</w:r></w:p>')
        elif kind == "table":
            rows = []
            for r, cells in enumerate(value):
                tcs = []
                for cell in cells:
                    runs = (f'<w:r><w:rPr><w:b/></w:rPr>' if r == 0 else '<w:r>')
                    tcs.append(
                        f'<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>'
                        f'<w:p>{runs}<w:t xml:space="preserve">'
                        f'{escape(_plain(cell))}</w:t></w:r></w:p></w:tc>')
                rows.append(f"<w:tr>{''.join(tcs)}</w:tr>")
            borders = "".join(
                f'<w:{edge} w:val="single" w:sz="4" w:color="auto"/>'
                for edge in ("top", "left", "bottom", "right", "insideH", "insideV"))
            body.append(
                f'<w:tbl><w:tblPr><w:tblBorders>{borders}</w:tblBorders></w:tblPr>'
                f"{''.join(rows)}</w:tbl><w:p/>")

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>{"".join(body)}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
        '</w:sectPr></w:body></w:document>')

    # A default "Normal" is not optional: without one, a paragraph carrying no
    # explicit style has nothing to fall back to, and a reader resolves its
    # style to nothing at all.
    styles = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
              f'<w:styles xmlns:w="{W_NS}">',
              '<w:docDefaults><w:rPrDefault><w:rPr>'
              '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/>'
              '</w:rPr></w:rPrDefault>'
              '<w:pPrDefault><w:pPr><w:spacing w:after="120"/></w:pPr></w:pPrDefault>'
              '</w:docDefaults>',
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
              '<w:name w:val="Normal"/><w:qFormat/></w:style>']
    for level, size in ((1, 32), (2, 26), (3, 24), (4, 22), (5, 22), (6, 22)):
        styles.append(
            f'<w:style w:type="paragraph" w:styleId="Heading{level}">'
            f'<w:name w:val="heading {level}"/><w:basedOn w:val="Normal"/><w:qFormat/>'
            f'<w:pPr><w:outlineLvl w:val="{level - 1}"/>'
            f'<w:spacing w:before="240" w:after="120"/></w:pPr>'
            f'<w:rPr><w:b/><w:sz w:val="{size}"/></w:rPr></w:style>')
    styles.append("</w:styles>")

    return _zip({
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            '</Types>',
        "_rels/.rels": RELS.format(items=(
            '<Relationship Id="rId1" Target="word/document.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>')),
        "word/_rels/document.xml.rels": RELS.format(items=(
            '<Relationship Id="rId1" Target="styles.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"/>')),
        "word/document.xml": document,
        "word/styles.xml": "".join(styles),
    })


# ---------------------------------------------------------------- excel

S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _rows_from(content) -> list[list[str]]:
    """Rows, from a list of lists, a markdown table, or CSV text."""
    if isinstance(content, list):
        return [[("" if c is None else str(c)) for c in (r if isinstance(r, list) else [r])]
                for r in content]
    text = str(content or "")
    for kind, value in _blocks(text):
        if kind == "table":
            return [[_plain(c) for c in row] for row in value]
    import csv
    rows = list(csv.reader(io.StringIO(text)))
    return [r for r in rows if any(c.strip() for c in r)]


def _number(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _xlsx(content) -> bytes:
    rows = _rows_from(content)
    if not rows:
        raise DocumentError("there are no rows to put in a spreadsheet")

    body = []
    for r, cells in enumerate(rows, 1):
        parts = []
        for c, value in enumerate(cells):
            ref = ""
            n = c
            while True:
                ref = chr(ord("A") + n % 26) + ref
                n = n // 26 - 1
                if n < 0:
                    break
            # Numbers stored as numbers, so the spreadsheet can add them up.
            if _number(value) and not str(value).startswith("0") or _number(value) and value in ("0", "0.0"):
                parts.append(f'<c r="{ref}{r}"><v>{escape(str(value))}</v></c>')
            else:
                parts.append(f'<c r="{ref}{r}" t="inlineStr"><is><t xml:space="preserve">'
                             f'{escape(str(value))}</t></is></c>')
        body.append(f'<row r="{r}">{"".join(parts)}</row>')

    return _zip({
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>',
        "_rels/.rels": RELS.format(items=(
            '<Relationship Id="rId1" Target="xl/workbook.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>')),
        "xl/_rels/workbook.xml.rels": RELS.format(items=(
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>')),
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{S_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/worksheets/sheet1.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{S_NS}"><sheetData>{"".join(body)}</sheetData></worksheet>',
    })


# ----------------------------------------------------------- powerpoint

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _slides_from(markdown: str) -> list[tuple[str, list[str]]]:
    """A heading starts a slide; everything under it is that slide's bullets."""
    slides: list[tuple[str, list[str]]] = []
    title, bullets = None, []
    for kind, value in _blocks(markdown):
        if kind == "heading":
            if title is not None or bullets:
                slides.append((title or "", bullets))
            title, bullets = _plain(value[1]), []
        elif kind == "list":
            bullets.extend(_plain(i) for i in value[1])
        elif kind == "para":
            bullets.append(_plain(value))
        elif kind == "table":
            bullets.extend(" — ".join(_plain(c) for c in row) for row in value)
    if title is not None or bullets:
        slides.append((title or "", bullets))
    return slides or [("", [])]


def _slide_xml(title: str, bullets: list[str]) -> str:
    body = "".join(
        f'<a:p><a:pPr lvl="0"/><a:r><a:t>{escape(b)}</a:t></a:r></a:p>'
        for b in bullets) or "<a:p/>"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{REL}"><p:cSld><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr/>'
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr><a:spLocks noGrp="1"/>'
        '</p:cNvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="838200" y="365125"/><a:ext cx="10515600" cy="1325563"/>'
        '</a:xfrm></p:spPr>'
        f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{escape(title)}</a:t></a:r></a:p>'
        '</p:txBody></p:sp>'
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Content"/><p:cNvSpPr><a:spLocks noGrp="1"/>'
        '</p:cNvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="838200" y="1825625"/><a:ext cx="10515600" cy="4351338"/>'
        '</a:xfrm></p:spPr>'
        f'<p:txBody><a:bodyPr/><a:lstStyle/>{body}</p:txBody></p:sp>'
        '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')


def _pptx(markdown: str) -> bytes:
    slides = _slides_from(markdown)
    n = len(slides)

    parts = {
        "_rels/.rels": RELS.format(items=(
            f'<Relationship Id="rId1" Target="ppt/presentation.xml" Type="{REL}/officeDocument"/>')),
        "ppt/presentation.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:presentation xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{REL}">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rIdMaster"/></p:sldMasterIdLst>'
            '<p:sldIdLst>'
            + "".join(f'<p:sldId id="{256 + i}" r:id="rIdSlide{i + 1}"/>' for i in range(n))
            + '</p:sldIdLst>'
            '<p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
            '</p:presentation>',
        "ppt/_rels/presentation.xml.rels": RELS.format(items=(
            f'<Relationship Id="rIdMaster" Target="slideMasters/slideMaster1.xml" Type="{REL}/slideMaster"/>'
            + "".join(
                f'<Relationship Id="rIdSlide{i + 1}" Target="slides/slide{i + 1}.xml" '
                f'Type="{REL}/slide"/>' for i in range(n)))),
        "ppt/slideMasters/slideMaster1.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:sldMaster xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{REL}"><p:cSld><p:spTree>'
            '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr/></p:spTree></p:cSld>'
            '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" '
            'accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
            'accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
            '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
            '</p:sldMaster>',
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": RELS.format(items=(
            f'<Relationship Id="rId1" Target="../slideLayouts/slideLayout1.xml" Type="{REL}/slideLayout"/>'
            f'<Relationship Id="rId2" Target="../theme/theme1.xml" Type="{REL}/theme"/>')),
        "ppt/slideLayouts/slideLayout1.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:sldLayout xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{REL}" type="obj">'
            '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/>'
            '<p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld>'
            '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>',
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": RELS.format(items=(
            f'<Relationship Id="rId1" Target="../slideMasters/slideMaster1.xml" Type="{REL}/slideMaster"/>')),
        "ppt/theme/theme1.xml": _THEME,
    }
    for i, (title, bullets) in enumerate(slides, 1):
        parts[f"ppt/slides/slide{i}.xml"] = _slide_xml(title, bullets)
        parts[f"ppt/slides/_rels/slide{i}.xml.rels"] = RELS.format(items=(
            f'<Relationship Id="rId1" Target="../slideLayouts/slideLayout1.xml" '
            f'Type="{REL}/slideLayout"/>'))

    overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.'
        f'openxmlformats-officedocument.presentationml.slide+xml"/>' for i in range(1, n + 1))
    parts["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        + overrides + '</Types>')
    return _zip(parts)


_THEME = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<a:theme xmlns:a="{A_NS}" name="Heddled"><a:themeElements>'
    '<a:clrScheme name="Heddled"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
    '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
    '<a:dk2><a:srgbClr val="16150F"/></a:dk2><a:lt2><a:srgbClr val="FBFBFA"/></a:lt2>'
    '<a:accent1><a:srgbClr val="B4442E"/></a:accent1><a:accent2><a:srgbClr val="5C5A50"/></a:accent2>'
    '<a:accent3><a:srgbClr val="7AA2F7"/></a:accent3><a:accent4><a:srgbClr val="7AC07A"/></a:accent4>'
    '<a:accent5><a:srgbClr val="E0AF68"/></a:accent5><a:accent6><a:srgbClr val="F07178"/></a:accent6>'
    '<a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink>'
    '</a:clrScheme>'
    '<a:fontScheme name="Heddled"><a:majorFont><a:latin typeface="Calibri Light"/>'
    '<a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/>'
    '</a:minorFont></a:fontScheme>'
    '<a:fmtScheme name="Heddled">'
    '<a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
    '<a:lnStyleLst><a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>'
    '<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle>'
    '<a:effectStyle><a:effectLst/></a:effectStyle>'
    '<a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
    '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>'
    '</a:fmtScheme></a:themeElements></a:theme>')


# --------------------------------------------------------------- writing

def build(suffix: str, content) -> bytes:
    suffix = suffix.lower()
    if suffix == ".docx":
        return _docx(content)
    if suffix == ".xlsx":
        return _xlsx(content)
    if suffix == ".pptx":
        return _pptx(content)
    raise DocumentError(
        f"cannot make a '{suffix}' file. This writes "
        + ", ".join(sorted(WRITABLE)) + ", and plain text of any kind.")


# --------------------------------------------------------------- reading

def _text_from_docx(data: bytes) -> str:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml")
    for para in ElementTree.fromstring(xml).iter(f"{{{W_NS}}}p"):
        runs = [t.text or "" for t in para.iter(f"{{{W_NS}}}t")]
        out.append("".join(runs))
    return "\n".join(out).strip()


def _text_from_xlsx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.iter(f"{{{S_NS}}}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{S_NS}}}t")))
        sheets = [n for n in z.namelist() if n.startswith("xl/worksheets/sheet")]
        lines = []
        for name in sorted(sheets):
            root = ElementTree.fromstring(z.read(name))
            for row in root.iter(f"{{{S_NS}}}row"):
                cells = []
                for c in row.iter(f"{{{S_NS}}}c"):
                    kind = c.get("t")
                    if kind == "s":
                        v = c.find(f"{{{S_NS}}}v")
                        idx = int(v.text) if v is not None and v.text else 0
                        cells.append(shared[idx] if idx < len(shared) else "")
                    elif kind == "inlineStr":
                        cells.append("".join(t.text or "" for t in c.iter(f"{{{S_NS}}}t")))
                    else:
                        v = c.find(f"{{{S_NS}}}v")
                        cells.append(v.text if v is not None and v.text else "")
                lines.append(",".join(cells))
    return "\n".join(lines).strip()


def _text_from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise DocumentError(
            "reading PDFs needs an extra package that is not installed. "
            "`pip install pypdf` and restart, or convert the file to text first.")
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()


def extract(path: Path) -> str:
    """The text inside a document, for an agent that asked to read it."""
    suffix = path.suffix.lower()
    data = path.read_bytes()
    if suffix == ".docx":
        text = _text_from_docx(data)
    elif suffix == ".xlsx":
        text = _text_from_xlsx(data)
    elif suffix == ".pdf":
        text = _text_from_pdf(data)
    else:
        raise DocumentError(f"'{path.name}' is not a kind of document this can read")
    if not text:
        raise DocumentError(
            f"'{path.name}' opened, but there was no text in it — it may be "
            "scanned images rather than words.")
    return text
