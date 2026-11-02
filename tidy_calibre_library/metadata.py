"""Metadata rules for a calibre library: one spelling per author, calibre's sort values,
a language on every book.

The checks read calibre's metadata.db read-only, because a book's metadata.opf can be out
of date (in one library, 63 OPFs said `en` for books that had no language in the
database). Without the database they fall back to the OPF files. A book whose OPF differs
from the database gets a finding too, because the book checks read the OPF. Findings are
INFO and do not change a verdict: a metadata fault is repaired in calibre, not by another
edition. The rules:

    initials     a full stop after each initial, no space between them: George R.R. Martin
    last-first   the name is "First Last"; calibre makes the sort value "Last, First"
    spelling     one spelling per person; accents as the book prints them (China Miéville)
    sort         calibre's author sort, with surname particles kept: Le Guin, Ursula K.
"""
import collections
import os
import re
import sqlite3
import sys
import unicodedata
from urllib.parse import quote

from .books import INFO, issue

# calibre's default tweaks (author_name_*), with surname prefixes on: our rule keeps "Le Guin"
COPYWORDS = {"agency", "corporation", "company", "co.", "council", "committee", "inc.", "institute",
             "national", "society", "club", "team", "software", "games", "entertainment", "media", "studios"}
SUFFIXES = {"jr", "sr", "inc", "ph.d", "phd", "md", "m.d", "i", "ii", "iii", "iv", "junior", "senior"}
PREFIXES = {"mr", "mrs", "ms", "dr", "prof"}
PARTICLES = {"da", "de", "di", "la", "le", "van", "von"}
ISBN_CHARS = re.compile(r"[^0-9Xx]")
LANG2 = {"en": "eng", "es": "spa", "fr": "fra", "de": "deu", "ru": "rus", "it": "ita"}  # an OPF may give 2 letters


def initials(name):
    """'George R R Martin', 'George R. R. Martin' -> 'George R.R. Martin'. A bare letter at the
    end is a name (Malcolm X), and a small letter after the first name a particle (Thomas a Kempis)."""
    out, run = [], ""
    tokens = name.split()
    for i, tok in enumerate(tokens):
        named = (i == len(tokens) - 1 and len(tok) == 1 and not run) or (i and tok.isalpha() and tok.islower())
        if not named and re.fullmatch(r"[A-Za-z]\.?|(?:[A-Z]\.)+", tok):
            run += "".join(c.upper() + "." for c in tok if c.isalpha())
            continue
        if run:
            out.append(run)
            run = ""
        out.append(tok)
    if run:
        out.append(run)
    return " ".join(out)


def author_sort(name):
    """calibre's author_to_author_sort ("comma" method), with surname particles kept."""
    tokens = re.sub(r"[\(\[].*?[\)\]]", "", name).split()
    if "," in name or len(tokens) < 2 or {t.lower() for t in tokens} & COPYWORDS:
        return name
    pre = PREFIXES | {p + "." for p in PREFIXES}
    suf = SUFFIXES | {s + "." for s in SUFFIXES}
    first = next((i for i, t in enumerate(tokens) if t.lower() not in pre), None)
    if first is None:
        return name
    last = next((i for i in range(len(tokens) - 1, first - 1, -1) if tokens[i].lower() not in suf), None)
    if last is None:
        return name
    suffix = " ".join(tokens[last + 1:])
    if last > first and tokens[last - 1].lower() in PARTICLES:
        tokens[last - 1] += " " + tokens[last]
        tokens.pop(last)
        last -= 1
    out = tokens[last:last + 1] + tokens[first:last]
    if len(out) > 1:
        out[0] += ","
    return " ".join(out + ([suffix] if suffix else []))


def valid_isbn(raw):
    d = ISBN_CHARS.sub("", raw).upper()
    if len(d) == 10 and d[:9].isdigit():
        return sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d)) % 11 == 0
    if len(d) == 13 and d.isdigit():
        return sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(d)) % 10 == 0
    return False


def _last_first(name):
    """"Austen, Jane", not "Martin Luther King, Jr."."""
    return "," in name and name.partition(",")[2].strip().rstrip(".").lower() not in SUFFIXES


def fixed(name):
    """The name after the last-first and initials rules."""
    if _last_first(name):
        last, _, first = name.partition(",")
        name = f"{first.strip()} {last.strip()}"
    return initials(name)


def _key(name):
    """One key per person: accents, case and punctuation do not count. Letters of any script do."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", fixed(name)) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[\W\d_]+", " ", plain.casefold()).split())


def from_db(library):
    """{book id: authors, author_sort, language, isbn} from metadata.db, or None.

    Opened read-only with SQLite's normal locking, so a running calibre GUI is safe."""
    path = os.path.join(library, "metadata.db")
    if not os.path.exists(path):
        return None
    try:
        db = sqlite3.connect(f"file:{quote(os.path.abspath(path))}?mode=ro", uri=True, timeout=10)
        q = lambda s: db.execute(s).fetchall()
        out = {b: {"authors": [], "author_sort": s, "language": "", "isbn": [], "path": p}
               for b, s, p in q("select id, author_sort, path from books")}
        # A row with no book: calibre added the book after the first query.
        for b, name in q("select l.book, a.name from books_authors_link l join authors a on a.id = l.author order by l.id"):
            if b in out:
                out[b]["authors"].append(name.replace("|", ","))
        for b, code in q("select bl.book, l.lang_code from books_languages_link bl join languages l on l.id = bl.lang_code "
                         "order by bl.item_order"):
            if b in out:
                out[b]["language"] = ",".join(x for x in (out[b]["language"], code) if x)
        for b, val in q("select book, val from identifiers where type = 'isbn'"):
            if b in out:
                out[b]["isbn"].append(val)
        db.close()
        return out
    except sqlite3.Error as e:
        print(f"metadata.db cannot be read ({e}), so the database rules and the orphan check are off.", file=sys.stderr)
        return None


def check(blist, db=None):
    """INFO issues per book id, for the books of one library. db: from_db(), if available."""
    def rec(b):
        return db.get(b["id"], b) if db else b
    use = collections.defaultdict(collections.Counter)
    for b in blist:
        for a in rec(b)["authors"]:
            use[_key(a)][fixed(a)] += 1
    # per person: the accented spelling, else the one on the most books
    preferred = {k: max([f for f in forms if not f.isascii()] or forms, key=lambda f: (forms[f], f))
                 for k, forms in use.items()}
    out = {}
    for opf in blist:
        if opf.get("orphan"):
            continue  # not in the database: its id may belong to another book
        b = rec(opf)
        found = []
        add = lambda code, msg: found.append(issue(code, INFO, msg))
        if b is not opf:
            stale = [f for f, x, y in (("authors", opf["authors"], b["authors"]),
                                       ("language", opf.get("language", ""), b["language"].split(",")[0]))
                     if x != y and not (f == "language" and LANG2.get(x) == y)]
            if stale:
                add("meta_stale_opf", f"The book's metadata.opf differs from calibre's database ({', '.join(stale)}). "
                                      "The book checks read the OPF. Rewrite it: calibredb backup_metadata.")
        for a in b["authors"]:
            if a.strip().lower() == "unknown":
                add("meta_unknown", "“Unknown” is listed as an author. Remove it, or name the author.")
                continue
            if _last_first(a):
                last, _, first = a.partition(",")
                add("meta_last_first", f"The author “{a}” is written last name first. Write “{first.strip()} {last.strip()}”.")
            elif initials(a) != a:
                add("meta_initials", f"Write the initials as “{initials(a)}”, not “{a}”.")
            best = preferred[_key(a)]
            if fixed(a) != best:
                add("meta_spelling", f"The author “{a}” is also written “{best}” on {use[_key(a)][best]} book(s). Use one spelling.")
        if b["authors"] and b.get("author_sort") is not None:
            want = " & ".join(author_sort(a) for a in b["authors"])
            if b["author_sort"] != want:
                add("meta_author_sort", f"The author sort is “{b['author_sort']}”; the rule gives “{want}”.")
        if not b.get("language"):
            add("meta_language", "The book has no language. The word checks need one (English: eng).")
        if opf.get("has_cover") is False:
            add("meta_no_cover", "The book has no cover in calibre. Edit metadata: Download cover.")
        for i in b.get("isbn") or []:
            if not valid_isbn(i):
                add("meta_isbn", f"The ISBN “{i}” has a wrong check digit.")
        if found:
            out[opf["id"]] = found
    return out
