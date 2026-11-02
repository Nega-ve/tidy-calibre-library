"""Build a small demo library, and from it the pictures of the report in this folder:
sample-report.html, report.png and social-preview.png (the last two need Google Chrome).

    python3 docs/make_demo.py

Each book is another stretch of fixture.txt (the opening of Pride and Prejudice, public domain),
so no two books share text. One is clean, one has only a metadata fault, and the others have
one planted fault each, made with the builders in tests.py.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from tests import PARAS, ROMAN, make_epub, mutate  # noqa: E402

CHROME = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", shutil.which("google-chrome"), shutil.which("chromium"))
PER_BOOK = len(PARAS) // 8
OPF = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata '
       'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf"><dc:title>{title}</dc:title>'
       '<dc:creator opf:file-as="{sort}">{author}</dc:creator><dc:language>eng</dc:language></metadata></package>')


def chapters(k):
    paras = PARAS[k * PER_BOOK:(k + 1) * PER_BOOK]
    per = len(paras) // 8
    return [(f"Chapter {ROMAN[i]}", paras[i * per:(i + 1) * per]) for i in range(8)]


def ocr(chaps):
    def misread(w):
        for a, b in (("h", "li"), ("m", "rn"), ("w", "vv"), ("d", "cl")):
            if w.islower() and a in w:
                return w.replace(a, b, 1)
        return w
    return mutate(chaps, misread, 0.08)


def sample(chaps):
    chaps[-1][1].append("This is the end of the free sample. Buy the full book to keep reading.")
    return chaps


def repeated(chaps):
    block = [p for _, ps in chaps[:4] for p in ps]
    chaps[6][1][1:1] = block
    return chaps


def print_lines(chaps):
    return [(h, [textwrap.fill(p, 70) for p in ps]) for h, ps in chaps]


def credits_kept(chaps):
    for _, ps in chaps[::2]:
        ps.insert(2, "Scanned and proofed by J. Doe")
    return chaps


def lost_chapter(chaps):
    del chaps[3]
    return chaps


BOOKS = (("clean copy", None, {}),
         ("a free sample", sample, {}),
         ("a raw OCR scan", ocr, {}),  # the longest stretch: the word checks need 500 words
         ("a repeated block", repeated, {}),
         ("print lines kept", print_lines, {}),
         ("scanner credits", credits_kept, {"author": "Austen, Jane"}),
         ("a lost chapter", lost_chapter, {}),
         ("no cover in calibre", None, {"cover": False}))


def library(root):
    """A calibre library: a folder per book, and a metadata.db."""
    os.makedirs(root)
    db = sqlite3.connect(os.path.join(root, "metadata.db"))
    db.executescript("""
        create table books (id integer primary key, title text, author_sort text, path text);
        create table authors (id integer primary key, name text, sort text);
        create table books_authors_link (id integer primary key, book integer, author integer);
        create table languages (id integer primary key, lang_code text);
        create table books_languages_link (id integer primary key, book integer, lang_code integer, item_order integer);
        create table identifiers (id integer primary key, book integer, type text, val text);
        insert into languages values (1, 'eng');""")
    for k, (what, fault, opts) in enumerate(BOOKS):
        i, title, author = k + 1, f"Pride and Prejudice: {what}", opts.get("author", "Jane Austen")
        rel = f"Jane Austen/Pride and Prejudice_ {what} ({i})"
        folder = os.path.join(root, rel)
        os.makedirs(folder)
        chaps = chapters(k)
        make_epub(os.path.join(folder, "book.epub"), fault(chaps) if fault else chaps)
        with open(os.path.join(folder, "metadata.opf"), "w", encoding="utf-8") as f:
            f.write(OPF.format(title=title, author=author, sort="Austen, Jane"))
        if opts.get("cover", True):
            open(os.path.join(folder, "cover.jpg"), "wb").close()
        db.execute("insert into books values (?, ?, ?, ?)", (i, title, "Austen, Jane", rel))
        db.execute("insert into authors values (?, ?, ?)", (i, author.replace(",", "|"), "Austen, Jane"))
        db.execute("insert into books_authors_link values (?, ?, ?)", (i, i, i))
        db.execute("insert into books_languages_link values (?, ?, 1, 0)", (i, i))
    db.commit()
    db.close()


def shot(chrome, page, png, width, height, tmp):
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--force-device-scale-factor=2",
                    f"--user-data-dir={os.path.join(tmp, 'chrome')}", f"--window-size={width},{height}", f"--screenshot={png}",
                    f"file://{page}"], check=True, capture_output=True)


def main():
    tmp = tempfile.mkdtemp(prefix="demo-")
    try:
        lib = os.path.join(tmp, "Demo Library")
        library(lib)
        subprocess.run([sys.executable, "-m", "tidy_calibre_library.validate", "library", lib, "--out", os.path.join(tmp, "out"),
                        "--cache", os.path.join(tmp, "cache")], cwd=REPO, check=True)
        report = os.path.join(tmp, "out", "report.html")
        shutil.copy(report, os.path.join(HERE, "sample-report.html"))
        chrome = next((c for c in CHROME if c and os.path.exists(c)), None)
        if not chrome:
            print("Google Chrome not found: the pictures were not made.")
            return
        with open(report, encoding="utf-8") as f:
            page = f.read()
        # For the picture: light whatever this machine's theme is, the first card open, and the verdict
        # cards in view (no summary table, no sample pages).
        page = page.replace("<html lang='en'>", "<html lang='en' data-theme='light'>")
        page = page.replace('<details class="book"', '<details open class="book"', 1)
        page = page.replace("</style>", "main>table,.sample,h3:has(+.sample){display:none}</style>", 1)
        opened = os.path.join(tmp, "opened.html")
        with open(opened, "w", encoding="utf-8") as f:
            f.write(page)
        png = os.path.join(HERE, "report.png")
        shot(chrome, opened, png, 1040, 1180, tmp)
        social = os.path.join(tmp, "social.html")
        with open(social, "w", encoding="utf-8") as f:
            f.write("<!doctype html><meta charset='utf-8'><style>body{margin:0;width:1280px;height:640px;overflow:hidden;"
                    "background:#fbfaf7;color:#1d1d1b;font:600 30px/1.3 system-ui,sans-serif}h1{font-size:60px;margin:0 0 12px}"
                    "div{padding:44px 56px 28px}img{display:block;width:1168px;margin:0 56px;border:1px solid #e4e1d8;"
                    "border-radius:10px 10px 0 0}</style><div><h1>tidy-calibre-library</h1>"
                    "Find the damaged e-books in your calibre library.</div>" f"<img src='file://{png}'>")
        shot(chrome, social, os.path.join(HERE, "social-preview.png"), 1280, 640, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
