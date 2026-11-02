"""Write report.html (worst books first, with evidence), report.csv and report.json."""
import csv
import html
import json
import os
import re
import statistics
from datetime import datetime

from .checks import LIMITS

ORDER = {"REPLACE": 0, "FIX": 1, "WARN": 2, "KEEP": 3}
METRICS = ("words", "nonword_pct", "ocr_10k", "glued_10k", "split_10k", "hyphen_10k", "garbage_10k",
           "broken_pct", "fragment_pct", "giant_pct", "digit_10k", "page_photos", "pre_pct", "header_lines", "pagenum_paras", "watermarks",
           "dup_words", "chapter_gaps", "page_gaps", "page_dups", "blank_pages")
CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--card:#fff;--mark:#ffe28a;
--REPLACE:#b42318;--FIX:#b54708;--WARN:#8a6d00;--KEEP:#1a7f37;--INFO:#6b6a65}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;
--line:#2e2d2a;--card:#1f1f1d;--mark:#6b5300;--REPLACE:#ff8a7a;--FIX:#ffb366;--WARN:#e8c84a;--KEEP:#6fd08c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}a{color:inherit;text-decoration-color:var(--muted)}h1{font-size:22px;margin:0 0 4px}
.muted{color:var(--muted)}.counts{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}
.count{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 14px}
.count b{font-size:20px;display:block}details.book{background:var(--card);border:1px solid var(--line);
border-radius:8px;margin:8px 0}details.book>summary{cursor:pointer;padding:10px 14px;list-style:none;
display:grid;grid-template-columns:90px 44px 1fr;gap:10px;align-items:baseline}
.badge{font-weight:700;font-size:12px;letter-spacing:.04em}.score{color:var(--muted);font-variant-numeric:tabular-nums}
.title{font-weight:600}.top{color:var(--muted);font-size:13px;grid-column:3}
.body{padding:0 14px 14px;border-top:1px solid var(--line)}ul.issues{padding-left:18px}
ul.issues li{margin:4px 0}.ex{color:var(--muted);font-size:13px}table{border-collapse:collapse;font-size:13px;
margin:8px 0;display:block;overflow-x:auto}td,th{border:1px solid var(--line);padding:3px 8px;text-align:right}
th{text-align:left}.sample{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:6px;
padding:8px 10px;font-size:13px;max-height:260px;overflow:auto}mark{background:var(--mark);color:inherit}
.filter{margin:12px 0}.filter label{margin-right:12px}h3{font-size:14px;margin:14px 0 4px}
@media (max-width:600px){details.book>summary{grid-template-columns:76px 1fr}.score{display:none}.top,.title{grid-column:2}}
"""
SCRIPT = """
document.querySelectorAll('.filter input').forEach(cb=>cb.addEventListener('change',()=>{
 const on=[...document.querySelectorAll('.filter input:checked')].map(c=>c.value);
 document.querySelectorAll('details.book').forEach(d=>d.style.display=on.includes(d.dataset.v)?'':'none')}));
"""


def write(out, name, blist, web_url=None, llm_mode="off", model=""):
    blist = sorted(blist, key=lambda b: (ORDER[b["verdict"]], b["score"], b["title"].lower()))
    _json(out, name, blist)
    _csv(out, blist)
    _html(out, name, blist, web_url, llm_mode, model)


def _best(b):
    return b.get("best_file")


def _json(out, name, blist):
    rows = []
    for b in blist:
        rows.append({k: b.get(k) for k in ("id", "title", "authors", "verdict", "score", "best", "shown", "meta")} |
                    {"formats": [{"fmt": f["fmt"], "size": f["size"], "level": (f.get("final") or {}).get("level"),
                                  "metrics": (f.get("final") or {}).get("metrics"), "llm": f.get("llm"),
                                  "issues": (f.get("final") or {}).get("issues"),
                                  "signs": (f.get("final") or {}).get("signs"),
                                  "suspects": (f.get("final") or {}).get("suspects")} for f in b["formats"]]})
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"library": name, "made": datetime.now().isoformat(timespec="seconds"), "books": rows}, f,
                  ensure_ascii=False, indent=1)


def _csv(out, blist):
    with open(os.path.join(out, "report.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "title", "authors", "verdict", "score", "best", "formats", "llm_rating", *METRICS, "issues"])
        for b in blist:
            best = _best(b)
            m = (best or {}).get("final", {}).get("metrics", {}) if best else {}
            rating = ((best or {}).get("llm") or {}).get("rating", "")
            w.writerow([b["id"], b["title"], "; ".join(b["authors"]), b["verdict"], b["score"], b.get("best", ""),
                        " ".join(f["fmt"] for f in b["formats"]), rating, *[m.get(k, "") for k in METRICS],
                        " ".join(sorted({i["code"] for i in b["shown"]}))])


def _esc(s):
    return html.escape(str(s))


def _mark(text, suspects):
    words = {s[0] for s in suspects}
    if not words:
        return _esc(text)
    rx = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + r")\b", re.I)
    out, last = [], 0
    for m in rx.finditer(text):
        out += [_esc(text[last:m.start()]), f"<mark>{_esc(m.group(0))}</mark>"]
        last = m.end()
    return "".join(out) + _esc(text[last:])


def _issue_li(i):
    ex = i.get("evidence", {})
    bits = ex.get("examples") or ex.get("files") or ex.get("links") or []
    log = ex.get("log")
    extra = f'<div class="ex">{_esc(" · ".join(map(str, bits)))}</div>' if bits else ""
    if log:
        extra += f'<div class="sample">{_esc(log)}</div>'
    return (f'<li><span class="badge" style="color:var(--{i["level"]})">{i["level"]}</span> '
            f'{_esc(i["msg"])}{extra}</li>')


def _book(b, web_url):
    best = _best(b)
    title = _esc(b["title"])
    if web_url and isinstance(b["id"], int):
        title = f'<a href="{_esc(web_url.rstrip("/"))}/book/{b["id"]}">{title}</a>'
    fmts = " ".join(f"<b>{f['fmt'].upper()}</b>" if f is best else f["fmt"].upper() for f in b["formats"])
    # a calibre book has an id; a file checked before it goes into calibre has a file name
    where = _esc(os.path.basename(b["formats"][0]["path"])) if b.get("path") and b["formats"] else f"#{b['id']}"
    top = "; ".join(i["msg"].split(". ")[0].rstrip(".") for i in b["shown"][:2])
    parts = [f'<details class="book" data-v="{b["verdict"]}"><summary>'
             f'<span class="badge" style="color:var(--{b["verdict"]})">{b["verdict"]}</span>'
             f'<span class="score">{b["score"]}</span><span><span class="title">{title}</span> '
             f'<span class="muted">— {_esc(", ".join(b["authors"]))} · {where} · {fmts}</span></span>'
             f'<span class="top">{_esc(top)}</span></summary><div class="body">']
    if b["shown"]:
        parts.append('<ul class="issues">' + "".join(_issue_li(i) for i in b["shown"]) + "</ul>")
    if b.get("meta"):
        parts.append("<h3>Metadata</h3><ul class='issues'>" + "".join(_issue_li(i) for i in b["meta"]) + "</ul>")
    rows = [f for f in b["formats"] if f.get("final")]
    if rows:
        keys = [k for k in METRICS if any(k in f["final"]["metrics"] for f in rows)]
        parts.append("<table><tr><th>format</th>" + "".join(f"<th>{k}</th>" for k in keys) + "<th>LLM</th></tr>")
        for f in rows:
            rating = (f.get("llm") or {}).get("rating", "")
            parts.append(f"<tr><th>{f['fmt'].upper()}</th>" + "".join(
                f"<td>{_esc(f['final']['metrics'].get(k, ''))}</td>" for k in keys) + f"<td>{rating}</td></tr>")
        parts.append("</table>")
        for f in rows:
            others = [i for i in f["final"]["issues"] if f is not best]
            if others:
                parts.append(f"<h3>{f['fmt'].upper()} issues</h3><ul class='issues'>"
                             + "".join(_issue_li(i) for i in others) + "</ul>")
    if best and best.get("final"):
        fin = best["final"]
        if fin.get("suspects"):
            parts.append("<h3>Suspect words</h3><div class='ex'>" + _esc(" · ".join(
                f"{t}→{fix} ×{c}" if fix else f"{t} ×{c}" for t, fix, c, _ in fin["suspects"][:30])) + "</div>")
        review = best.get("llm") or {}
        if review.get("note") or review.get("error"):
            count = (f" · {review['errors']} errors in about {review['pages']} pages"
                     if review.get("errors") is not None and review.get("pages") else "")
            parts.append(f"<h3>LLM reader</h3><div>{_esc(review.get('rating', ''))}/5{_esc(count)} · "
                         f"{_esc(review.get('note') or review.get('error'))}</div>")
        if b.get("path"):
            parts.append(f"<div class='ex'>Folder: {_esc(b['path'])}</div>")
        for k, label in (("pages1", "Sample pages: 12% into the text"), ("pages2", "Sample pages: 37%"),
                         ("pages3", "Sample pages: 62%"), ("pages4", "Sample pages: 87%"), ("densest", "Sample pages: most suspect words")):
            if fin.get("samples", {}).get(k):
                parts.append(f"<h3>{label}</h3><div class='sample'>{_mark(fin['samples'][k], fin.get('suspects', []))}</div>")
    parts.append("</div></details>")
    return "".join(parts)


def _html(out, name, blist, web_url, llm_mode, model):
    counts = {v: sum(1 for b in blist if b["verdict"] == v) for v in ORDER}
    dist = []
    for k in ("nonword_pct", "ocr_10k", "broken_pct", "dup_words"):
        vals = sorted(_best(b)["final"]["metrics"].get(k, 0) for b in blist
                      if _best(b) and _best(b).get("final") and k in _best(b)["final"]["metrics"])
        if vals:
            p95 = vals[min(len(vals) - 1, int(0.95 * len(vals)))]
            dist.append(f"<tr><th>{k}</th><td>{statistics.median(vals)}</td><td>{p95}</td><td>{vals[-1]}</td>"
                        f"<td>{_esc(LIMITS[k])}</td></tr>")
    llm_line = f"LLM review: {_esc(llm_mode)} ({_esc(model)})" if llm_mode != "off" else "LLM review: off"
    doc = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
           f"content='width=device-width,initial-scale=1'><title>Book check: {_esc(name)}</title>"
           f"<style>{CSS}</style></head><body><main><h1>Book check: {_esc(name)}</h1>"
           f"<div class='muted'>{len(blist)} books · {datetime.now():%d %b %Y %H:%M} · {llm_line} · "
           "score = 100 − 35 per REPLACE issue, 10 per FIX, 5 per WARN</div><div class='counts'>"]
    doc += [f"<div class='count'><b style='color:var(--{v})'>{c}</b>{v}</div>" for v, c in counts.items()]
    doc.append("</div>")
    if dist:
        doc.append("<table><tr><th>metric (best format)</th><th>median</th><th>p95</th><th>max</th><th>limit</th></tr>"
                   + "".join(dist) + "</table>")
    meta = [b for b in blist if b.get("meta")]
    if meta:
        doc.append(f"<h3>Metadata: {sum(len(b['meta']) for b in meta)} finding(s) in {len(meta)} book(s)</h3>"
                   "<div class='muted'>Fix these in calibre (Edit metadata, or Manage authors). They do not "
                   "change a verdict.</div><ul class='issues'>" + "".join(
                       f"<li>#{b['id']} <span class='title'>{_esc(b['title'])}</span>: "
                       f"{_esc(' '.join(i['msg'] for i in b['meta']))}</li>"
                       for b in sorted(meta, key=lambda b: b["title"].lower())) + "</ul>")
    doc.append("<div class='filter'>" + "".join(
        f"<label><input type='checkbox' value='{v}' checked> {v}</label>" for v in ORDER) + "</div>")
    doc += [_book(b, web_url) for b in blist]
    doc.append(f"</main><script>{SCRIPT}</script></body></html>")
    with open(os.path.join(out, "report.html"), "w", encoding="utf-8") as f:
        f.write("".join(doc))
