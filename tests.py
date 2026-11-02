"""Planted-fault tests. Each test takes clean text, plants one known fault, and checks
that the matching check fires and no other check does.

The clean text is fixture.txt: the first ~9,700 words of Pride and Prejudice, from the
Standard Ebooks edition (public domain text, CC0 files). The tests build EPUBs and PDFs
from it, so they need no books on disk; the two calibre tests are skipped without ebook-convert.

Run in this folder: python3 -m pytest tests.py
"""
import html
import json
import multiprocessing
import os
import random
import re
import subprocess
import textwrap
import zipfile

import pytest

from tidy_calibre_library import books, checks, llm, report, validate
from tidy_calibre_library import words as W
from tidy_calibre_library.books import FIX, INFO, REPLACE, WARN, issue

# Workers must see what monkeypatch.setenv sets. Linux's default, forkserver, keeps the environment it started with.
multiprocessing.set_start_method("spawn", force=True)

HERE = os.path.dirname(os.path.abspath(__file__))
PARAS = open(os.path.join(HERE, "fixture.txt"), encoding="utf-8").read().splitlines()
ROMAN = "I II III IV V VI VII VIII IX X XI XII".split()
META = {"title": "Test Book", "authors": ["Jane Austen"], "language": "en"}
TEXT = set(validate.TEXT_CODES)
XHTML = ('<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" '
         'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>{t}</title></head><body>{b}</body></html>')


# ---------------------------------------------------------------- builders

def chapters(n=10):
    per = len(PARAS) // n
    return [(f"Chapter {ROMAN[i]}", PARAS[i * per:(i + 1) * per]) for i in range(n)]


def make_epub(path, chaps, spine_extra=(), rights=False, missing=False, broken_xml=False, toc=True, extra_html="",
              raw_newlines=False, css="", html_paras=False, images=None, lang="en"):
    """html_paras: each paragraph is ready XHTML (for pictures); images: {file name: bytes}."""
    esc = html.escape
    files, items, spine = {}, [], []
    for name, data in (images or {}).items():
        files[f"OEBPS/images/{name}"] = data
        items.append(f'<item id="{name.replace(".", "_")}" href="images/{name}" media-type="image/png"/>')
    for i, (head, paras) in enumerate(chaps, 1):
        nl = "\n" if raw_newlines else "<br/>"  # raw: newlines in the source, as Gutenberg's editor wraps them
        body = f"<h2>{esc(head)}</h2>" + "".join(p if html_paras else "<p>" + esc(p).replace("\n", nl) + "</p>"
                                                 for p in paras)
        if broken_xml and i == 2:
            body += "<p>an <i>unclosed tag</p>"
        if i == 2:
            body += extra_html
        page = XHTML.format(t=esc(head), b=body)
        if css:
            page = page.replace("</head>", '<link rel="stylesheet" type="text/css" href="style.css"/></head>')
        files[f"OEBPS/ch{i}.xhtml"] = page
        items.append(f'<item id="ch{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="ch{i}"/>')
    spine += [f'<itemref idref="{x}"/>' for x in spine_extra]
    if missing:
        items.append('<item id="img" href="images/plate.jpg" media-type="image/jpeg"/>')
    if css:
        files["OEBPS/style.css"] = css
        items.append('<item id="css" href="style.css" media-type="text/css"/>')
    links = "".join(f'<li><a href="ch{i}.xhtml">{esc(h)}</a></li>' for i, (h, _) in enumerate(chaps, 1)) if toc else ""
    files["OEBPS/nav.xhtml"] = XHTML.format(t="Contents", b=f'<nav epub:type="toc"><ol>{links}</ol></nav>')
    items.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    files["OEBPS/content.opf"] = (
        '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">t'
        f'</dc:identifier><dc:title>Test Book</dc:title><dc:creator>Jane Austen</dc:creator><dc:language>{lang}'
        f'</dc:language></metadata><manifest>{"".join(items)}</manifest><spine>{"".join(spine)}</spine></package>')
    files["META-INF/container.xml"] = (
        '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        '</rootfiles></container>')
    if rights:
        files["META-INF/rights.xml"] = "<rights/>"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in files.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return path


def pdf_pages(lines_per_page=24):
    text = " ".join(PARAS)
    for a, b in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'), ("—", "--"), ("…", "...")):
        text = text.replace(a, b)
    lines = textwrap.wrap(text.encode("ascii", "ignore").decode(), 70)
    return [lines[i:i + lines_per_page] for i in range(0, len(lines), lines_per_page)]


def make_pdf(path, pages, numbers=None):  # numbers: printed page numbers, or "" for none
    """A plain PDF: one Helvetica text block per page, the printed page number last."""
    numbers = numbers or list(range(1, len(pages) + 1))
    objects = {1: "<< /Type /Catalog /Pages 2 0 R >>",
               3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    for i, (lines, num) in enumerate(zip(pages, numbers)):
        pid, cid = 4 + 2 * i, 5 + 2 * i
        kids.append(f"{pid} 0 R")
        esc = [l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for l in lines + ["", str(num)]]
        stream = "BT /F1 10 Tf 12 TL 50 760 Td " + " ".join(f"({l}) '" for l in esc) + " ET"
        objects[pid] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 432 792] "
                        f"/Resources << /Font << /F1 3 0 R >> >> /Contents {cid} 0 R >>")
        objects[cid] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", {}
    for k in sorted(objects):
        offsets[k] = len(out)
        out += f"{k} 0 obj\n{objects[k]}\nendobj\n".encode("latin-1")
    xref, size = len(out), max(objects) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offsets[k]:010d} 00000 n \n".encode() for k in range(1, size))
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    with open(path, "wb") as f:
        f.write(out)
    return path


def found(path, fmt="epub"):
    doc = books.read(str(path), fmt, str(path.parent), "test")
    res = checks.check(doc, META)
    return {i["code"]: i["level"] for i in res["issues"]}


def mutate(chaps, fn, rate, seed=1):
    rnd = random.Random(seed)
    return [(h, [" ".join(fn(w) if rnd.random() < rate else w for w in p.split(" ")) for p in ps]) for h, ps in chaps]


@pytest.fixture
def epub(tmp_path):
    return lambda chaps=None, **kw: make_epub(tmp_path / "book.epub", chaps or chapters(), **kw)


# ---------------------------------------------------------------- clean baselines

def test_clean_epub(epub):
    assert found(epub()) == {}


def test_clean_pdf(tmp_path):
    assert found(make_pdf(tmp_path / "b.pdf", pdf_pages()), "pdf") == {}


@pytest.mark.skipif(not books.tool("ebook-convert", "QA_EBOOK_CONVERT"), reason="needs calibre's ebook-convert")
def test_clean_mobi(epub, tmp_path):
    mobi = tmp_path / "book.mobi"
    subprocess.run([books.tool("ebook-convert", "QA_EBOOK_CONVERT"), str(epub()), str(mobi)],
                   check=True, capture_output=True)
    assert found(mobi, "mobi") == {}


# ---------------------------------------------------------------- text faults

def test_ocr_swaps(epub):
    def misread(w):
        for a, b in (("h", "li"), ("m", "rn"), ("w", "vv"), ("d", "cl")):
            if w.islower() and a in w:
                return w.replace(a, b, 1)
        return w
    got = found(epub(mutate(chapters(), misread, 0.05)))
    assert {"nonwords", "ocr_swaps"} <= set(got) <= TEXT
    assert got["ocr_swaps"] == REPLACE


def test_glued_words(epub):
    chaps = [(h, [p.replace(" of the ", " ofthe ").replace(" in the ", " inthe ").replace(" to the ", " tothe ")
                  for p in ps]) for h, ps in chapters()]
    got = found(epub(chaps))
    assert "glued" in got and set(got) <= TEXT


def test_split_words(epub):
    def split(w):
        return f"{w[:len(w) // 2]} {w[len(w) // 2:]}" if w.isalpha() and w.islower() and len(w) >= 8 else w
    got = found(epub(mutate(chapters(), split, 0.3)))
    assert "split_words" in got and set(got) <= TEXT


def test_hyphen_leftovers(epub):
    def hyphen(w):
        return f"{w[:3]}- {w[3:]}" if w.isalpha() and w.islower() and len(w) >= 7 else w
    got = found(epub(mutate(chapters(), hyphen, 0.2)))
    assert "hyphens" in got and "split_words" not in got and set(got) <= TEXT | {"hyphens"}


def test_mojibake(epub):
    chaps = [(h, [p.replace("’", "â€™").replace("“", "â€œ") for p in ps]) for h, ps in chapters()]
    got = found(epub(chaps))
    assert got.get("mojibake") == FIX and set(got) <= {"mojibake"} | TEXT  # decodable, so a FIX


def test_watermark(epub):
    chaps = chapters()
    for i in range(0, 10, 2):
        chaps[i][1].insert(3, "Free ebooks ==> www.Ebook777.com")
    assert set(found(epub(chaps))) == {"watermark"}


# ---------------------------------------------------------------- layout faults

def test_wrapped_lines(epub):
    chaps = [(h, [textwrap.fill(p, 70) for p in ps]) for h, ps in chapters()]
    assert set(found(epub(chaps))) == {"broken_lines"}


def test_lines_as_paragraphs(epub):
    chaps = [(h, [line for p in ps for line in textwrap.wrap(p, 70)]) for h, ps in chapters()]
    assert set(found(epub(chaps))) == {"broken_lines"}


def test_running_heads(epub):
    out, page, count = [], 0, 0
    for h, ps in chapters():
        new = []
        for p in ps:
            new.append(p)
            count += len(p.split())
            if count >= 300:
                page, count = page + 1, 0
                new += ["PRIDE AND PREJUDICE", str(page)]
        out.append((h, new))
    assert set(found(epub(out))) == {"headers"}


# ---------------------------------------------------------------- structure, duplicates, gaps

def test_duplicate_block(epub):
    chaps = chapters()
    block, n = [], 0
    for p in chaps[1][1]:
        block.append(p)
        n += len(p.split())
        if n >= 350:
            break
    chaps[6][1][2:2] = block
    got = found(epub(chaps))
    assert set(got) == {"duplicate_text"} and got["duplicate_text"] == REPLACE


def test_duplicate_chapter_in_spine(epub):
    got = found(epub(spine_extra=("ch3",)))  # nothing is lost: deleting the second entry repairs it
    assert got == {"dup_spine": FIX, "duplicate_file": FIX}


def test_missing_chapter(epub):
    chaps = chapters()
    del chaps[4]
    assert found(epub(chaps)) == {"chapter_gap": WARN}


def test_sample_notice(epub):
    chaps = chapters()
    chaps[-1][1].append("This is the end of the free sample. Buy the full book to keep reading.")
    assert set(found(epub(chaps))) == {"sample"}


def test_cut_off(epub):
    chaps = chapters()
    last = chaps[-1][1][-1].split()
    chaps[-1][1][-1] = " ".join(last[:max(12, len(last) * 2 // 3)]).rstrip(".,;:!?’”\"'—")
    assert set(found(epub(chaps))) == {"cut_off"}


def test_drm(epub):
    assert set(found(epub(rights=True))) == {"drm"}


def test_missing_file(epub):
    assert found(epub(missing=True)) == {"missing_files": "FIX"}


def test_malformed_xhtml(epub):
    assert set(found(epub(broken_xml=True))) == {"malformed_xhtml"}


def test_no_toc(epub):
    assert set(found(epub(toc=False))) == {"no_toc"}


def test_pdf_missing_pages(tmp_path):
    pages = pdf_pages()
    numbers = list(range(1, len(pages) + 1))
    del pages[14:17], numbers[14:17]
    got = found(make_pdf(tmp_path / "b.pdf", pages, numbers), "pdf")
    assert got == {"page_gap": REPLACE}


def test_pdf_duplicate_page(tmp_path):
    pages = pdf_pages()
    numbers = list(range(1, len(pages) + 1))
    pages.insert(10, pages[9])
    numbers.insert(10, numbers[9])
    got = found(make_pdf(tmp_path / "b.pdf", pages, numbers), "pdf")
    assert got.get("duplicate_page") == REPLACE and set(got) <= {"duplicate_page", "duplicate_text"}


# ---------------------------------------------------------------- words, verdicts, LLM

@pytest.mark.parametrize("token,kind", [
    ("the", W.WORD), ("gaol", W.WORD), ("connexion", W.WORD), ("o'er", W.WORD), ("whereupon", W.WORD),
    ("tlie", W.OCR), ("rnore", W.OCR), ("wbich", W.OCR), ("aud", W.OCR), ("l0ve", W.OCR), ("fuch", W.OCR),
    ("ofthe", W.GLUED), ("andthe", W.GLUED), ("thewindow", W.GLUED),
    ("fitten", W.OTHER), ("goin", W.OTHER), ("sittest", W.OTHER), ("showeth", W.OTHER), ("1820s", W.WORD)])
def test_classify(token, kind):
    assert W.classify(token)[0] == kind


def _format(issues, rating, **review):
    return {"result": {"issues": issues, "metrics": {}, "reflow": True},
            "llm": {"rating": rating, "note": "n", **review}}


def test_llm_good_rating_lowers_text_issues_only():
    res = validate.effective(_format([issue("nonwords", WARN, "x"), issue("ocr_swaps", REPLACE, "y"),
                                      issue("chapter_gap", WARN, "z")], 5))
    levels = {i["code"]: i["level"] for i in res["issues"]}
    assert levels == {"nonwords": INFO, "ocr_swaps": WARN, "chapter_gap": WARN}


def test_llm_poor_rating_adds_replace():
    res = validate.effective(_format([], 2))
    assert res["level"] == REPLACE and res["issues"][0]["code"] == "llm_poor"


def test_llm_reply_is_parsed_and_cached(tmp_path, monkeypatch):
    class Reply:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": 'Here: {"rating": 2, "problems": ["ocr", "bogus"], '
                                                        '"examples": ["tlie"], "note": "Many OCR errors."}'}}]}
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: Reply())
    samples = {"pages1": "some text"}
    first = llm.review(samples, "T", "key", str(tmp_path))
    assert first == {"rating": 2, "errors": None, "passages": [], "pages": 0, "problems": ["ocr"], "examples": ["tlie"],
                     "note": "Many OCR errors.", "model": llm.MODEL}
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: pytest.fail("not cached"))
    assert llm.review(samples, "T", "key", str(tmp_path)) == first


def test_llm_reply_with_line_break_in_a_quote(tmp_path, monkeypatch):
    class Reply:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": '{"rating": 1, "problems": ["missing"], '
                                                        '"examples": ["gifted ar\nantly"], "note": "Shredded."}'}}]}
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: Reply())
    assert llm.review({"pages1": "x"}, "T", "key", str(tmp_path))["examples"] == ["gifted ar\nantly"]


# ---------------------------------------------------------------- false alarms found on real books

def test_lost_text(epub):
    def shred(ps):
        out = []
        for i, p in enumerate(ps):
            words = p.split(" ")
            if i % 3 == 0 and len(words) > 20:
                w = words[10]
                out += [" ".join(words[:10] + [w[:2]]), " ".join([w[2:]] + words[11:])]
            else:
                out.append(p)
        return out
    got = found(epub([(h, shred(ps)) for h, ps in chapters()]))
    assert got.get("lost_text") == REPLACE


def test_heading_on_two_lines_or_without_label_is_no_gap(epub):
    chaps = chapters()
    chaps[4] = ("Chapter\nV", chaps[4][1])  # "CHAPTER" <br> "THIRTY-ONE"
    chaps[5] = ("Six", chaps[5][1])          # "Thirty-Two" with no "Chapter"
    assert found(epub(chaps)) == {}


def test_chapter_number_paragraphs_are_not_page_numbers(epub):
    chaps = [("", [str(i + 1)] + ps) for i, (_, ps) in enumerate(chapters())]
    assert found(epub(chaps)) == {}


def test_publisher_ad_page_is_no_watermark(epub):
    chaps = chapters()
    chaps[-1][1].extend(["Visit www.AuthorTracker.com for exclusive information on your favorite author.",
                         "www.harpercollins.com", "www.harpercollins.com"])
    assert found(epub(chaps)) == {}


def test_foreign_lines_are_not_lost_text(epub):
    chaps = chapters()
    tamil = ("saapudu kannu", "neela mangavai", "onaku enna kavalai", "vaada kanna", "chellam nee",
             "kuzhandai paadu", "maambazham theru", "amma vandaachu", "kaapi kudi", "thoongu kanna")
    for i in range(10):
        chaps[i][1].insert(2, f"Saapudu {tamil[i]} saapudu")
        chaps[i][1].insert(4, f"Eze ilikwa {tamil[9 - i]} ikwaba akwa")
    assert found(epub(chaps)) == {}


def test_repeated_contents_list_is_no_duplicate(epub):
    chaps = chapters()
    contents = [f"Chapter {r}: The day the visitors came to the house" for r in ROMAN[:10]]
    chaps[0] = (chaps[0][0], contents + chaps[0][1])
    chaps[-1] = (chaps[-1][0], chaps[-1][1] + contents)
    assert set(found(epub(chaps))) <= {"chapter_gap"} and "duplicate_text" not in found(epub(chaps))


def test_glossary_is_not_lost_text(epub):
    chaps = chapters()
    words_ = ("efukfu", "egwugwu", "ilo", "obi", "ogbanje", "osu", "ekwe", "chi", "iba", "ndichie")
    glossary = [f"{w} : a word used in the village for the thing described" for w in words_]
    chaps.append(("Glossary of Ibo words", glossary * 3))
    assert found(epub(chaps)) == {}


# ---------------------------------------------------------------- more false alarms found on real books

def test_code_blocks_are_skipped(epub):
    shell = "".join(f"<pre># zpool status\npool: data{i}\nstate: DEGRADED\n# gpart modify -i 2 -l f0{i}-serial da2</pre>"
                    for i in range(40))
    assert found(epub(extra_html=shell)) == {}


def test_minhash_tells_books_apart():
    words_ = " ".join(PARAS).lower().split()
    a, b = checks.minhash(words_[:4000]), checks.minhash(words_[4000:8000])
    assert sum(x == y for x, y in zip(a, b)) / 64 < 0.1
    assert checks.minhash(words_[:4000]) == a


def test_pdf_text_layer_is_info_only():
    doc = books.Doc("pdf", "x.pdf", reflow=False)
    doc.parts = [books.Part(f"page {i}", [("p", line.replace("1", "\uf731")) for line in page])
                 for i, page in enumerate(pdf_pages())]
    res = checks.check(doc, META)
    assert {i["level"] for i in res["issues"]} <= {INFO}


def test_pdf_quoted_half_page_is_no_duplicate(tmp_path):
    pages = pdf_pages()
    pages[4] = pages[4][:12] + pages[20][:12]
    got = found(make_pdf(tmp_path / "b.pdf", pages), "pdf")
    assert "duplicate_page" not in got


def test_pdf_back_cover_repeating_front_is_no_duplicate(tmp_path):
    pages = pdf_pages()
    pages.append(pages[0])
    got = found(make_pdf(tmp_path / "b.pdf", pages, list(range(1, len(pages) + 1))), "pdf")
    assert "duplicate_page" not in got


def test_pdf_gap_at_chapter_start_is_warn(tmp_path):
    pages = pdf_pages()
    numbers = list(range(1, len(pages) + 1))
    del pages[14:16], numbers[14:16]
    pages[14] = ["Chapter 2. Nash Equilibrium"] + pages[14][1:]
    got = found(make_pdf(tmp_path / "b.pdf", pages, numbers), "pdf")
    assert got == {"page_gap": WARN}


def test_word_broken_across_paragraphs_is_broken_lines_not_lost_text(epub):
    def breakup(ps):
        out = []
        for p in ps:
            words = p.split(" ")
            k = next((k for k in range(8, len(words) - 4) if len(words[k]) >= 6 and words[k].isalpha()
                      and words[k].islower()), None)
            if k is None:
                out.append(p)
            else:
                w = words[k]
                out += [" ".join(words[:k] + [w[:3] + "-"]), " ".join([w[3:]] + words[k + 1:])]
        return out
    got = found(epub([(h, breakup(ps)) for h, ps in chapters()]))
    assert "broken_lines" in got and "lost_text" not in got


def test_shell_lines_are_skipped(epub):
    chaps = chapters()
    for i in range(10):
        chaps[i][1].insert(3, f"# gpart modify -i 2 -l f0{i}-serialnum da{i}")
        chaps[i][1].insert(5, f"$ zfs set compression=lz4 tank/data{i}")
    assert found(epub(chaps)) == {}


def test_prose_about_scanning_is_no_watermark(epub):
    chaps = chapters()
    chaps[3][1].insert(2, "The forms were scanned by a computer, and the numbers of persons having the "
                          "disease were counted by hand afterwards.")
    assert found(epub(chaps)) == {}


def test_pdf_far_apart_duplicate_page_is_warn(tmp_path):
    pages = pdf_pages()
    pages[25:27] = pages[5:7]  # 2 pages, ~500 words: over the text-repeat REPLACE limit
    got = found(make_pdf(tmp_path / "b.pdf", pages), "pdf")
    # the text repeat far away is INFO: in a PDF the page check judges repeated pages
    assert got.get("duplicate_page") == WARN and set(got.values()) - {INFO} == {WARN}


def test_verse_one_line_per_paragraph_is_not_broken_lines(epub):
    widths = (30, 35, 40, 38, 62, 70)
    def verse(ps):
        out, k = [], 0
        for p in ps:
            for line in textwrap.wrap(p, widths[k % len(widths)]):
                out.append(line)
                k += 1
        return out
    assert "broken_lines" not in found(epub([(h, verse(ps)) for h, ps in chapters()]))


# ---------------------------------------------------------------- line breaks follow HTML rules

def test_newlines_in_source_are_spaces(epub):
    """Gutenberg wraps paragraphs in its source; a reader app shows flowing text."""
    chaps = [(h, [textwrap.fill(p, 70) for p in ps]) for h, ps in chapters()]
    assert found(epub(chaps, raw_newlines=True)) == {}


def test_verse_in_a_white_space_pre_class_keeps_its_lines(epub):
    stanza = "Once I lov’d a bonie lass,\nAy, and I love her still;\nAnd whilst that virtue warms my breast,\nI’ll love my handsome Nell."
    verse = "".join(f'<div class="pre">{stanza}</div>' for _ in range(3))
    path = epub(extra_html=verse, css=".pre { margin-left: 10%; white-space: pre }")
    doc = books.read_epub(str(path))
    lines = [t for p in doc.parts for _, t in p.blocks if "bonie lass" in t]
    assert lines and lines[0].count("\n") == 3
    assert found(path) == {}


def test_wrapped_prose_in_pre_is_broken_lines(epub):
    chaps = chapters()
    prose = "".join(f"<pre>{html.escape(textwrap.fill(p, 70))}</pre>" for p in chaps[2][1])
    assert "broken_lines" in found(epub(chaps, extra_html=prose))


def test_formats_with_the_same_text_share_one_llm_review(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "review", lambda samples, *a, **k: calls.append(samples) or {"rating": 2, "note": "n"})
    fmt = lambda name, words: {"fmt": name, "result": {"metrics": {"words": words}, "samples": {"middle": name},
                                                       "level": INFO}}
    b = {"title": "T", "formats": [fmt("mobi", 92816), fmt("epub", 92816), fmt("pdf", 120000)]}
    validate.run_llm([b], "all", "key", "/tmp", llm.MODEL)
    assert len(calls) == 2                                   # one per distinct text
    assert all(f["llm"]["rating"] == 2 for f in b["formats"])  # the MOBI is judged too


def test_llm_does_not_reuse_answers_from_an_older_prompt(tmp_path, monkeypatch):
    """Version 3 changed the scale (the old one rated page photos 4/5), so an old answer is not valid."""
    import hashlib
    samples = {"pages1": "some text"}
    body = "Book: T\n\n=== Passage 1 ===\nsome text"
    tag = hashlib.sha256(f"{llm.PROMPT_VERSION - 1}\n{llm.MODEL}\n{body}".encode()).hexdigest()
    (tmp_path / "llm").mkdir()
    (tmp_path / "llm" / f"{tag}.json").write_text(json.dumps({"rating": 5, "note": "old"}))
    asked = []
    class Reply:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": '{"errors": 9, "rating": 2, "problems": [], "examples": [], '
                                                        '"note": "page photos"}'}}]}
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: asked.append(1) or Reply())
    assert llm.review(samples, "T", "key", str(tmp_path))["rating"] == 2 and asked


# ---------------------------------------------------------------- faults found by reading books by hand

def _png(w, h):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (w, h), 200).save(buf, "PNG")
    return buf.getvalue()


def _with_pictures(mid_sentence, size=(549, 778), per_chapter=3):
    """Chapters as XHTML with pictures: either between the halves of a sentence (a page photo),
    or between paragraphs (an illustration)."""
    out = []
    k = 0
    for head, paras in chapters():
        html_paras, placed = [], 0
        for p in paras:
            words = p.split(" ")
            cut = next((j for j in range(len(words) // 3, len(words) - 1)
                        if words[j + 1][:1].islower() and not checks.ends_sentence(words[j])), None)
            if placed < per_chapter and len(words) > 30 and cut:
                k += 1
                img = f'<img src="images/p{k}.png" alt=""/>'
                if mid_sentence:
                    html_paras += [f"<p>{html.escape(' '.join(words[:cut + 1]))}</p>", img,
                                   f"<p>{html.escape(' '.join(words[cut + 1:]))}</p>"]
                else:
                    html_paras += [f"<p>{html.escape(p)}</p>", f"<div>{img}</div>"]
                placed += 1
            else:
                html_paras.append(f"<p>{html.escape(p)}</p>")
        out.append((head, html_paras))
    return out, {f"p{i}.png": _png(*size) for i in range(1, k + 1)}


def test_page_photos(epub):
    chaps, imgs = _with_pictures(mid_sentence=True)
    got = found(epub(chaps, html_paras=True, images=imgs))
    assert got.get("page_photos") == REPLACE
    assert set(got) <= {"page_photos", "broken_lines"}  # a split paragraph is also a mid-sentence break


def test_illustrations_between_paragraphs_are_not_page_photos(epub):
    chaps, imgs = _with_pictures(mid_sentence=False)
    assert found(epub(chaps, html_paras=True, images=imgs)) == {}


def test_reader_records_pictures_in_reading_order(epub):
    chaps, imgs = _with_pictures(mid_sentence=True)
    doc = books.read(str(epub(chaps, html_paras=True, images=imgs)), "epub", "/tmp", "t")
    part = doc.parts[0]
    bi, name, w, h, _ = part.images[0]
    assert (name, w, h) == ("p1.png", 549, 778)
    assert not checks.ends_sentence(part.blocks[bi - 1][1]) and part.blocks[bi][1][:1].islower()


def test_digits_read_as_letters(epub):
    chaps = [(h, [p.replace(" I am ", " 1 am ").replace(" I have ", " 1 have ").replace("“I ", "“1 ")
                  for p in ps]) for h, ps in chapters()]
    got = found(epub(chaps))
    assert got.get("digit_letters") == WARN and set(got) <= {"digit_letters"} | TEXT


def test_numbers_in_maths_are_not_digit_letters(epub):
    maths = ("Let the slope equal 1 so that a 1 can be read as one unit, and if x = 0, then 0, 1 and 2 "
             "are the only values; case 1 was simpler, and 1 was the answer for x2 and y3.")
    chaps = [(h, ps + [maths]) for h, ps in chapters()]
    assert "digit_letters" not in found(epub(chaps))


def test_lost_paragraph_breaks(epub):
    chaps = [(h, [" ".join(ps)]) for h, ps in chapters()]  # each chapter one paragraph: dialogue run together
    got = found(epub(chaps))
    assert got.get("lost_paragraphs") == REPLACE


def test_long_narrative_paragraphs_are_not_lost_paragraphs(epub):
    chaps = [(h, [" ".join(p for p in ps if "“" not in p)] + [p for p in ps if "“" in p]) for h, ps in chapters()]
    assert "lost_paragraphs" not in found(epub(chaps))


def test_llm_good_rating_lowers_lost_paragraphs():
    res = validate.effective(_format([issue("lost_paragraphs", REPLACE, "x"), issue("page_photos", REPLACE, "y")], 5))
    assert {i["code"]: i["level"] for i in res["issues"]} == {"lost_paragraphs": WARN, "page_photos": REPLACE}


def test_llm_damage_in_one_passage_only_is_warn():
    """Four clean passages, and a forensic test in the story that shows broken words."""
    res = validate.effective(_format([], 2, passages=[5, 5, 4, 5, 1]))
    assert [(i["code"], i["level"]) for i in res["issues"]] == [("llm_local", WARN)]
    res = validate.effective(_format([], 2, passages=[2, 3, 2, 5, 1]))
    assert [(i["code"], i["level"]) for i in res["issues"]] == [("llm_poor", REPLACE)]


def test_long_paragraphs_the_reader_did_not_call_lost_move_down():
    """Long paragraphs in an old translation, rated 3/5 for word slips."""
    res = validate.effective(_format([issue("lost_paragraphs", REPLACE, "x")], 3, problems=["ocr"]))
    assert {i["code"]: i["level"] for i in res["issues"]} == {"lost_paragraphs": WARN, "llm_fair": WARN}
    res = validate.effective(_format([issue("lost_paragraphs", REPLACE, "x")], 3, problems=["paragraphs"]))
    assert {i["code"]: i["level"] for i in res["issues"]}["lost_paragraphs"] == REPLACE


def test_llm_reads_pages_of_the_main_text(epub):
    wrap = lambda ps: [f"<p>{html.escape(p)}</p>" for p in ps]
    chaps, imgs = _with_pictures(mid_sentence=False, size=(800, 500), per_chapter=1)
    chaps = ([("Contents", wrap(["Chapter I", "Chapter II", "Chapter III"] * 5))] + chaps
             + [("Acknowledgements", wrap(["I thank my editor, my agent and everyone at the press. " * 8]))])
    doc = books.read(str(epub(chaps, html_paras=True, images=imgs)), "epub", "/tmp", "t")
    samples = checks.check(doc, META)["samples"]
    assert {"pages1", "pages2", "pages3", "pages4"} <= set(samples)
    text = "\n".join(samples.values())
    assert "my editor, my agent" not in text and "Chapter I\n\nChapter II" not in text
    assert all(500 <= len(samples[k].split()) <= 1100 for k in ("pages1", "pages2", "pages3", "pages4"))
    assert "\n\n" in samples["pages1"]                         # paragraph breaks are visible
    assert "[picture 800x500, not page-shaped, between paragraphs]" in text  # and so are pictures


# ---------------------------------------------------------------- preformatted text

PRE_CSS = ".pre {white-space: pre; font-family: monospace}"


def _pre_chapters(width, capitalize):
    """Each print line in its own preformatted block: prose wrapped at `width` characters,
    or verse-like lines that start with a capital."""
    out = []
    for head, paras in chapters():
        lines = []
        for p in paras:
            for line in textwrap.wrap(p, width):
                lines.append(line[:1].upper() + line[1:] if capitalize else line)
        out.append((head, [f'<pre class="pre">{html.escape(l)}</pre>' for l in lines]))
    return out


def test_preformatted_prose_is_replace(epub):
    got = found(epub(_pre_chapters(70, False), html_paras=True, css=PRE_CSS))
    assert got.get("preformatted") == REPLACE


def test_short_verse_in_preformatted_blocks_is_fine(epub):
    """Gutenberg sets some verse in white-space: pre, one short line at a time."""
    got = found(epub(_pre_chapters(38, True), html_paras=True, css=PRE_CSS))
    assert "preformatted" not in got


def test_long_verse_lines_in_preformatted_blocks_warn(epub):
    """Ballads with long lines in <pre> may run off a small screen."""
    got = found(epub(_pre_chapters(68, True), html_paras=True, css=PRE_CSS))
    assert got.get("preformatted") == WARN


# ---------------------------------------------------------------- metadata rules

from tidy_calibre_library import metadata as M


def _meta(*rows):
    """Book ids -> finding codes, for books given as (authors, author sort, language, ISBNs)."""
    blist = [{"id": i, "title": f"Book {i}", "authors": a, "author_sort": s, "language": lang, "isbn": isbn}
             for i, (a, s, lang, isbn) in enumerate(rows, 1)]
    return {i: {x["code"] for x in v} for i, v in M.check(blist).items()}


def test_author_name_rules():
    assert M.initials("George R R Martin") == M.initials("George R. R. Martin") == "George R.R. Martin"
    assert M.initials("f Thomson Leighton") == "F. Thomson Leighton"
    assert M.author_sort("Ursula K. Le Guin") == "Le Guin, Ursula K."
    assert M.author_sort("Walter M. Miller Jr.") == "Miller, Walter M. Jr."
    assert M.author_sort("David Mack") == "Mack, David"
    assert M.author_sort("Plato") == "Plato"
    assert M.valid_isbn("978-0-553-58202-4") and M.valid_isbn("055358202X") and not M.valid_isbn("9780553582025")


def test_clean_metadata_has_no_findings():
    assert _meta((["George R.R. Martin"], "Martin, George R.R.", "eng", ["9780553582024"]),
                 (["Ursula K. Le Guin", "J.R.R. Tolkien"], "Le Guin, Ursula K. & Tolkien, J.R.R.", "eng", [])) == {}


def test_metadata_faults():
    got = _meta((["George R. R. Martin"], "Martin, George R. R.", "eng", []),
                (["George R.R. Martin"], "Martin, George R.R.", "eng", []),
                (["Doe, Jane"], "Doe, Jane", "eng", []),
                (["China Mieville"], "Mieville, China", "eng", []),
                (["China Miéville"], "Miéville, China", "eng", []),
                (["Patrick Rothfuss"], "ROTHFUSS, PATRICK", "", []),
                (["John R. Roe", "Unknown"], "Roe, John R. & Unknown", "eng", ["9780553582025"]))
    assert got == {1: {"meta_initials"}, 3: {"meta_last_first"}, 4: {"meta_spelling"},
                   6: {"meta_author_sort", "meta_language"}, 7: {"meta_unknown", "meta_isbn"}}


def test_opf_author_sort(tmp_path):
    p = tmp_path / "metadata.opf"
    p.write_text('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
                 '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">'
                 '<dc:title>Tehanu</dc:title><dc:creator opf:file-as="Le Guin, Ursula K." opf:role="aut">'
                 'Ursula K. Le Guin</dc:creator><dc:language>eng</dc:language></metadata></package>', encoding="utf-8")
    meta = validate.read_opf(str(p))
    assert meta["authors"] == ["Ursula K. Le Guin"] and meta["author_sort"] == "Le Guin, Ursula K."


def test_metadata_from_db_and_stale_opf(tmp_path):
    """The database is the truth; an OPF that says something else is reported."""
    import sqlite3
    db = sqlite3.connect(tmp_path / "metadata.db")
    db.executescript("""
        create table books (id integer primary key, title text, author_sort text, path text);
        create table authors (id integer primary key, name text, sort text);
        create table books_authors_link (id integer primary key, book integer, author integer);
        create table languages (id integer primary key, lang_code text);
        create table books_languages_link (id integer primary key, book integer, lang_code integer, item_order integer);
        create table identifiers (id integer primary key, book integer, type text, val text);
        insert into books values (1, 'Tehanu', 'Le Guin, Ursula K.', 'Ursula K. Le Guin/Tehanu (1)'),
                                 (2, 'Wool', 'Howey, Hugh', 'Hugh Howey/Wool (2)');
        insert into authors values (1, 'Ursula K. Le Guin', 'Le Guin, Ursula K.'), (2, 'Howey| Hugh', 'Howey, Hugh');
        insert into books_authors_link values (1, 1, 1), (2, 2, 2);
        insert into languages values (1, 'eng');
        insert into books_languages_link values (1, 1, 1, 0);
        insert into identifiers values (1, 1, 'isbn', '9780553582024');""")
    db.commit()
    rows = M.from_db(str(tmp_path))
    assert rows[2]["authors"] == ["Howey, Hugh"] and rows[1]["language"] == "eng" and rows[2]["language"] == ""
    blist = [{"id": 1, "title": "Tehanu", "authors": ["Ursula K. Le Guin"], "language": "en", "isbn": []},
             {"id": 2, "title": "Wool", "authors": ["Hugh Howey"], "language": "en", "isbn": []}]
    got = {i: {x["code"] for x in v} for i, v in M.check(blist, rows).items()}
    assert got == {2: {"meta_stale_opf", "meta_last_first", "meta_language"}}


# ---------------------------------------------------------------- by design, not damage

def _lib_book(i, title, words, sig, mid=None):
    return {"id": i, "title": title, "authors": ["Jane Doe"], "issues": [], "best_sig": sig,
            "best_mid": mid or list(range(100, 164)), "best_words": words}


def test_same_text_is_a_library_note_and_a_wrong_title_is_a_wrong_book():
    sig = list(range(64))
    omnibus, novella = _lib_book(1, "The Harbour Stories", 43000, sig), _lib_book(2, "Low Tide", 8000, sig)
    validate.library_dups([omnibus, novella])
    assert [(i["code"], i["level"]) for i in omnibus["issues"]] == [("same_text", INFO)]
    assert "contains that book" in omnibus["issues"][0]["msg"]
    novel, other = _lib_book(3, "Harbour", 187000, sig), _lib_book(4, "The Road to Harbour", 186000, sig)
    validate.library_dups([novel, other])
    assert [(i["code"], i["level"]) for i in other["issues"]] == [("wrong_book", WARN)]
    # two volumes of one book that open with the same front matter are not a wrong book
    vol1, vol2 = _lib_book(5, "Tower, Volume 1", 272000, sig, list(range(64))), _lib_book(6, "Tower, Volume 2", 273000, sig)
    validate.library_dups([vol1, vol2])
    assert [i["code"] for i in vol1["issues"]] == ["same_text"]


def test_volume_numbers_and_subtitles_keep_titles_apart():
    k = validate._title_key
    assert k("Lantern Road (I)") != k("Lantern Road (II)")
    assert k("Harbour: The Second Voyage") != k("Harbour")
    assert k("The Way of Rivers") == k("Way of Rivers (River Saga 1)")


def test_end_of_book_web_address_is_not_cut_off(epub):
    chaps = chapters()
    chaps[-1][1].append("More about the author and a complete list of the novels, the short stories and the plays "
                        "of the Harbour series can be found on www.example-author.co.uk")
    assert "cut_off" not in found(epub(chaps))


def test_text_that_stops_inside_a_word_is_replace(epub):
    chaps = chapters()
    chaps[-1][1].append("She closed the ledger and told them that the ships would sail at first light, all of "
                        "them, whatever the weather on the grey water, to the old harbour at Kestr")
    assert found(epub(chaps)) == {"cut_off": REPLACE}


def test_appendix_after_the_story_is_not_cut_off(epub):
    chaps = chapters()
    chaps[-1][1].extend(["APPENDIX", "THE RULERS OF THE NORTH, " + ", ".join(
        f"Ser Knight{n} of House Name{n}" for n in range(8)) + ","])
    assert "cut_off" not in found(epub(chaps))


def _word_chapters(n=12):
    words = "One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve".split()
    per = len(PARAS) // n
    return [(f"Chapter {words[i]}", PARAS[i * per:(i + 1) * per]) for i in range(n)]


def test_misspelt_chapter_number_is_no_gap(epub):
    chaps = _word_chapters()
    chaps[10] = ("Chapter Elevven", chaps[10][1])  # a misprint such as CHAPTER ELEVVEN
    assert "chapter_gap" not in found(epub(chaps))


def test_heading_run_into_its_first_sentence_is_no_gap(epub):
    chaps = chapters()
    _, paras = chaps[4]
    chaps[4] = ("", [f"CHAPTER V. {paras[0]}"] + paras[1:])
    assert "chapter_gap" not in found(epub(chaps))


def test_letter_openings_and_sign_offs_are_not_running_heads(epub):
    out = []
    for h, ps in chapters():  # a novel in letters: two letters a chapter
        half = len(ps) // 2
        out.append((h, ["Dear friend,"] + ps[:half] + ["Love always, Sam", "Dear friend,"] + ps[half:]
                    + ["Love always, Sam"]))
    assert "headers" not in found(epub(out))


def test_hyphenated_spellings_are_not_print_leftovers(epub):
    chaps = chapters()
    for _, ps in chaps:
        ps += ["They meant to pre-empt the move, and the band played wah-wah once more."] * 4
    assert "hyphens" not in found(epub(chaps))


def test_author_web_site_is_not_a_watermark(epub):
    chaps = chapters()
    for _, ps in chaps:
        ps.append("Visit www.janeausten.com for the reading guide.")
    assert "watermark" not in found(epub(chaps))


def test_publisher_address_on_each_copyright_page_is_not_a_watermark(epub):
    chaps = chapters()
    for _, ps in chaps:  # an omnibus: one copyright page per book
        ps[3:3] = ["Copyright © 2008 by the author. All rights reserved.", "www.harpercollins.co.uk"]
    assert "watermark" not in found(epub(chaps))


def test_question_mark_in_a_web_address_is_not_garbage(epub):
    chaps = chapters()
    for _, ps in chaps:
        ps.append("See www.example-data.org/index.php?id=1368 and www.example-data.org/a.php?pt=2 for the data.")
    assert "garbage" not in found(epub(chaps))


def test_teaser_page_before_the_story_is_not_a_duplicate(epub):
    chaps = chapters()
    teaser, n = [], 0
    for p in chaps[6][1]:
        teaser.append(p)
        n += len(p.split())
        if n >= 300:
            break
    chaps.insert(0, ("", list(teaser)))  # a tie-in novel: a scene from later, before chapter 1
    got = found(epub(chaps))
    assert "duplicate_text" not in got and got.get("repeat_by_design") == INFO


def test_quoted_passage_is_not_a_duplicate(epub):
    chaps = chapters()
    words = " ".join(chaps[1][1]).split()[:150]
    chaps[7][1].append("She read the letter aloud, and it began this way: “" + " ".join(words) + "”")
    assert "duplicate_text" not in found(epub(chaps))


def test_list_of_user_names_is_not_lost_text(epub):
    chaps = chapters()
    chaps[3][1][2:2] = ["reedwarbler,", "tidepoolx,", "quillmoss,", "brackenfox,", "lowfenn,", "copperkite,",
                        "marshhen,", "slatewren,", "ashgrovey,", "fennick,"]  # a technical book's contributors
    assert "lost_text" not in found(epub(chaps))


def test_display_equation_between_halves_of_a_sentence_is_not_broken_lines(epub):
    chaps, imgs = _with_pictures(mid_sentence=True, size=(300, 40), per_chapter=8)
    assert "broken_lines" not in found(epub(chaps, html_paras=True, images=imgs))


def test_english_text_with_another_language_in_the_metadata_is_checked(epub):
    doc = books.read(str(epub()), "epub", "/tmp", "test")
    res = checks.check(doc, dict(META, language="rus"))
    got = {i["code"]: i["level"] for i in res["issues"]}
    assert got == {"language_metadata": WARN} and "nonword_pct" in res["metrics"]


def test_folder_not_in_the_database_is_an_orphan():
    books_ = [{"id": 700, "folder": "Jane Doe/The First Book (700)", "issues": []},
              {"id": 700, "folder": "John Roe/Another Book (700)", "issues": []}]
    validate.orphans(books_, {700: {"path": "John Roe/Another Book (700)"}})
    assert [i["code"] for i in books_[0]["issues"]] == ["orphan_folder"] and not books_[1]["issues"]


def test_italic_runs_and_bold_text_are_counted(epub):
    css = ".thought { font-style: italic } .body { font-weight: bold }"
    chaps = [(h, [f'<p class="body">{html.escape(p)} <i>Run.</i> <span class="thought">He thought so.</span></p>'
                  for p in ps]) for h, ps in chapters()]
    doc = books.read(str(epub(chaps, html_paras=True, css=css)), "epub", "/tmp", "test")
    st = doc.info["style"]
    assert st["italic_runs"] >= 2 * sum(len(ps) for _, ps in chapters())
    assert st["bold_words"] >= 0.9 * st["words"] - 50  # headings are not in the bold class


def test_drop_caps_split_from_the_first_word(epub):
    out = [(h, ["D", "espite all that had passed, they walked on together through the rain."] + ps)
           for h, ps in chapters()]  # "D ¶ espite" at every chapter start
    got = found(epub(out))
    assert got.get("drop_caps") == FIX and "lost_text" not in got


def test_lines_in_another_language_are_not_non_words(epub):
    chaps = chapters()
    verse = ["la casa tiene muchas ventanas y el jardín da al río que baja de la sierra",
             "en el verano los niños juegan en la plaza hasta que la luna sale sobre el monte"]
    for _, ps in chaps:
        ps += verse * 6  # a bilingual edition prints the Spanish poems beside the English
    assert "nonwords" not in found(epub(chaps))


def test_whole_file_repeated_is_fix(epub):
    chaps = chapters()
    chaps.insert(5, chaps[4])  # one chapter twice, as two identical files
    got = found(epub(chaps))
    assert got.get("duplicate_file") == FIX and "duplicate_text" not in got


def test_mojibake_and_soft_hyphens_are_fix(epub):
    chaps = [(h, [p.replace("’", "â€™") for p in ps] + ["It was poi?sonous, and the inno?cence of it all was gone."] * 4)
             for h, ps in chapters()]
    got = found(epub(chaps))
    assert got.get("mojibake") == FIX and got.get("hyphens") == FIX


def test_footnote_marks_are_not_glued_to_words(epub):
    chaps = [(h, [f"<p>{html.escape(p)}</p>" for p in ps] + ['<p>He was cheerfully announced<sup><a href="#n1">fn36</a></sup> '
                                                             'and could be quite creative.<sup>35</sup> Then he left.</p>'] * 20)
             for h, ps in chapters()]
    doc = books.read(str(epub(chaps, html_paras=True)), "epub", "/tmp", "test")
    text = " ".join(t for p in doc.parts for _, t in p.blocks)
    assert "announcedfn36" not in text and "announced" in text


# ---------------------------------------------------------------- signs of a damaged source

def _strip_open_quotes(chaps, limit=None):
    """Drop the opening quote of speeches that close with a speech tag (as in some old scans)."""
    n, out = 0, []
    for h, ps in chaps:
        new = []
        for p in ps:
            if p.startswith("“") and re.search(r",” (?:said|replied|cried)", p) and (limit is None or n < limit):
                p, n = p[1:], n + 1
            new.append(p)
        out.append((h, new))
    return out


def test_lost_opening_quotes(epub):
    got = found(epub(_strip_open_quotes(chapters())))
    assert got.get("speech_marks") == WARN


def test_hyphens_where_dashes_belong(epub):
    chaps = chapters()
    for _, ps in chaps:
        ps += ["He paused-and then he left the room without a word."] * 2
    assert found(epub(chaps)).get("dashes") == WARN


def test_no_space_after_full_stops(epub):
    rnd = random.Random(3)
    chaps = [(h, [re.sub(r"([a-z])\. ([A-Z])", lambda mm: mm.group(1) + "." + mm.group(2) if rnd.random() < 0.3
                         else mm.group(0), p) for p in ps]) for h, ps in chapters()]
    assert found(epub(chaps)).get("ocr_punctuation") == WARN


def test_digits_inside_words(epub):
    rnd = random.Random(4)
    def mark(w):
        return w[:2] + "1" + w[2:] if w.isalpha() and w.islower() and len(w) > 4 and rnd.random() < 0.02 else w
    chaps = [(h, [" ".join(mark(w) for w in p.split(" ")) for p in ps]) for h, ps in chapters()]
    assert found(epub(chaps)).get("digits_in_words") == WARN


def test_no_italics_alone_is_a_note_and_with_small_signs_an_old_etext(epub, monkeypatch):
    monkeypatch.setitem(checks.STYLE_LIMITS, "italic_min_words", 5000)
    meta = dict(META, tags=["Fantasy"])
    plain = checks.check(books.read(str(epub()), "epub", "/tmp", "t"), meta)
    assert {i["code"]: i["level"] for i in plain["issues"]} == {"no_italics": INFO}
    chaps = _strip_open_quotes(chapters(), limit=8)  # too few for speech_marks, enough for a small sign
    for _, ps in chaps[:4]:
        ps += ["He paused-and then he left the room without a word."] * 2
    worn = checks.check(books.read(str(epub(chaps)), "epub", "/tmp", "t"), meta)
    assert {i["code"]: i["level"] for i in worn["issues"]} == {"old_etext": WARN}


# ---------------------------------------------------------------- false alarms from a rescan

def test_names_with_an_inner_capital_are_not_spliced_words(epub):
    chaps = chapters()
    for _, ps in chaps:  # invented names: the author's spellings, many times
        ps += ["Ka’elDor stood in the hall, and the vEnmade knelt before Ro’elDor."] * 2
    assert "ocr_punctuation" not in found(epub(chaps))
    rnd = random.Random(7)
    for k, (_, ps) in enumerate(chaps[:8]):  # "tWhienter": each splice is new
        ps.append(f"He looked at the dY{''.join(rnd.choice('aeioubcdfg') for _ in range(8))} for a while.")
    assert found(epub(chaps)).get("ocr_punctuation") == REPLACE


def test_to_die_is_a_verb_not_ocr(epub):
    chaps = chapters()
    for _, ps in chaps:
        ps.append("They were all going to die in the morning, and the soldiers were ready to die for the king.")
    assert "ocr_punctuation" not in found(epub(chaps))


def _mix_quotes(chaps, share, seed=6):
    rnd = random.Random(seed)
    return [(h, [re.sub(r"(^|\s)“", lambda mm: mm.group(1) + '"' if rnd.random() < share else mm.group(0), p)
                 for p in ps]) for h, ps in chaps]


def test_mixed_quote_marks_count_when_most_speeches_mix_them(epub):
    assert "speech_marks" not in found(epub(_mix_quotes(chapters(), 0.15)))  # clean copies: 10-23%
    assert found(epub(_mix_quotes(chapters(), 0.8))).get("speech_marks") == WARN  # a bad copy: 63%


def test_short_page_printed_twice_is_by_design(epub):
    epigraph = ("The river keeps no account of the boats it carries. It turns at the old mill, runs under the "
                "stone bridge and on past the fields, and by the time it reaches the sea it has forgotten every "
                "name it was given on the way. Those who follow it to the end find only salt and light, and a "
                "wide grey water that does not answer.")
    chaps = [("", [epigraph])] + chapters() + [("", [epigraph])]  # the epigraph again at the end
    got = found(epub(chaps))
    assert "duplicate_file" not in got and "duplicate_text" not in got and got.get("repeat_by_design") == INFO


def test_stray_letters_after_the_end_are_not_cut_off(epub):
    chaps = chapters()
    chaps[-1][1].append("THE ENDzq")
    assert "cut_off" not in found(epub(chaps))


def test_to_be_written_inside_a_sentence_is_no_draft(epub):
    chaps = chapters()
    chaps[3][1].append("It was the first detective novel to be written in Spanish.")
    assert "draft" not in found(epub(chaps))
    chaps[5][1].append("10.1 To be written 311")  # a draft's contents list
    assert found(epub(chaps)).get("draft") == WARN


def test_countdown_under_the_scene_breaks_is_not_a_running_head(epub):
    out, left = [], 24 * 60
    for h, ps in chapters():  # a countdown: "13 hours, 47 minutes", then "13 hours, 45 minutes"
        new = []
        for k, p in enumerate(ps):
            if k % 6 == 0:
                left -= 13
                new.append(f"{left // 60} hours, {left % 60} minutes")
            new.append(p)
        out.append((h, new))
    assert "headers" not in found(epub(out))


def test_quotation_after_a_colon_and_its_next_paragraph_are_not_a_duplicate(epub):
    chaps = chapters()
    quoted = [p for p in chaps[3][1] if 160 <= len(p.split()) < 195][:2]  # over the WARN limit, under QUOTED_WORDS
    assert len(quoted) == 2
    chaps[7][1].extend(["I wrote a foreword to the first edition, which began this way:", quoted[0],
                        "It went on, after a page about the village, to say this.", quoted[1]])
    assert "duplicate_text" not in found(epub(chaps))


def test_chapter_headings_in_the_notes_are_no_gap(epub):
    chaps = chapters()  # the notes skip the chapters with no notes
    chaps.append(("Notes", [x for n in (2, 3, 7, 9) for x in (
        f"CHAPTER {n}", f"{n}. See the letters of 1811, which the family kept at Longbourn until the death of "
                        "Mr. Bennet; they show that the entail was drawn up in 1790 and never changed.")]))
    assert "chapter_gap" not in found(epub(chaps))


def test_scanner_skips_the_blank_pages_before_a_part_opening(tmp_path):
    def gaps(part_page):
        pages = pdf_pages()
        numbers = list(range(1, len(pages) + 1))
        numbers = numbers[:14] + [""] + [n + 2 for n in numbers[15:]]  # printed pages 16 and 17 not scanned
        if part_page:
            pages[14] = ["PART THREE", "A Breakthrough in Studies"]
        doc = books.read(str(make_pdf(tmp_path / "b.pdf", pages, numbers)), "pdf", str(tmp_path), "t")
        doc.info["scanned"] = True
        out = []
        checks._page_gaps(doc, {}, out)
        return {i["code"]: i["level"] for i in out}
    assert gaps(True) == {} and gaps(False) == {"page_gap": WARN}


def test_comic_that_reuses_a_picture_far_away_has_no_duplicate_page():
    rnd = random.Random(5)
    vecs = [[rnd.random() for _ in range(64)] for _ in range(40)]
    vecs[29] = list(vecs[9])  # a comic: chapters 5 and 6 open on the same picture
    def pages(colour):
        doc = books.Doc("pdf", "x.pdf", parts=[books.Part(f"page {i + 1}", []) for i in range(40)], reflow=False,
                        info={"thumbs": [(i, 60.0, v) for i, v in enumerate(vecs)], "colour": colour})
        out = []
        checks._pdf_pages(doc, {}, out)
        return {i["code"]: i["level"] for i in out}
    assert pages(0.0) == {"duplicate_page": WARN} and pages(0.2) == {}


def _page_splits(chaps, speeches=0.0, prose=0.09, seed=8):
    """Paragraphs split at print page ends, mid-sentence ("the station gate.
    ¶ communications technicians") and, with speeches > 0, inside a speech at a sentence end
    ("…how would I study them? ¶ No, you're right.”")."""
    rnd = random.Random(seed)
    def split(ps):
        out = []
        for p in ps:
            w = p.split(" ")
            k = next((i for i in range(len(w) // 2, len(w) - 3) if w[i + 1][:1].islower()), None)
            mm = re.match(r"“[^”]*?[.?!] (?=[A-Z])", p)
            if len(p) >= 200 and k and rnd.random() < prose:
                out += [" ".join(w[:k + 1]), " ".join(w[k + 1:])]
            elif mm and rnd.random() < speeches:
                out += [p[:mm.end()].rstrip(), p[mm.end():]]
            else:
                out.append(p)
        return out
    return [(h, split(ps)) for h, ps in chaps]


def test_page_splits_in_four_percent_of_the_paragraphs_are_broken_lines(epub):
    chaps = _page_splits(chapters())
    doc = books.read(str(epub(chaps)), "epub", "/tmp", "t")
    res = checks.check(doc, META)
    got = {i["code"]: i["level"] for i in res["issues"]}
    assert 3.0 <= res["metrics"]["broken_pct"] < 5.0 and got.get("broken_lines") == FIX, res["metrics"]["broken_pct"]


def test_ocr_garbled_chapter_headings_count_as_present():
    def present(heading, n):  # a scan whose decorative capitals were misread
        blocks = [(0, "p", "CHAPTER SEVEN"), (0, "p", "He walked on. " * 20), (0, "p", heading),
                  (0, "p", "She stayed. " * 20), (0, "p", "CHAPTER ELEVEN")]
        return checks._heading_present(blocks, 0, 4, {n}, "chapter")
    assert present("CHAPITER EIGHT", 8) and present("CHAPTER CEN", 10) and present("CHAPTER", 9)
    assert present("CHAPtER EIQHt", 8) and present("CHAPTER 'CHIR'CEEN", 13)
    assert not present("CHAPTER SIX", 8) and not present("Chapters of the old book were lost in the fire.", 8)


SPLIT_BEFORE_A_NAME = ["And as he thought that, as the lamps along the quay went out one by one, he believed he saw a "
                       "figure much like a ferryman, though surely more than one, and it was likely only",
                       "Marten’s imagination, but even so, it raised the old sailor’s hopes."]  # a page split before a name


def test_page_split_in_the_last_paragraph_is_not_cut_off(epub):
    chaps = chapters()
    chaps[-1][1].extend(SPLIT_BEFORE_A_NAME + ["The tide would turn again."])
    assert "cut_off" not in found(epub(chaps))




# ---------------------------------------------------------------- faults found by reviewing the code (issue #1)

def _corrupt(path, name, at=0.5):
    """Flip one byte of a file's compressed data inside the zip."""
    with zipfile.ZipFile(path) as z:
        info = z.getinfo(name)
    raw = bytearray(path.read_bytes())
    start = info.header_offset + 30 + len(info.filename) + len(info.extra)
    raw[start + int(at * info.compress_size)] ^= 0xFF
    path.write_bytes(raw)


def _rewrite(path, name, fn):
    """Change one file inside the zip."""
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    files[name] = fn(files[name])
    with zipfile.ZipFile(path, "w") as z:
        for n, data in files.items():
            z.writestr(n, data, compress_type=zipfile.ZIP_STORED if n == "mimetype" else zipfile.ZIP_DEFLATED)
    return path


@pytest.mark.parametrize("at", [0.5, 0.9])
def test_damaged_file_in_the_zip_is_replace_and_the_rest_is_still_read(epub, at):
    path = epub()
    _corrupt(path, "OEBPS/ch3.xhtml", at)
    got = found(path)
    assert got["damaged"] == REPLACE and "checker_error" not in got and "missing_files" not in got
    names = [p.name for p in books.read_epub(str(path)).parts]
    assert len(names) == 9 and "OEBPS/ch3.xhtml" not in names


def test_damaged_package_file_is_replace(epub):
    path = epub()
    _corrupt(path, "OEBPS/content.opf")
    assert found(path).get("damaged") == REPLACE


def _scan_pdf(path, twin=None):
    """A PDF of page pictures with no text layer. twin: (page, page it repeats)."""
    from PIL import Image, ImageDraw
    pages = []
    for p in range(12):
        rnd = random.Random(twin[1] if twin and p == twin[0] else p)
        im = Image.new("L", (600, 900), 255)
        draw = ImageDraw.Draw(im)
        for _ in range(60):
            x, y = rnd.randint(0, 560), rnd.randint(0, 880)
            draw.rectangle([x, y, x + rnd.randint(10, 60), y + 6], fill=0)
        pages.append(im)
    pages[0].save(path, save_all=True, append_images=pages[1:], resolution=200.0)
    return path


def test_repeated_page_in_a_scan_with_no_text_layer(tmp_path):
    assert found(_scan_pdf(tmp_path / "plain.pdf"), "pdf") == {"image_only": WARN}
    got = found(_scan_pdf(tmp_path / "twin.pdf", twin=(6, 5)), "pdf")
    assert got == {"image_only": WARN, "duplicate_page": REPLACE}


def _one_file(*first, blocks):
    return books.Doc("epub", "x", parts=[books.Part("index_split_000.html", [("p", t) for t in first] + blocks)])


def test_also_by_line_does_not_hide_the_damage_in_a_one_file_book():
    twice = [("p", p) for p in PARAS[:60] + PARAS[:60]]
    for doc in (_one_file(blocks=twice), _one_file("Also by Jane Austen", blocks=twice)):
        assert {i["code"]: i["level"] for i in checks.check(doc, META)["issues"]} == {"duplicate_text": REPLACE}


def test_a_folder_name_or_a_word_in_a_file_name_is_not_back_matter():
    text = [("p", "It was a dark night. " * 40)]
    part = lambda name, blocks=text: books.Part(name, list(blocks))
    assert not checks._is_back(part("Notes/ch1.xhtml")) and not checks._is_back(part("OEBPS/Credits/ch1.xhtml"))
    # index.html can hold the whole book
    assert checks._back_matter(books.Doc("epub", "x", parts=[part("OEBPS/index.html")])) == set()
    # every file of "Notes on a Scandal" says notes
    scandal = [part(f"notes-on-a-scandal-{i:02}.xhtml") for i in range(30)]
    assert checks._back_matter(books.Doc("epub", "x", parts=scandal)) == set()
    # a real notes file and a real index stay back matter, whatever else is in the book
    parts = [part("OEBPS/index.html", text * 20)] + [part(f"ch{i}.xhtml") for i in range(40)] + [part("OEBPS/notes.xhtml")]
    assert checks._back_matter(books.Doc("epub", "x", parts=parts)) == {0, 41}
    whole = [part("OEBPS/index.html", text * 400), part("copyright.xhtml"), part("acknowledgments.xhtml")]
    assert checks._back_matter(books.Doc("epub", "x", parts=whole)) == {1, 2}


def test_a_long_file_that_opens_with_a_copyright_line_is_the_story():
    long_text, short_text = [("p", "word " * 6000)], [("p", "word " * 300)]
    opens = lambda head, body: checks._is_back(books.Part("part0001.html", [("p", head)] + body))
    assert opens("Copyright 1999 by the author", short_text) and not opens("Copyright 1999 by the author", long_text)
    assert opens("Also by Jane Austen", short_text) and not opens("Also by Jane Austen", long_text)
    assert opens("Notes", long_text) and opens("Glossary", long_text)  # these can run long


@pytest.mark.parametrize("cut", [(4, 5), (3, 6), (3, 7), (2, 7)])
def test_a_chapter_gap_of_any_size_is_reported(epub, cut):
    chaps = chapters()
    del chaps[cut[0]:cut[1]]
    assert found(epub(chaps)) == {"chapter_gap": WARN}


def test_misnumbered_heading_far_ahead_is_no_gap(epub):
    chaps = chapters()
    chaps[8] = ("Chapter XIX", chaps[8][1])  # 8, 19, 10: a typing fault in chapter 9
    assert found(epub(chaps)) == {}


def test_a_hundred_numbers_per_part_is_no_gap(epub):
    chaps = [(f"Chapter {n}", ps) for n, (_, ps) in zip([1, 2, 3, 4, 5, 101, 102, 103, 104, 105], chapters())]
    assert found(epub(chaps)) == {}


def test_undeclared_prefix_is_an_inline_element(epub, tmp_path):
    path = epub(extra_html="<p>One <span>two<o:p></o:p></span> three.</p>")
    assert found(path) == {"malformed_xhtml": FIX}  # the other checks still run
    blocks = [t for p in books.read_epub(str(path)).parts for _, t in p.blocks]
    assert "One two three." in blocks


def test_undeclared_prefix_in_metadata_opf_does_not_stop_the_scan(tmp_path):
    folder = tmp_path / "Jane Austen" / "Emma (1)"
    folder.mkdir(parents=True)
    (folder / "metadata.opf").write_text('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
                                         "<metadata><dc:title>Emma</dc:title></metadata></package>", encoding="utf-8")
    assert [b["title"] for b in validate.scan_library(str(tmp_path))] == ["Emma (1)"]  # the folder name stands in


def test_token_with_many_ones_is_a_number_and_does_not_blow_up():
    assert W.classify("0b" + "1" * 64) == (W.OTHER, None)
    assert W.classify("1ittle") == (W.OCR, "little")


@pytest.mark.parametrize("reply", ['{"rating": 5, "examples": null, "problems": null, "passages": null, "note": null}',
                                   '{"rating": 5, "examples": "tlie", "problems": 3, "passages": 4}',
                                   '{"rating": 5}'])
def test_llm_reply_with_odd_fields_keeps_its_rating(tmp_path, monkeypatch, reply):
    class Reply:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": reply}}]}
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: Reply())
    got = llm.review({"pages1": "x"}, "T", "key", str(tmp_path))
    assert (got["rating"], got["examples"], got["problems"], got["passages"], got["note"]) == (5, [], [], [], "")


def test_write_json_leaves_no_half_file(tmp_path):
    path = tmp_path / "x.json"
    books.write_json(str(path), {"a": 1})
    with pytest.raises(TypeError):
        books.write_json(str(path), {"a": object()})
    assert books.read_json(str(path)) == {"a": 1} and os.listdir(tmp_path) == ["x.json"]


def _book(path, fmt, title="Test Book"):
    return {"title": title, "authors": ["Jane Austen"], "language": "en", "tags": [],
            "formats": [{"fmt": fmt, "path": str(path)}]}


def _results(blist):
    return [{i["code"]: i["level"] for i in b["formats"][0]["result"]["issues"]} for b in blist]


def test_truncated_cache_files_are_read_again(epub, tmp_path):
    cache, path = tmp_path / "cache", epub()
    first = _book(path, "epub")
    validate.run_checks([first], str(cache), 1)
    for damaged in (first["formats"][0]["cache"], str(cache / "hashes.json")):
        whole = open(damaged, "rb").read()
        with open(damaged, "wb") as f:
            f.write(whole[:len(whole) // 2])
        again = _book(path, "epub")
        validate.run_checks([again], str(cache), 1)
        assert _results([again]) == [{}]


def test_truncated_llm_answer_is_asked_again(tmp_path, monkeypatch):
    asked = []
    class Reply:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": '{"rating": 4, "note": "fine"}'}}]}
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: asked.append(1) or Reply())
    assert llm.review({"pages1": "x"}, "T", "key", str(tmp_path))["rating"] == 4
    answer = next((tmp_path / "llm").iterdir())
    answer.write_text('{"rating": 4, "no')
    assert llm.review({"pages1": "x"}, "T", "key", str(tmp_path))["rating"] == 4 and len(asked) == 2


def _calibre_db(folder, extra=""):
    import sqlite3
    db = sqlite3.connect(folder / "metadata.db")
    db.executescript(f"""
        create table books (id integer primary key, title text, author_sort text, path text);
        create table authors (id integer primary key, name text, sort text);
        create table books_authors_link (id integer primary key, book integer, author integer);
        create table languages (id integer primary key, lang_code text);
        create table books_languages_link (id integer primary key, book integer, lang_code integer, item_order integer);
        create table identifiers (id integer primary key, book integer, type text, val text);
        insert into books values (1, 'Emma', 'Austen, Jane', 'Jane Austen/Emma (1)');
        insert into authors values (1, 'Jane Austen', 'Austen, Jane');
        insert into books_authors_link values (1, 1, 1);
        insert into languages values (1, 'eng');
        insert into books_languages_link values (1, 1, 1, 0);
        {extra}""")
    db.commit()
    db.close()


def test_rows_for_a_book_added_during_the_read_are_skipped(tmp_path):
    _calibre_db(tmp_path, """insert into books_authors_link values (2, 99, 1);
                             insert into books_languages_link values (2, 99, 1, 0);
                             insert into identifiers values (1, 99, 'isbn', '9780553582024');""")
    assert set(M.from_db(str(tmp_path))) == {1}


@pytest.mark.parametrize("name", ["Books #1", "What?", "100% books", "a%20b", "Библиотека"])
def test_library_path_with_url_characters(tmp_path, name):
    (tmp_path / name).mkdir()
    _calibre_db(tmp_path / name)
    assert M.from_db(str(tmp_path / name))[1]["authors"] == ["Jane Austen"]


def test_unreadable_database_says_so(tmp_path, capsys):
    (tmp_path / "metadata.db").write_bytes(b"not a database, not at all" * 100)
    assert M.from_db(str(tmp_path)) is None and "metadata.db cannot be read" in capsys.readouterr().err


def test_ancestor_class_in_a_pre_rule_is_not_preformatted(epub):
    css = "div.chapter p.verse { white-space: pre; font-family: monospace }"
    assert books.pre_classes_of([css]) == books.mono_classes_of([css]) == {"verse"}
    paras = lambda ps: ['<div class="chapter"><p>' + html.escape(textwrap.fill(p, 70)) + "</p></div>" for p in ps]
    chaps = [(h, paras(ps)) for h, ps in chapters()]
    assert found(epub(chaps, html_paras=True, raw_newlines=True, css=css)) == {}


def _french(end):
    rnd = random.Random(3)
    who = "Le vieux marin|La jeune femme|Mon frère|Notre voisin|Le médecin|La servante|Le capitaine|Ma tante".split("|")
    did = "regardait cherchait oubliait portait traversait écoutait attendait quittait".split()
    what = ("la maison blanche|le chemin de pierre|une lettre ancienne|le vent du soir|la rivière gelée|"
            "le jardin abandonné|la porte fermée|le pont de bois").split("|")
    when = ("avant la nuit|sans dire un mot|pendant la pluie|au milieu de la foule|comme chaque matin|"
            "malgré la fatigue|quand les cloches sonnaient|près de la fenêtre").split("|")
    para = lambda: " ".join(f"{rnd.choice(who)} {rnd.choice(did)} {rnd.choice(what)} {rnd.choice(when)}." for _ in range(5))
    chaps = [(f"Chapitre {ROMAN[c]}", [para() for _ in range(14)]) for c in range(6)]
    chaps[-1][1].append(end)
    return chaps


def _french_found(epub, end):
    doc = books.read(str(epub(_french(end))), "epub", "/tmp", "t")
    return {i["code"]: i["level"] for i in checks.check(doc, dict(META, language="fra"))["issues"]}


@pytest.mark.parametrize("end", ["Achevé à Yasnaïa Poliana", "Ende", "Конец", "Il partit vers le nord et ne revint jamais."])
def test_last_word_of_a_book_in_another_language_is_not_cut_off(epub, end):
    assert _french_found(epub, end) == {}


def test_book_in_another_language_that_stops_mid_sentence_warns(epub):
    end = " ".join(["Le vieux marin regardait la maison blanche et le chemin de pierre"] * 3) + " et puis"
    assert _french_found(epub, end) == {"cut_off": WARN}


def test_sample_notice_in_a_short_book_is_replace_and_in_a_long_book_warn(epub, monkeypatch):
    chaps = chapters()
    chaps[-1][1].append("End of excerpt")
    assert found(epub(chaps)) == {"sample": REPLACE}
    monkeypatch.setattr(checks, "SAMPLE_WHOLE", 5000)
    assert found(epub(chaps)) == {"sample": WARN}


def test_sample_notice_in_back_matter_is_warn(epub):
    chaps = chapters()
    chaps.append(("Excerpt from the next book", [chaps[0][1][0], "End of excerpt"]))
    assert found(epub(chaps)).get("sample") == WARN


def test_sample_phrase_in_a_speech_is_no_notice(epub):
    chaps = chapters()
    chaps[-1][1].insert(1, "“This is a sample of her hand,” the clerk said.")
    assert found(epub(chaps)) == {}


def test_same_document_link_in_the_nav_is_not_broken(epub):
    landmarks = (b'<nav epub:type="landmarks"><ol><li><a epub:type="toc" href="#toc">Contents</a></li></ol></nav>'
                 b'<nav id="toc" epub:type="toc">')
    path = _rewrite(epub(), "OEBPS/nav.xhtml", lambda d: d.replace(b'<nav epub:type="toc">', landmarks))
    assert found(path) == {}


def _converter(tmp_path, name, body, version=":"):
    """A fake ebook-convert. --version (books.start_calibre) runs only `version`."""
    script = tmp_path / name
    script.write_text(f'#!/bin/sh\nif [ "$1" = --version ]; then {version}; exit 0; fi\n{body}\n')
    script.chmod(0o755)
    return str(script)


def _mobi(folder, name="a.mobi", data=b"stable bytes" * 40):
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(data)
    return folder / name


@pytest.mark.parametrize("fail,level", [("exit 1", REPLACE), ("exit 137", WARN), ("kill -9 $$", WARN)])
def test_failed_conversion_is_tried_again_on_the_next_run(epub, tmp_path, monkeypatch, fail, level):
    cache, mobi = tmp_path / "cache", _mobi(tmp_path / "lib")
    monkeypatch.setenv("QA_EBOOK_CONVERT", _converter(tmp_path, "fail.sh", fail))
    first = _book(mobi, "mobi")
    validate.run_checks([first], str(cache), 1)
    assert _results([first]) == [{"convert_failed": level}] and not os.listdir(cache / "results")
    monkeypatch.setenv("QA_EBOOK_CONVERT", _converter(tmp_path, "good.sh", f'cp "{epub()}" "$2"'))
    second = _book(mobi, "mobi")
    validate.run_checks([second], str(cache), 1)
    assert _results([second]) == [{}] and os.listdir(cache / "results")


def test_file_not_read_for_lack_of_a_tool_is_read_again(tmp_path, monkeypatch):
    cache, djvu = tmp_path / "cache", tmp_path / "a.djvu"
    djvu.write_bytes(b"AT&TFORM" + b"\0" * 200)
    empty = tmp_path / "nothing"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    b = _book(djvu, "djvu")
    validate.run_checks([b], str(cache), 1)
    assert _results([b]) == [{"not_read": INFO}] and not os.listdir(cache / "results")


def test_two_copies_of_a_file_can_convert_at_the_same_time(epub, tmp_path, monkeypatch):
    source = epub()
    half = os.path.getsize(source) // 2
    monkeypatch.setenv("QA_EBOOK_CONVERT", _converter(tmp_path, "slow.sh", (
        f'sleep 0.5\nhead -c {half} "{source}" > "$2"\nsleep 0.3\ntail -c +{half + 1} "{source}" >> "$2"')))
    cache = tmp_path / "cache"
    blist = [_book(_mobi(tmp_path / folder), "mobi", folder) for folder in ("one", "two")]
    validate.run_checks(blist, str(cache), 2)
    assert _results(blist) == [{}, {}]
    conv = os.listdir(cache / "conv")
    assert len(conv) == 1 and zipfile.ZipFile(cache / "conv" / conv[0]).testzip() is None


@pytest.mark.parametrize("jobs", [1, 2])
def test_worker_that_dies_costs_one_file_not_the_run(epub, tmp_path, monkeypatch, jobs):
    monkeypatch.setenv("QA_EBOOK_CONVERT", _converter(tmp_path, "kills.sh", (
        f'case "$1" in *kills*) kill -9 $PPID; sleep 5;; esac\ncp "{epub()}" "$2"')))
    cache = tmp_path / "cache"
    names = ["a", "b", "kills", "c", "d"]
    blist = [_book(_mobi(tmp_path / "lib", f"{n}.mobi", n.encode() * 50), "mobi", n) for n in names]
    validate.run_checks(blist, str(cache), jobs)
    got = dict(zip(names, _results(blist)))
    assert got.pop("kills") == {"checker_error": WARN} and all(v == {} for v in got.values())
    assert len(os.listdir(cache / "results")) == 4  # the next run tries the one that died again


def test_authors_in_other_scripts_have_their_own_keys():
    assert M._key("Лев Толстой") != M._key("Фёдор Достоевский") != M._key("村上春樹") != ""
    assert M._key("Фёдор Достоевский") == M._key("Федор Достоевский")  # one person, accents aside
    assert M._key("China Miéville") == M._key("china mieville")
    assert _meta((["Лев Толстой"], "Толстой, Лев", "rus", []), (["Фёдор Достоевский"], "Достоевский, Фёдор", "rus", [])) == {}


def test_name_suffix_after_a_comma_is_not_a_first_name():
    assert M.fixed("Martin Luther King, Jr.") == "Martin Luther King, Jr."
    assert M.fixed("Austen, Jane") == "Jane Austen"
    assert _meta((["Martin Luther King, Jr."], "Martin Luther King, Jr.", "eng", [])) == {}


def test_one_letter_names_and_particles_are_not_initials():
    assert M.initials("Malcolm X") == "Malcolm X" and M.initials("Thomas a Kempis") == "Thomas a Kempis"
    assert M.initials("Tolkien J R R") == "Tolkien J.R.R." and M.initials("Malcolm X.") == "Malcolm X."


def test_titles_in_other_scripts_have_their_own_keys():
    assert validate._title_key("Война и мир") != validate._title_key("Анна Каренина")
    war, anna = _lib_book(1, "Война и мир", 1000, None), _lib_book(2, "Анна Каренина", 1000, None)
    copy = _lib_book(3, "Война и мир", 1000, None)
    validate.library_dups([war, anna, copy])
    assert [i["code"] for i in war["issues"]] == ["same_title"] and anna["issues"] == []


def test_parser_warning_is_not_malformed_xhtml():
    root, errors = books.parse_xml(b'<html xmlns="relative-uri"><body><p>Hello</p></body></html>', recover=False)
    assert root is not None and errors == []


def test_one_bad_character_in_a_short_file_is_no_garbage():
    def codes(bad):
        doc = _one_file(blocks=[("p", f"A short note with {bad} in it.")])
        return {i["code"] for i in checks.check(doc, META)["issues"]}
    assert "garbage" not in codes("one bad � character") and "mojibake" not in codes("one bad â€™ quote")
    assert "garbage" in codes(" ".join(["�"] * 5)) and "mojibake" in codes(" ".join(["â€™"] * 5))


def test_verdict_comes_from_the_file_that_won():
    """Two files of one format: X.epub and x.EPUB beside each other."""
    bad = {"fmt": "epub", "size": 1, "result": {"issues": [issue("damaged", REPLACE, "x")], "metrics": {}, "reflow": True}}
    good = {"fmt": "epub", "size": 1, "result": {"issues": [], "metrics": {}, "reflow": True}}
    b = {"id": 1, "title": "T", "authors": [], "formats": [bad, good]}
    validate.judge(b)
    validate.verdict(b)
    assert b["best_file"] is good and b["verdict"] == "FIX"  # "delete the bad copy", not the bad copy's REPLACE
    assert report._best(b) is good


# ---------------------------------------------------------------- the word list

def _scowl(monkeypatch, content=None, error=None):
    import hashlib
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("final/english-words.35", b"Zebra\napple\nna\xefve\n"),
                           ("final/british-words.80", b"colour\napple\n"),
                           ("final/english-contractions.10", b"don't\n"),
                           ("final/english-words.95", b"junk\n"),
                           ("final/proper-names.35", b"london\n")):
            info = tarfile.TarInfo(f"scowl-2020.12.07/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    body = content or buf.getvalue()

    class Reply:
        content = body

        def raise_for_status(self):
            pass

    def get(*a, **k):
        if error:
            raise error
        return Reply()
    monkeypatch.setattr(W, "SCOWL_SHA256", hashlib.sha256(buf.getvalue()).hexdigest())
    monkeypatch.setattr("requests.get", get)


def test_word_list_is_built_from_the_chosen_scowl_lists(tmp_path, monkeypatch):
    _scowl(monkeypatch)
    path = tmp_path / "new" / "words.txt"
    W.fetch(str(path))
    assert path.read_text(encoding="utf-8").split("\n") == ["apple", "colour", "don't", "naïve", "zebra", ""]
    assert os.listdir(path.parent) == ["words.txt"]
    assert os.stat(path).st_mode & 0o777 == 0o644  # in the Docker image, a non-root user reads the root-made file


def test_word_list_is_fetched_once_when_no_file_exists(tmp_path, monkeypatch):
    _scowl(monkeypatch)
    path = tmp_path / "words.txt"
    monkeypatch.setattr(W, "WORD_FILES", (None, str(tmp_path / "none.txt"), str(path)))
    monkeypatch.setattr(W, "_words", None)
    assert "apple" in W.words() and path.exists()
    monkeypatch.setattr("requests.get", lambda *a, **k: pytest.fail("fetched twice"))
    monkeypatch.setattr(W, "_words", None)
    assert "apple" in W.words()


def test_word_list_with_the_wrong_sha256_is_refused(tmp_path, monkeypatch):
    _scowl(monkeypatch, content=b"something else")
    with pytest.raises(SystemExit, match="wrong sha256"):
        W.fetch(str(tmp_path / "words.txt"))
    assert not os.path.exists(tmp_path / "words.txt")


def test_word_list_download_failure_says_what_to_do(tmp_path, monkeypatch):
    import requests
    _scowl(monkeypatch, error=requests.ConnectionError("no route"))
    with pytest.raises(SystemExit, match="QA_WORDS"):
        W.fetch(str(tmp_path / "words.txt"))


def test_word_list_download_is_tried_again(tmp_path, monkeypatch):
    import requests
    _scowl(monkeypatch)
    good, calls = requests.get, []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise requests.ConnectionError("a bad mirror")
        return good(*a, **k)
    monkeypatch.setattr("requests.get", flaky)
    W.fetch(str(tmp_path / "words.txt"))
    assert len(calls) == 2 and (tmp_path / "words.txt").exists()


def test_env_file_default_is_the_current_folder(tmp_path, monkeypatch):
    (tmp_path / "books").mkdir()
    seen = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(validate, "run_llm", lambda blist, mode, key, *rest: seen.append(key))
    monkeypatch.setattr("sys.argv", ["tidy-calibre-library", "files", "books", "--llm", "all",
                                     "--cache", "c", "--out", "o"])
    with pytest.raises(SystemExit, match="OPENROUTER_API_KEY"):
        validate.main()
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=from-the-folder\n")
    validate.main()
    assert seen == ["from-the-folder"]


def test_parallel_first_runs_of_calibre_do_not_fail(epub, tmp_path, monkeypatch):
    # calibre makes its config folder on its first run, and first runs that start together fail
    # (FileExistsError: .../plugins), which gave a good book REPLACE. This converter fails the same way.
    config = tmp_path / "calibre"
    first_run = f'[ -d "{config}" ] || {{ sleep 1; mkdir "{config}" || exit 1; }}'
    monkeypatch.setenv("QA_EBOOK_CONVERT", _converter(tmp_path, "first.sh", f'{first_run}\ncp "{epub()}" "$2"',
                                                      version=first_run))
    blist = [_book(_mobi(tmp_path / "lib", f"{n}.mobi", n.encode() * 50), "mobi", n) for n in "ab"]
    validate.run_checks(blist, str(tmp_path / "cache"), 2)
    assert _results(blist) == [{}, {}]


def test_undetermined_language_is_no_language(epub):
    # calibre gives a converted file that states no language 'und' (books.convert)
    doc = books.read(str(epub(lang="und")), "epub", "unused", "test")
    assert checks.check(doc, {**META, "language": ""})["issues"] == []


@pytest.mark.skipif(not books.tool("ebook-convert", "QA_EBOOK_CONVERT"), reason="needs calibre's ebook-convert")
def test_every_format_calibre_writes_reads_clean(epub, tmp_path, monkeypatch):
    """The check for a new calibre in the Docker image (CONTRIBUTING.md): each format converts, and
    a format that states no language gets none from a German locale."""
    exe = books.tool("ebook-convert", "QA_EBOOK_CONVERT")
    chaps, images = _with_pictures(False, per_chapter=1)
    source = epub(chaps, html_paras=True, images=images)
    monkeypatch.setenv("CALIBRE_CONFIG_DIRECTORY", str(tmp_path / "calibre"))
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LANGUAGE", "de")
    (tmp_path / "lib").mkdir()
    blist = []
    for fmt in ("mobi", "azw3", "fb2", "pdb", "docx", "rtf", "txt"):
        out = tmp_path / "lib" / f"book.{fmt}"
        subprocess.run([exe, str(source), str(out)], check=True, capture_output=True)
        blist.append({**_book(out, fmt), "language": ""})
    validate.run_checks(blist, str(tmp_path / "cache"), 4)
    assert dict(zip([b["formats"][0]["fmt"] for b in blist], _results(blist))) == {
        b["formats"][0]["fmt"]: {} for b in blist}
