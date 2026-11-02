"""Check a calibre library, or loose book files, for OCR damage, duplicate or missing
pages and the like, and give each book a verdict: KEEP, WARN, FIX or REPLACE.

  library LIB   every book in a calibre library. LIB is the library folder or its
                metadata.db. It reads each book folder's metadata.opf, and opens metadata.db
                read-only for the metadata rules (metadata.py), so it is safe while calibre
                runs. It never writes to the library. Metadata findings are listed apart
                and do not change a verdict.
  files F...    loose files or folders: books to check before they go into calibre (a folder
                is searched to any depth; X.epub and X.mobi side by side are one book).

A book's verdict comes from its best readable format (EPUB first). A worse extra format
adds a FIX ("delete the MOBI"); formats whose word counts differ by more than 15% add a
WARN. The LLM review (llm.py) adds a REPLACE at 1-2/5 (a WARN when only one passage is bad)
and a WARN at 3/5. At 4-5/5 it moves each text-only issue (non-words, OCR swaps, glued or
split words) one step down: REPLACE to WARN, WARN to INFO. Lost paragraphs move down at
4-5/5, and at 3/5 when the reader did not report lost paragraphs. It never touches
structure, duplicate, gap or page-photo issues.

Output: report.html, report.csv and report.json in --out (default: ./out/<name>). Check
results are cached per file (sha256 + this checker's own source), so a re-run reads only
new or changed files. A result that says more about this machine or this run than about the
file (a missing tool, a failed conversion, a checker error) is not cached: it runs again.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

from . import books, checks, llm, metadata, report, words
from .books import FIX, INFO, RANK, READABLE, REPLACE, WARN, issue

HERE = os.path.dirname(os.path.abspath(__file__))
PREF = ("epub", "azw3", "mobi", "azw", "prc", "fb2", "kfx", "pdb", "docx", "odt", "rtf", "txt", "pdf", "djvu", "djv")
POINTS = {REPLACE: 35, FIX: 10, WARN: 5, INFO: 0}
TEXT_CODES = {"nonwords", "ocr_swaps", "glued", "split_words"}
NOT_CACHED = {"not_read", "checker_error", "convert_failed"}  # about this machine or this run, not the file
VERDICT = {INFO: "KEEP", WARN: "WARN", FIX: "FIX", REPLACE: "REPLACE"}
WORD_DIFF = 0.15
SAME_TEXT = 0.6  # MinHash agreement over the first 5,000 words
WRONG_BOOK = 0.15  # the same opening and middle, a similar length, another title: a wrong file
NAME = "tidy-calibre-library"


def cpus():
    """CPUs this process may use. os.cpu_count() sees all of the host's CPUs inside a container."""
    return len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()


def default_cache():
    return os.path.join(words.CACHE_HOME, NAME)


def library_root(path):
    """The library folder for a folder or a metadata.db path; stops when there is no metadata.db."""
    root = os.path.abspath(path)
    if os.path.basename(root) == "metadata.db":
        root = os.path.dirname(root)
    if not os.path.isfile(os.path.join(root, "metadata.db")):
        raise SystemExit(f"No metadata.db in {root}: not a calibre library. Give the library folder or its "
                         f"metadata.db, or check loose books with: {NAME} files PATH...")
    return root


def version():
    h = hashlib.sha1()
    for name in ("books.py", "checks.py", "words.py"):
        with open(os.path.join(HERE, name), "rb") as f:
            h.update(f.read())
    h.update(str(len(words.words())).encode())
    return h.hexdigest()[:12]


# ---------------------------------------------------------------- finding books

def read_opf(path):
    with open(path, "rb") as f:
        root, _ = books.parse_xml(f.read(), recover=True)
    if root is None:
        return {}
    meta = books._opf_meta(root)
    meta["tags"] = [e.text.strip() for e in root.iter() if books.local(e) == "subject" and e.text]
    meta["isbn"] = [e.text.strip() for e in root.iter() if books.local(e) == "identifier" and e.text
                    and "isbn" in (" ".join(e.attrib.values())).lower()]
    # calibre writes the book's author sort as opf:file-as on each creator
    creator = next((e for e in root.iter() if books.local(e) == "creator"), None)
    meta["author_sort"] = next((v for k, v in creator.attrib.items() if k.endswith("file-as")), None) if creator is not None else None
    return meta


def _fmt(path):
    return os.path.splitext(path)[1].lower().lstrip(".")


def scan_library(root):
    out = []
    for author in sorted(os.scandir(root), key=lambda e: e.name):
        if not author.is_dir() or author.name.startswith("."):
            continue
        for bdir in sorted(os.scandir(author.path), key=lambda e: e.name):
            idm = re.search(r"\((\d+)\)$", bdir.name)
            if not bdir.is_dir() or not idm:
                continue
            opf = os.path.join(bdir.path, "metadata.opf")
            meta = read_opf(opf) if os.path.exists(opf) else {}
            files = [e for e in os.scandir(bdir.path) if e.is_file() and _fmt(e.name) in READABLE + books.NOT_READ]
            out.append({"id": int(idm.group(1)), "title": meta.get("title") or bdir.name,
                        "folder": f"{author.name}/{bdir.name}",
                        "authors": meta.get("authors", []), "language": meta.get("language", ""),
                        "tags": meta.get("tags", []), "isbn": meta.get("isbn", []),
                        "author_sort": meta.get("author_sort"),
                        "has_cover": os.path.exists(os.path.join(bdir.path, "cover.jpg")),
                        "formats": [{"fmt": _fmt(e.name), "path": e.path, "size": e.stat().st_size} for e in files]})
    return out


def scan_files(paths):
    """Loose files, and every book file under a folder. Files with the same name in the same
    folder (X.epub, X.mobi) are the formats of one book."""
    found = []
    for p in paths:
        if os.path.isdir(p):
            for d, dirs, names in os.walk(p):
                dirs[:] = sorted(x for x in dirs if not x.startswith("."))
                found += [os.path.join(d, n) for n in sorted(names)
                          if not n.startswith(".") and _fmt(n) in READABLE + books.NOT_READ]
        else:
            found.append(p)
    groups = {}
    for p in found:
        key = (os.path.dirname(os.path.abspath(p)), os.path.splitext(os.path.basename(p))[0].lower())
        groups.setdefault(key, []).append(p)
    out = []
    for i, ps in enumerate(groups.values(), 1):
        out.append({"id": i, "title": os.path.splitext(os.path.basename(ps[0]))[0], "authors": [], "language": "",
                    "tags": [], "isbn": [], "has_cover": None, "path": os.path.dirname(os.path.abspath(ps[0])),
                    "formats": [{"fmt": _fmt(p), "path": p, "size": os.path.getsize(p)} for p in ps]})
    return out


# ---------------------------------------------------------------- running the checks

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def failed(job, log):
    """The result for a file that the checker could not finish."""
    return {"metrics": {}, "issues": [issue("checker_error", WARN, "The checker failed on this file.", log=log)],
            "suspects": [], "samples": {}, "minhash": [], "info": {}, "reflow": True, "converted": False,
            "fmt": job[1], "sha256": job[2], "level": WARN}


def work(job):
    path, fmt, sha, meta, cache_dir = job
    try:
        doc = books.read(path, fmt, cache_dir, sha)
        res = checks.check(doc, meta)
        res["info"] = {k: v for k, v in doc.info.items() if k != "thumbs"}
        res["reflow"], res["converted"] = doc.reflow, doc.converted
    except Exception:
        return failed(job, traceback.format_exc()[-1500:])
    res["fmt"], res["sha256"] = fmt, sha
    res["level"] = checks.level_of(res["issues"])
    return res


def run_checks(blist, cache_dir, jobs):
    ver = version()
    os.makedirs(os.path.join(cache_dir, "results"), exist_ok=True)
    index_path = os.path.join(cache_dir, "hashes.json")
    index = books.read_json(index_path) or {}
    todo = []
    for b in blist:
        meta = {"title": b["title"], "authors": b["authors"], "language": b["language"], "tags": b["tags"]}
        mkey = hashlib.sha1(json.dumps(meta, sort_keys=True).encode()).hexdigest()[:8]
        for f in b["formats"]:
            if f["fmt"] not in READABLE:
                continue
            st = os.stat(f["path"])
            known = index.get(f["path"])
            if known and known[:2] == [st.st_size, st.st_mtime_ns]:
                f["sha256"] = known[2]
            else:
                f["sha256"] = sha256(f["path"])
                index[f["path"]] = [st.st_size, st.st_mtime_ns, f["sha256"]]
            f["cache"] = os.path.join(cache_dir, "results", f"{f['sha256']}-{ver}-{mkey}.json")
            f["result"] = books.read_json(f["cache"])
            if f["result"] is None:
                todo.append((f, (f["path"], f["fmt"], f["sha256"], meta, cache_dir), b["title"]))
    books.write_json(index_path, index)
    print(f"{sum(len(b['formats']) for b in blist)} files; {len(todo)} to check, the rest cached.", flush=True)
    done, total, width = 0, len(todo), jobs
    if any(item[1][1] in books.CONVERT for item in todo):
        books.start_calibre()

    def keep(item, res):
        nonlocal done
        f, _, title = item
        f["result"] = res
        if not any(i["code"] in NOT_CACHED for i in res["issues"]):
            books.write_json(f["cache"], res)
        done += 1
        print(f"[{done}/{total}] {VERDICT[res['level']]:7} {f['fmt']:5} {title}", flush=True)

    while todo:
        finished = set()
        try:
            with ProcessPoolExecutor(max_workers=width) as ex:
                futures = {ex.submit(work, item[1]): k for k, item in enumerate(todo)}
                for fut in as_completed(futures):
                    keep(todo[futures[fut]], fut.result())
                    finished.add(futures[fut])
        except BrokenProcessPool:  # a worker died, most often out of memory, and took the pool with it
            pass
        rest = [item for k, item in enumerate(todo) if k not in finished]
        if rest and width == 1:  # one worker takes the files in order, so it died on the first
            keep(rest[0], failed(rest[0][1], "The worker process died on this file. It may need more memory than this machine has."))
            rest, width = rest[1:], jobs
        elif rest and not finished:
            width = 1  # nothing got done: one at a time finds the file that ends the worker
        todo = rest


def same_text_groups(formats):
    """Formats whose word counts agree within 1% hold the same text and share one review.
    (A new MOBI can carry an old EPUB's OCR damage word for word; with one review per book,
    the MOBI would look clean only because the LLM never read it.)"""
    groups = []
    for f in sorted(formats, key=lambda f: PREF.index(f["fmt"]) if f["fmt"] in PREF else 99):
        w = f["result"]["metrics"].get("words", 0)
        for g in groups:
            w0 = g[0]["result"]["metrics"].get("words", 0)
            if w0 and abs(w - w0) <= 0.01 * w0:
                g.append(f)
                break
        else:
            groups.append([f])
    return groups


def run_llm(blist, mode, key, cache_dir, model):
    jobs = []
    for b in blist:
        fs = [f for f in b["formats"] if f.get("result") and f["result"].get("samples")]
        for group in same_text_groups(fs):
            if mode == "flagged" and all(f["result"]["level"] == INFO for f in group):
                continue
            jobs.append((b, group))
    if not jobs:
        return
    print(f"LLM review: {len(jobs)} text(s) in {len({id(b) for b, _ in jobs})} book(s) with {model}.", flush=True)
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(llm.review, g[0]["result"]["samples"], b["title"], key, cache_dir, model): g
                   for b, g in jobs}
        try:
            for fut in as_completed(futures):
                for f in futures[fut]:
                    f["llm"] = fut.result()
        except llm.Stop as e:
            for fut in futures:
                fut.cancel()
            print(f"LLM review stopped: {e}", file=sys.stderr)


# ---------------------------------------------------------------- verdicts

def effective(f):
    """A format's issues after the LLM review (the cached check result is not changed)."""
    res = copy.deepcopy(f["result"])
    review = f.get("llm")
    res["llm"] = review
    if review and "rating" in review:
        r, note = review["rating"], review.get("note", "")
        per = review.get("passages") or []
        if r <= 2 and len(per) >= 3 and sum(p >= 4 for p in per) >= len(per) - 1:
            res["issues"].append(issue("llm_local", WARN, f"The LLM reader found damage in one passage only "
                                                          f"({min(per)}/5); the others read cleanly. Read that "
                                                          f"passage: one damaged chapter, or a document in the "
                                                          f"story. {note}", examples=review.get("examples", [])))
        elif r <= 2:
            res["issues"].append(issue("llm_poor", REPLACE, f"The LLM reader rated the text {r}/5. {note}",
                                       examples=review.get("examples", [])))
        elif r == 3:
            res["issues"].append(issue("llm_fair", WARN, f"The LLM reader rated the text 3/5. {note}",
                                       examples=review.get("examples", [])))
        for i in res["issues"]:
            # The reader judges dialect and old spelling better than a word list. It also saw the
            # paragraph breaks, so long paragraphs that it does not call lost are the author's.
            lower = (r >= 4 and i["code"] in TEXT_CODES) or (i["code"] == "lost_paragraphs" and (
                r >= 4 or (r == 3 and "paragraphs" not in review.get("problems", []))))
            if lower and i["level"] in (REPLACE, WARN):
                i["level"] = WARN if i["level"] == REPLACE else INFO
                i["msg"] += f" The LLM reader rated the text {r}/5: check the samples."
    res["level"] = checks.level_of(res["issues"])
    return res


def judge(b):
    b["issues"] = []
    add = b["issues"].append
    for f in b["formats"]:
        if f.get("result"):
            f["final"] = effective(f)
    read = [f for f in b["formats"] if f.get("final")]
    skipped = [f for f in read if any(i["code"] == "not_read" for i in f["final"]["issues"])]
    read = [f for f in read if f not in skipped]  # a DjVu where djvulibre is missing says nothing
    if not b["formats"]:
        add(issue("no_file", REPLACE, "The book has no file."))
    elif not read and skipped:
        add(issue("not_read", WARN, "The " + ", ".join(f["fmt"].upper() for f in skipped)
                  + " was not read here: install djvulibre and check again."))
    elif not read:
        add(issue("unreadable_format", REPLACE, "Only " + ", ".join(f["fmt"].upper() for f in b["formats"])
                  + ": most e-readers cannot open this format."))
    best = None
    if read:
        best = min(read, key=lambda f: (RANK[f["final"]["level"]], PREF.index(f["fmt"]) if f["fmt"] in PREF else 99))
        b["best"], b["best_file"] = best["fmt"], best
        for f in read:
            if f is not best and f["final"]["level"] == REPLACE and best["final"]["level"] != REPLACE:
                why = next((i for i in f["final"]["issues"] if i["level"] == REPLACE), None)
                if best["fmt"] in books.PAGE_IMAGES and f["final"]["reflow"]:
                    # a MOBI made from the OCR text of the scan beside it
                    add(issue("ocr_copy", INFO, f"The {f['fmt'].upper()} is the OCR text of the {best['fmt'].upper()} "
                                                f"scan, and it is damaged. Read the {best['fmt'].upper()}: it shows the "
                                                "printed pages." + (f" The {f['fmt'].upper()}: {why['msg']}" if why else "")))
                    continue
                add(issue("bad_extra_format", FIX, f"The {f['fmt'].upper()} is bad, the {best['fmt'].upper()} is fine. "
                                                   f"Delete the {f['fmt'].upper()} format in calibre."
                                                   + (f" The {f['fmt'].upper()}: {why['msg']}" if why else ""),
                          examples=(why or {}).get("evidence", {}).get("examples", [])[:5]))
        counts = [(f["fmt"], f["final"]["metrics"].get("words", 0)) for f in read if f["final"]["metrics"].get("words")]
        if len(counts) > 1:
            lo, hi = min(counts, key=lambda c: c[1]), max(counts, key=lambda c: c[1])
            limit = WORD_DIFF if all(f["final"]["reflow"] for f in read) else 2 * WORD_DIFF
            if hi[1] and (hi[1] - lo[1]) / hi[1] > limit:
                add(issue("formats_differ", WARN, f"The formats differ in length: {hi[0].upper()} {hi[1]:,} words, "
                                                  f"{lo[0].upper()} {lo[1]:,} words. One may be incomplete, or "
                                                  "another edition."))
    b["best_sig"] = best["result"].get("minhash") if best else None
    b["best_mid"] = best["result"].get("minhash_mid") if best else None
    b["best_words"] = best["final"]["metrics"].get("words", 0) if best else 0


def verdict(b):
    best = b.get("best_file")
    shown = b["issues"] + (best["final"]["issues"] if best else [])
    b["verdict"] = VERDICT[checks.level_of(shown)]
    b["score"] = max(0, 100 - sum(POINTS[i["level"]] for i in shown))
    b["shown"] = sorted(shown, key=lambda i: -RANK[i["level"]])


def library_dups(blist):
    import numpy as np
    sig = [(b, b["best_sig"]) for b in blist if len(b.get("best_sig") or []) == 64]
    if len(sig) > 1:
        mat = np.array([s for _, s in sig], dtype=np.uint64)
        for i in range(len(sig) - 1):
            same = (mat[i + 1:] == mat[i]).mean(axis=1)
            for j in np.where(same >= SAME_TEXT)[0]:
                a, b = sig[i][0], sig[i + 1 + j][0]
                wa, wb = a.get("best_words", 0), b.get("best_words", 0)
                ma, mb = a.get("best_mid") or [], b.get("best_mid") or []
                middle = len(ma) == len(mb) == 64 and sum(x == y for x, y in zip(ma, mb)) >= SAME_TEXT * 64
                wrong = (_title_key(a["title"]) != _title_key(b["title"]) and middle and min(wa, wb) > 0
                         and abs(wa - wb) <= WRONG_BOOK * max(wa, wb))
                for x, y in ((a, b), (b, a)):
                    if wrong:
                        x["issues"].append(issue("wrong_book", WARN, f"The text is the text of #{y['id']} “{y['title']}” "
                                                                     f"({x.get('best_words', 0):,} and {y.get('best_words', 0):,} "
                                                                     "words), but the titles differ: one of the two files is "
                                                                     "the wrong book."))
                        continue
                    size = ("it contains that book (an omnibus or a collection)" if x.get("best_words", 0) > 1.3 * y.get("best_words", 0)
                            else "that book contains it" if y.get("best_words", 0) > 1.3 * x.get("best_words", 0)
                            else "a duplicate (keep one)")
                    x["issues"].append(issue("same_text", INFO, f"The same opening text as #{y['id']} “{y['title']}”: "
                                                                f"{size}. A library note, not damage."))
    keys = {}
    for b in blist:
        k = (_title_key(b["title"]), (b["authors"][0].split()[-1].lower() if b["authors"] else ""))
        keys.setdefault(k, []).append(b)
    for group in keys.values():
        for b in group if len(group) > 1 else []:
            others = ", ".join(f"#{o['id']}" for o in group if o is not b)
            b["issues"].append(issue("same_title", INFO, f"The same title and author as {others}. A library note."))


def orphans(blist, db):
    """Book folders that calibre's database does not list: the files are on disk, but calibre
    and Calibre-Web cannot show them, and their id may belong to another book."""
    known = {r["path"].casefold() for r in db.values()}  # macOS and SMB ignore case: TITLE (106) is Title (106)
    for b in blist:
        if b.get("folder") and b["folder"].casefold() not in known:
            b["orphan"] = True
            b["issues"].append(issue("orphan_folder", WARN, f"The folder “{b['folder']}” is not in calibre's database: "
                                                            "calibre does not show this book. Add it to calibre, or "
                                                            "move the folder out of the library."))


def _title_key(t):
    """The full title, subtitle included, without "The"; a volume number in brackets stays
    ("Lantern Road (II)" is not "Lantern Road (I)"), other bracketed notes go."""
    t = re.sub(r"[\(\[]\s*(?:vol(?:ume)?\.?\s*)?(\d+|[ivxl]+)\s*[\)\]]", r" \1 ", t.lower())
    t = re.sub(r"[\(\[].*?[\)\]]", "", t)
    t = re.sub(r"^(the|a|an)\s+", "", t.strip())
    return " ".join(re.sub(r"[\W_]+", " ", t).split())


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(prog=NAME, description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("library", "files"):
        s = sub.add_parser(name)
        s.add_argument("paths", nargs=1 if name == "library" else "+",
                       help="the library folder or its metadata.db" if name == "library" else "book files or folders")
        s.add_argument("--out", help="report folder (default: ./out/<name>)")
        s.add_argument("--cache", default=default_cache(), help="check results and conversions (default: %(default)s)")
        s.add_argument("--jobs", type=int, default=cpus(), help="parallel files (default: the CPUs this process may use)")
        # Off by default: the review costs money and sends book text to OpenRouter.
        s.add_argument("--llm", choices=("auto", "off", "flagged", "all"), default="off",
                       help="off (default; free), all or flagged (paid, OpenRouter), auto = all if a key is set")
        s.add_argument("--model", default=llm.MODEL)
        s.add_argument("--env-file", default=".env")
        s.add_argument("--web-url", help="Calibre-Web base URL, for links to each book")
        s.add_argument("--only", help="comma-separated calibre book ids")
    a = p.parse_args()

    if a.cmd == "library":
        root = library_root(a.paths[0])
        blist, name = scan_library(root), os.path.basename(root.rstrip("/"))
    else:
        blist = scan_files(a.paths)
        one = a.paths[0].rstrip("/")
        name = os.path.basename(os.path.abspath(one)) if len(a.paths) == 1 and os.path.isdir(one) else "files"
    if a.only:
        ids = {int(x) for x in a.only.split(",")}
        blist = [b for b in blist if b["id"] in ids]
    out = a.out or os.path.join(os.getcwd(), "out", name)
    os.makedirs(out, exist_ok=True)

    run_checks(blist, a.cache, a.jobs)
    for b in blist:
        if a.cmd == "files":  # loose files: take the names the file carries
            info = next((f["result"].get("info", {}) for f in b["formats"] if f.get("result")), {})
            b["title"] = info.get("title") or b["title"]
            b["authors"] = info.get("authors") or b["authors"]
    key = llm.key_from_env(a.env_file)
    mode = ("all" if key else "off") if a.llm == "auto" else a.llm
    if mode != "off":
        if not key:
            raise SystemExit("--llm needs OPENROUTER_API_KEY (in the environment or in --env-file).")
        run_llm(blist, mode, key, a.cache, a.model)
    for b in blist:
        judge(b)
    library_dups(blist)
    db = metadata.from_db(root) if a.cmd == "library" else None
    if db:
        orphans(blist, db)
    found = metadata.check(blist, db) if a.cmd == "library" else {}
    for b in blist:
        verdict(b)
        b["meta"] = found.get(b["id"], [])
    report.write(out, name, blist, web_url=a.web_url, llm_mode=mode, model=a.model)
    counts = {v: sum(1 for b in blist if b["verdict"] == v) for v in ("REPLACE", "FIX", "WARN", "KEEP")}
    print(" ".join(f"{k} {v}" for k, v in counts.items()), f"→ {os.path.join(out, 'report.html')}")
    if a.cmd == "library":
        print(f"Metadata: {sum(len(v) for v in found.values())} finding(s) in {len(found)} book(s)"
              + (" (see the Metadata section of the report)" if found else ""))



if __name__ == "__main__":
    main()
