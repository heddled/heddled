"""Word, Excel and PowerPoint files made without a library to make them.

The structural checks below are cheap and always run. The ones that matter most
are the round trips through python-docx, openpyxl and python-pptx: a file that
unzips and parses is not necessarily a file Word will open, and the only honest
way to know is to hand it to somebody else's implementation. Those three are
dev-only — none of them is a runtime dependency, which is the entire point.
"""

import io
import zipfile

import pytest

from heddled import documents
from heddled.documents import DocumentError

REPORT = """# Invoice summary

Three invoices are unpaid as of today.

## What is outstanding

- F-2231 — Acme BV — €249.00
- F-2251 — Beta Ltd — €1,120.50

| Invoice | Customer | Amount |
| --- | --- | --- |
| F-2231 | Acme BV | 249.00 |
| F-2251 | Beta Ltd | 1120.50 |

## Next steps

1. Send a reminder today
2. Escalate on Friday
"""


def parts(data: bytes) -> set:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return set(z.namelist())


class TestTheyAreValidPackages:
    @pytest.mark.parametrize("suffix", [".docx", ".xlsx", ".pptx"])
    def test_a_zip_whose_every_part_is_well_formed_xml(self, suffix):
        from xml.etree import ElementTree

        data = documents.build(suffix, REPORT)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            assert z.testzip() is None
            names = z.namelist()
            assert "[Content_Types].xml" in names
            assert "_rels/.rels" in names
            for name in names:
                ElementTree.fromstring(z.read(name))   # raises if malformed

    def test_an_unknown_kind_says_what_it_can_make(self):
        with pytest.raises(DocumentError, match="cannot make"):
            documents.build(".odt", REPORT)


class TestWordOpensIt:
    """Handed to python-docx, which is not what wrote it."""

    def test_it_opens_and_every_paragraph_has_a_style(self):
        docx = pytest.importorskip("docx")
        d = docx.Document(io.BytesIO(documents.build(".docx", REPORT)))
        # Without a default style in styles.xml, a paragraph carrying no
        # explicit one resolves to nothing — which is how this was first wrong.
        assert all(p.style is not None for p in d.paragraphs)

    def test_headings_are_real_headings(self):
        docx = pytest.importorskip("docx")
        d = docx.Document(io.BytesIO(documents.build(".docx", REPORT)))
        headings = [p.text for p in d.paragraphs
                    if p.style and p.style.name.startswith("Heading")]
        assert headings == ["Invoice summary", "What is outstanding", "Next steps"]

    def test_a_markdown_table_becomes_a_table(self):
        docx = pytest.importorskip("docx")
        d = docx.Document(io.BytesIO(documents.build(".docx", REPORT)))
        assert len(d.tables) == 1
        assert [c.text for c in d.tables[0].rows[0].cells] == \
            ["Invoice", "Customer", "Amount"]

    def test_emphasis_markers_do_not_survive_as_text(self):
        docx = pytest.importorskip("docx")
        d = docx.Document(io.BytesIO(documents.build(".docx", "Some **bold** text.")))
        assert "**" not in "\n".join(p.text for p in d.paragraphs)


class TestExcelOpensIt:
    def test_it_opens_with_the_rows_in_it(self):
        openpyxl = pytest.importorskip("openpyxl")
        wb = openpyxl.load_workbook(io.BytesIO(documents.build(".xlsx", REPORT)))
        rows = [[c.value for c in r] for r in wb.active.iter_rows()]
        assert rows[0] == ["Invoice", "Customer", "Amount"]
        assert rows[1][0] == "F-2231"

    def test_numbers_are_numbers_so_they_can_be_added_up(self):
        """A spreadsheet of text that looks like money is not a spreadsheet."""
        openpyxl = pytest.importorskip("openpyxl")
        wb = openpyxl.load_workbook(io.BytesIO(documents.build(".xlsx", REPORT)))
        rows = [[c.value for c in r] for r in wb.active.iter_rows()]
        assert rows[1][2] == 249.0
        assert isinstance(rows[1][2], (int, float))

    def test_csv_works_as_well_as_a_markdown_table(self):
        openpyxl = pytest.importorskip("openpyxl")
        wb = openpyxl.load_workbook(
            io.BytesIO(documents.build(".xlsx", "a,b\n1,2\n3,4\n")))
        assert [[c.value for c in r] for r in wb.active.iter_rows()] == \
            [["a", "b"], [1, 2], [3, 4]]

    def test_a_list_of_rows_works_too(self):
        openpyxl = pytest.importorskip("openpyxl")
        wb = openpyxl.load_workbook(
            io.BytesIO(documents.build(".xlsx", [["x", "y"], ["1", "2"]])))
        assert [[c.value for c in r] for r in wb.active.iter_rows()] == \
            [["x", "y"], [1, 2]]

    def test_nothing_to_put_in_it_is_refused(self):
        with pytest.raises(DocumentError, match="no rows"):
            documents.build(".xlsx", "   ")


class TestPowerPointOpensIt:
    def test_one_slide_per_heading(self):
        pptx = pytest.importorskip("pptx")
        pres = pptx.Presentation(io.BytesIO(documents.build(".pptx", REPORT)))
        titles = [s.shapes[0].text_frame.text for s in pres.slides]
        assert titles == ["Invoice summary", "What is outstanding", "Next steps"]

    def test_the_bullets_land_on_their_slide(self):
        pptx = pytest.importorskip("pptx")
        pres = pptx.Presentation(io.BytesIO(documents.build(".pptx", REPORT)))
        second = list(pres.slides)[1]
        body = second.shapes[1].text_frame.text
        assert "F-2231" in body and "Beta Ltd" in body


class TestReadingThemBack:
    def test_word(self, tmp_path):
        path = tmp_path / "r.docx"
        path.write_bytes(documents.build(".docx", REPORT))
        text = documents.extract(path)
        assert "Invoice summary" in text and "Acme BV" in text

    def test_excel(self, tmp_path):
        path = tmp_path / "r.xlsx"
        path.write_bytes(documents.build(".xlsx", REPORT))
        text = documents.extract(path)
        assert "Invoice,Customer,Amount" in text

    def test_excel_written_by_something_else(self, tmp_path):
        """Real spreadsheets use a shared-string table; ours uses inline
        strings. Reading has to cope with both."""
        openpyxl = pytest.importorskip("openpyxl")
        wb = openpyxl.Workbook()
        wb.active.append(["Name", "Total"])
        wb.active.append(["Acme", 249])
        path = tmp_path / "theirs.xlsx"
        wb.save(path)
        text = documents.extract(path)
        assert "Name,Total" in text and "Acme" in text

    def test_word_written_by_something_else(self, tmp_path):
        docx = pytest.importorskip("docx")
        d = docx.Document()
        d.add_heading("Their heading", level=1)
        d.add_paragraph("Their paragraph.")
        path = tmp_path / "theirs.docx"
        d.save(path)
        text = documents.extract(path)
        assert "Their heading" in text and "Their paragraph." in text

    def test_something_that_is_not_a_document(self, tmp_path):
        path = tmp_path / "x.rtf"
        path.write_bytes(b"{\\rtf1}")
        with pytest.raises(DocumentError, match="not a kind of document"):
            documents.extract(path)

    def test_a_pdf_without_the_optional_package_says_so_plainly(self, tmp_path, monkeypatch):
        """PDF is the one format that genuinely needs a library. When it is not
        there the message says what to do, rather than an ImportError."""
        import builtins

        real = builtins.__import__

        def refuse(name, *a, **kw):
            if name == "pypdf":
                raise ImportError("no pypdf")
            return real(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", refuse)
        path = tmp_path / "x.pdf"
        path.write_bytes(b"%PDF-1.4\nnot really")
        with pytest.raises(DocumentError, match="pip install pypdf"):
            documents.extract(path)
