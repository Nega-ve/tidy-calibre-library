"""Read every book format into one shape, and find the problems that live in the file.

A Doc is a list of parts (chapter files, or PDF pages). Each part is a list of blocks
(kind, text), where kind is "h" for a heading, "c" for a <pre> block that holds code and
"p" for anything else. A line break is "\n", and it follows the HTML rules a reader app
follows: a <br> breaks a line, and so does a newline inside <pre> or inside an element
whose CSS says white-space: pre (Gutenberg sets verse so). Any other newline in the
source is a space (keeping it would make Gutenberg's editor-wrapped paragraphs look like
hard-wrapped lines). Reading also finds damage, DRM, missing chapter files,
broken TOC links, malformed XHTML, fixed layout, and for PDFs the text layer, the scan
resolution, blank pages and look-alike page images.

MOBI, AZW3, AZW, FB2 and the like go through calibre's ebook-convert into a cached
EPUB. Calibre writes that EPUB, so only its text is checked, not its structure. A DjVu
is read like a scanned PDF (djvulibre): a reader shows its pages, and its OCR text layer
serves search only (a clean DjVu scan often sits beside a garbled OCR MOBI of the same book).
LIT, CBZ, CBR, CHM and LRF are listed, not read.

Books are untrusted downloads: XML is parsed with entities and network access off.
"""
import glob
import html.entities
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from urllib.parse import unquote

from lxml import etree

REPLACE, FIX, WARN, INFO = "REPLACE", "FIX", "WARN", "INFO"
RANK = {INFO: 0, WARN: 1, FIX: 2, REPLACE: 3}

CONVERT = ("mobi", "azw3", "azw", "prc", "fb2", "kfx", "pdb", "rtf", "docx", "odt", "txt")
READABLE = ("epub", "pdf", "djvu", "djv") + CONVERT  # what this tool reads
NOT_READ = ("lit", "cbz", "cbr", "chm", "lrf")  # listed, not checked
PAGE_IMAGES = ("pdf", "djvu", "djv")  # the drawn pages are the book; the text layer is for search
FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
CONVERT_TIMEOUT = 900  # s; a 2,000-page omnibus MOBI takes a few minutes on 2 CPU cores
THUMB = 96             # px on the long edge of a PDF page thumbnail
MAX_THUMB_PAGES = 2000

BLOCK = frozenset(
    "address article aside blockquote body dd details div dl dt fieldset figcaption figure "
    "footer form h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table tbody td "
    "tfoot th thead tr ul".split())
HEADING = frozenset("h1 h2 h3 h4 h5 h6".split())
SKIP = frozenset("script style head title svg math".split())
XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
ENTITY = re.compile(rb"&([A-Za-z][A-Za-z0-9]*);")
UNDECLARED_ENTITY = {26, 27}  # libxml2: undeclared entity, error and warning
WS_PRE = re.compile(r"white-space\s*:\s*pre", re.I)
PRE_RULE = re.compile(r"([^{}]+)\{[^}]*white-space\s*:\s*pre[^}]*\}", re.I)
CODE_CHARS = set("{}[]<>=;|\\/$#")
PRE_LONG = 60          # chars: a preformatted line this long runs off a phone screen
# A bad CRC, a broken deflate stream, a cut file, a zip-encrypted file, an unknown compression method.
ZIP_FAULTS = (zipfile.BadZipFile, zlib.error, EOFError, OSError, RuntimeError, NotImplementedError)


def issue(code, level, msg, **evidence):
    out = {"code": code, "level": level, "msg": msg}
    if evidence:
        out["evidence"] = evidence
    return out


def read_json(path):
    """A cache file, or None when it is missing or unreadable: then the work runs again."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    """All or nothing: a run that stops halfway leaves no half file at the final path."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


@dataclass
class Part:
    name: str
    blocks: list
    images: list = field(default_factory=list)  # (block index it sits before, file name, width, height, KB)


@dataclass
class Doc:
    fmt: str
    path: str
    parts: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    info: dict = field(default_factory=dict)
    reflow: bool = True    # False for PDF: the page layout is the book
    converted: bool = False


def tool(name, env):
    found = os.environ.get(env) or shutil.which(name)
    mac = f"/Applications/calibre.app/Contents/MacOS/{name}"
    if not found and os.path.exists(mac):
        found = mac
    return found


# ---------------------------------------------------------------- XHTML to blocks

def local(el):
    """The tag without its namespace. An undeclared prefix stays ("o:p" from Word; etree.QName
    rejects it): an unknown inline element, as in a reader. As "p" it would split the paragraph."""
    return el.tag.rpartition("}")[2] if isinstance(el.tag, str) else None


def _numeric_entities(data):
    """&nbsp; and other HTML names are not XML; make them numeric before parsing."""
    def repl(m):
        name = m.group(1).decode()
        if name in XML_ENTITIES or name not in html.entities.name2codepoint:
            return m.group(0)
        return b"&#%d;" % html.entities.name2codepoint[name]
    return ENTITY.sub(repl, data)


def parse_xml(data, recover):
    parser = etree.XMLParser(recover=recover, resolve_entities=False, no_network=True,
                             huge_tree=True, load_dtd=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as e:
        return None, [str(e)]
    errors = [e for e in parser.error_log if e.level > etree.ErrorLevels.WARNING and e.type not in UNDECLARED_ENTITY]
    return root, errors


def clean(text):
    text = text.replace("­", "").replace("​", "").replace("﻿", "")
    lines = (" ".join(line.split()) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _keeps_lines(el, pre_classes):
    if local(el) == "pre" or WS_PRE.search(el.get("style") or ""):
        return True
    return bool(pre_classes & set((el.get("class") or "").split()))


def _txt(s, pre):
    return s if pre else re.sub(r"[ \t]*[\r\n]+[ \t]*", " ", s)


def _codey(el):
    """A <pre> that holds code or shell output, not verse or prose."""
    text = "".join(el.itertext())
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if any(re.match(r"[#$%>] \S", l) for l in lines):
        return True
    chars = [c for c in text if not c.isspace()]
    return bool(chars) and sum(c in CODE_CHARS for c in chars) >= 0.03 * len(chars)


def _image(el, mark):
    """Tell mark() about an <img>, or an SVG <image> (covers and plates are often SVG)."""
    if mark is None:
        return
    for e in ([el] if local(el) == "img" else [d for d in el.iter() if local(d) == "image"]):
        href = e.get("src") or e.get("{http://www.w3.org/1999/xlink}href") or e.get("href")
        if href:
            mark(href)


FOOTNOTE_MARK = re.compile(r"^\s*\[?(?:fn|n)?\d{1,3}\]?\s*$|^\s*[*†‡§]+\s*$", re.I)


def _footnote_mark(el):
    """A superscript that holds only a note mark ("fn35", "28", "*"): a word break, not text
    glued to the word before it ("cheerfully announcedfn36")."""
    return local(el) == "sup" and bool(FOOTNOTE_MARK.match("".join(el.itertext())))


def _inline(el, buf, pre=False, pre_classes=frozenset(), mark=None):
    if el.text:
        buf.append(_txt(el.text, pre))
    for ch in el:
        tag = local(ch)
        if tag == "br":
            buf.append("\n")
        elif tag == "sup" and _footnote_mark(ch):
            buf.append(" ")
        elif tag in ("img", "svg"):
            _image(ch, mark)
        elif tag and tag not in SKIP:
            _inline(ch, buf, pre or _keeps_lines(ch, pre_classes), pre_classes, mark)
        if ch.tail:
            buf.append(_txt(ch.tail, pre))


def _has_block(el):
    return any(local(d) in BLOCK for d in el.iterdescendants())


def _walk(el, out, kind, pre=False, pre_classes=frozenset(), mark=None):
    pre = pre or _keeps_lines(el, pre_classes)
    buf = []

    def flush():
        text = clean("".join(buf))
        if text:
            out.append((kind, text))
        buf.clear()

    if el.text:
        buf.append(_txt(el.text, pre))
    for ch in el:
        tag = local(ch)
        if tag in ("img", "svg"):
            _image(ch, mark)
        elif tag is None or tag in SKIP:
            pass
        elif tag == "br":
            buf.append("\n")
        elif tag == "sup" and _footnote_mark(ch):
            buf.append(" ")
        elif tag in BLOCK or _has_block(ch):
            flush()
            sub = ("h" if tag in HEADING or kind == "h" else
                   "c" if kind == "c" or (tag == "pre" and _codey(ch)) else "p")
            _walk(ch, out, sub, pre, pre_classes, mark)
        else:
            _inline(ch, buf, pre or _keeps_lines(ch, pre_classes), pre_classes, mark)
        if ch.tail:
            buf.append(_txt(ch.tail, pre))
    flush()


def blocks_of(root, pre_classes=frozenset(), images=None):
    """The blocks of an XHTML body. With images=[], it also collects (block index, href) for each
    picture, where the index is the block that follows it (a picture inside a paragraph counts
    as before that paragraph)."""
    body = next((e for e in root.iter() if local(e) == "body"), root)
    out = []
    mark = (lambda href: images.append((len(out), href))) if images is not None else None
    _walk(body, out, "p", False, pre_classes, mark)
    return out


MONO = re.compile(r"font-family\s*:[^;}]*(monospace|courier|consolas|menlo)", re.I)
RULE = re.compile(r"([^{}]+)\{([^}]*)\}")
ITALIC_TAGS = frozenset("i em cite var dfn".split())
BOLD_TAGS = frozenset("b strong".split())
ITALIC_CSS = re.compile(r"font-style\s*:\s*(?:italic|oblique)", re.I)
UPRIGHT_CSS = re.compile(r"font-style\s*:\s*normal", re.I)
BOLD_CSS = re.compile(r"font-weight\s*:\s*(?:bold(?:er)?|[6-9]00)\b", re.I)
LIGHT_CSS = re.compile(r"font-weight\s*:\s*(?:normal|lighter|[1-5]00)\b", re.I)


def _styled_classes(selectors):
    """The classes a rule styles. Only the last simple selector of each rule counts:
    "div.note p.caption" styles .caption, not .note."""
    names = set()
    for sel in selectors.split(","):
        last = re.split(r"[\s>+~]+", sel.strip())[-1] if sel.strip() else ""
        names |= set(re.findall(r"\.([\w-]+)", last))
    return names


def style_classes_of(css_texts):
    """CSS classes that set italic, upright, bold or normal-weight text."""
    found = {"italic": set(), "upright": set(), "bold": set(), "light": set()}
    for css in css_texts:
        for selectors, body in RULE.findall(css):
            names = _styled_classes(selectors)
            for key, rx in (("italic", ITALIC_CSS), ("upright", UPRIGHT_CSS), ("bold", BOLD_CSS), ("light", LIGHT_CSS)):
                if rx.search(body):
                    found[key] |= names
    return {k: frozenset(v) for k, v in found.items()}


def _style_stats(root, cls):
    """Words in italics, in bold and inside blockquotes, and italic runs (an italic stretch that
    starts inside upright text), in one chapter file. Thoughts, telepathy and titles are italic in
    print; a copy with none lost them. BookDesigner copies set all text bold."""
    st = {"words": 0, "italic_words": 0, "bold_words": 0, "quote_words": 0, "italic_runs": 0}
    body = next((e for e in root.iter() if local(e) == "body"), root)

    def walk(el, ital, bold, quote):
        tag = local(el)
        classes = set((el.get("class") or "").split())
        style = el.get("style") or ""
        italic_here = tag in ITALIC_TAGS or bool(classes & cls["italic"]) or bool(ITALIC_CSS.search(style))
        upright_here = bool(classes & cls["upright"]) or bool(UPRIGHT_CSS.search(style))
        it = (ital or italic_here) and not upright_here
        bo = (bold or tag in BOLD_TAGS or bool(classes & cls["bold"]) or bool(BOLD_CSS.search(style))) \
            and not (classes & cls["light"] or LIGHT_CSS.search(style))
        qu = quote or tag == "blockquote"
        if it and not ital and "".join(el.itertext()).strip():
            st["italic_runs"] += 1
        n = len((el.text or "").split())
        st["words"] += n
        st["italic_words"] += n * it
        st["bold_words"] += n * bo
        st["quote_words"] += n * qu
        for ch in el:
            if local(ch) not in SKIP and local(ch) is not None:
                walk(ch, it, bo, qu)
            k = len((ch.tail or "").split())
            st["words"] += k
            st["italic_words"] += k * it
            st["bold_words"] += k * bo
            st["quote_words"] += k * qu

    walk(body, False, False, False)
    return st


def mono_classes_of(css_texts):
    """CSS classes set in a fixed-width font."""
    found = set()
    for css in css_texts:
        for selector, body in RULE.findall(css):
            if MONO.search(body):
                found |= _styled_classes(selector)
    return frozenset(found)


def _pre_stats(root, pre_classes, mono_classes):
    """How much of a chapter sits in preformatted blocks, which never wrap: words, words in a
    fixed-width font, and the lines in reading order with how often a line stops mid-sentence
    and the next goes on in lowercase (prose wrapped at the print width, not verse). Code
    blocks are left out."""
    body = next((e for e in root.iter() if local(e) == "body"), root)
    st = {"words": len(" ".join(body.itertext()).split()), "pre_words": 0, "mono_words": 0,
          "lines": 0, "long_lines": 0, "joints": 0, "broken": 0}
    prev = None
    for el in body.iter():
        if not isinstance(el.tag, str) or not _keeps_lines(el, pre_classes):
            continue
        if any(_keeps_lines(a, pre_classes) for a in el.iterancestors()) or _codey(el):
            continue
        text = "".join(el.itertext())
        n = len(text.split())
        st["pre_words"] += n
        if local(el) == "pre" or mono_classes & set((el.get("class") or "").split()) \
                or MONO.search(el.get("style") or ""):
            st["mono_words"] += n
        for line in (l.strip() for l in text.splitlines()):
            if not line:
                continue
            st["lines"] += 1
            st["long_lines"] += len(line) >= PRE_LONG
            if prev is not None:
                st["joints"] += 1
                st["broken"] += prev[-1] not in ".!?…:;\"”’'»)]—–*" and line[:1].islower()
            prev = line
    return st


def pre_classes_of(css_texts):
    """CSS classes that keep line breaks (white-space: pre, pre-wrap, pre-line)."""
    found = set()
    for css in css_texts:
        for selector in PRE_RULE.findall(css):
            found |= _styled_classes(selector)
    return frozenset(found)


# ---------------------------------------------------------------- EPUB

def _href(base, href):
    return posixpath.normpath(posixpath.join(base, unquote(href.split("#")[0])))


def _opf_meta(opf):
    """title, authors, language from an OPF root (EPUB or calibre metadata.opf)."""
    def texts(name):
        return [e.text.strip() for e in opf.iter() if local(e) == name and e.text and e.text.strip()]
    title = texts("title")
    lang = (texts("language") or [""])[0]
    return {"title": title[0] if title else "", "authors": texts("creator"),
            "language": "" if lang.lower() == "und" else lang}  # und: undetermined (ISO 639-2)


def _damaged_members(z):
    """Files inside the zip that fail their CRC or do not inflate. (testzip stops at the first
    one, and lets a deflate fault through as zlib.error.)"""
    bad = []
    for name in z.namelist():
        try:
            with z.open(name) as f:
                while f.read(1 << 20):
                    pass
        except ZIP_FAULTS:
            bad.append(name)
    return bad


def read_epub(path, converted=False):
    doc = Doc("epub", path, converted=converted)
    add = doc.issues.append
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        add(issue("damaged", REPLACE, f"Not a readable EPUB (zip) file: {e}"))
        return doc
    damaged = _damaged_members(z)
    if damaged:
        add(issue("damaged", REPLACE, f"{len(damaged)} file(s) inside the EPUB are damaged.", files=damaged[:10]))
    listed = set(z.namelist())
    names = listed - set(damaged)  # what can be read: the rest of the book is still checked

    if not converted:
        drm = [n for n in ("META-INF/rights.xml", "META-INF/sinf.xml") if n in listed]
        if "META-INF/encryption.xml" in names:
            enc, _ = parse_xml(z.read("META-INF/encryption.xml"), recover=True)
            if enc is not None:
                for m in enc.iter():
                    if local(m) == "EncryptionMethod" and m.get("Algorithm") not in FONT_OBFUSCATION:
                        drm.append("META-INF/encryption.xml")
                        break
        if drm:
            add(issue("drm", REPLACE, "The EPUB has DRM. Readers without the shop's key cannot open it.",
                      files=sorted(set(drm))))

    try:
        container, _ = parse_xml(z.read("META-INF/container.xml"), recover=True)
        opf_path = next(e.get("full-path") for e in container.iter() if local(e) == "rootfile")
        opf, _ = parse_xml(z.read(opf_path), recover=True)
        if opf is None:
            raise ValueError("unreadable OPF")
    except (KeyError, StopIteration, ValueError, AttributeError, *ZIP_FAULTS) as e:
        add(issue("damaged", REPLACE, f"The EPUB has no readable package file (OPF): {e}"))
        return doc
    base = posixpath.dirname(opf_path)
    doc.info.update(_opf_meta(opf))

    manifest = {}
    for e in opf.iter():
        if local(e) == "item" and e.get("href"):
            manifest[e.get("id")] = (_href(base, e.get("href")), e.get("media-type", ""),
                                     (e.get("properties") or "").split())
    spine_el = next((e for e in opf.iter() if local(e) == "spine"), None)
    spine = [e.get("idref") for e in spine_el.iter() if local(e) == "itemref"] if spine_el is not None else []
    hrefs = [manifest[i][0] for i in spine if i in manifest]

    if not converted:
        missing = sorted(h for h, _, _ in manifest.values() if h not in listed)
        missing_text = [h for h in hrefs if h not in listed]
        if missing_text:
            add(issue("missing_files", REPLACE, f"{len(missing_text)} chapter file(s) listed in the EPUB are missing.",
                      files=missing_text[:10]))
        elif missing:
            add(issue("missing_files", FIX, f"{len(missing)} file(s) listed in the EPUB are missing (images or styles).",
                      files=missing[:10]))
        seen, dups = set(), []
        for h in hrefs:
            if h in seen:
                dups.append(h)
            seen.add(h)
        if dups:
            add(issue("dup_spine", FIX, "The reading order lists the same chapter file more than once, so a reader "
                                        "shows it twice. Fix: in calibre Edit book, delete the repeated <itemref> in the OPF spine.",
                      files=dups[:10]))
        layout = [e.text for e in opf.iter() if local(e) == "meta" and e.get("property") == "rendition:layout"]
        if layout and layout[0] and layout[0].strip() == "pre-paginated":
            doc.info["fixed_layout"] = True
            add(issue("fixed_layout", WARN, "Fixed-layout EPUB: the text does not reflow, and font size cannot change."))
        _check_toc(z, names, manifest, spine_el, doc)

    css = [z.read(n).decode("utf-8", "replace") for n in names if n.lower().endswith(".css")]
    pre_classes, mono_classes, book_css = pre_classes_of(css), mono_classes_of(css), style_classes_of(css)
    pre, style = {}, {}
    malformed, parsed, sizes = [], {}, {}
    for h in hrefs:  # in reading order, repeats included: a reader shows a repeated file twice
        if h not in names:
            continue
        if h not in parsed:
            data = _numeric_entities(z.read(h))
            root, errors = parse_xml(data, recover=False)
            if root is None or errors:
                malformed.append(h)
                root, _ = parse_xml(data, recover=True)
            imgs = []
            parsed[h] = (blocks_of(root, pre_classes, imgs), _image_sizes(z, names, h, imgs, sizes)) \
                if root is not None else None
            if root is not None:
                for k, v in _pre_stats(root, pre_classes, mono_classes).items():
                    pre[k] = pre.get(k, 0) + v
                inline = [e.text or "" for e in root.iter() if local(e) == "style"]  # <style> in the head
                cls = book_css if not any(inline) else {k: book_css[k] | v for k, v in style_classes_of(inline).items()}
                for k, v in _style_stats(root, cls).items():
                    style[k] = style.get(k, 0) + v
        if parsed[h] is not None:
            doc.parts.append(Part(h, *parsed[h]))
    doc.info["pre"] = pre
    doc.info["style"] = style
    if malformed and not converted:
        add(issue("malformed_xhtml", FIX,
                  f"{len(malformed)} chapter file(s) are not valid XHTML. Some readers show them blank or broken. "
                  "Fix: calibre Edit book > Tools > Check book, or convert EPUB to EPUB.", files=malformed[:10]))
    return doc


def _image_sizes(z, names, part, imgs, sizes):
    """(block index, file name, width, height, KB) for each picture; a missing or unreadable
    image has size 0 x 0. The image header is enough, so this stays fast."""
    import io
    from PIL import Image
    out = []
    for bi, href in imgs:
        path = _href(posixpath.dirname(part), href)
        if path not in sizes:
            w = h = kb = 0
            if path in names:
                data = z.read(path)
                kb = len(data) // 1024
                try:
                    with Image.open(io.BytesIO(data)) as im:
                        w, h = im.size
                except Exception:
                    pass
            sizes[path] = (w, h, kb)
        out.append((bi, posixpath.basename(path), *sizes[path]))
    return out


def _check_toc(z, names, manifest, spine_el, doc):
    nav = [h for h, _, props in manifest.values() if "nav" in props]
    ncx_id = spine_el.get("toc") if spine_el is not None else None
    ncx = [manifest[ncx_id][0]] if ncx_id in manifest else [h for h, mt, _ in manifest.values()
                                                             if mt == "application/x-dtbncx+xml"]
    src = (nav or ncx or [None])[0]
    links = []
    if src and src in names:
        root, _ = parse_xml(_numeric_entities(z.read(src)), recover=True)
        if root is not None:
            attr = "href" if src in nav else "src"
            tag = "a" if src in nav else "content"
            links = [e.get(attr) for e in root.iter() if local(e) == tag and e.get(attr)]
    links = [l for l in links if not re.match(r"[a-z]+:", l) and not l.startswith("#")]  # "#toc": this same file
    doc.info["toc_entries"] = len(links)
    if not links:
        doc.issues.append(issue("no_toc", WARN, "The EPUB has no table of contents."))
        return
    base = posixpath.dirname(src)
    broken = sorted({l for l in links if _href(base, l) not in names})
    if broken:
        doc.issues.append(issue("toc_broken", FIX, f"{len(broken)} table-of-contents link(s) point to missing files.",
                                links=broken[:10]))


# ---------------------------------------------------------------- converted formats

def start_calibre():
    """Run ebook-convert once before the workers start. Its first run makes calibre's config
    folder, and first runs that start together fail (FileExistsError: .../plugins)."""
    exe = tool("ebook-convert", "QA_EBOOK_CONVERT")
    if exe:
        try:
            subprocess.run([exe, "--version"], capture_output=True, timeout=CONVERT_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            pass  # convert() reports the fault on each file


def convert(src, cache_dir, sha):
    """EPUB made by ebook-convert, cached by the source's sha256. Returns (path, issue)."""
    out = os.path.join(cache_dir, "conv", f"{sha}.epub")
    if os.path.exists(out):
        return out, None
    exe = tool("ebook-convert", "QA_EBOOK_CONVERT")
    if not exe:
        raise SystemExit("ebook-convert not found. Install calibre, or set QA_EBOOK_CONVERT.")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # A folder of its own: two copies of one file in a library can convert at the same time.
    with tempfile.TemporaryDirectory(dir=os.path.dirname(out)) as folder:
        tmp = os.path.join(folder, "book.epub")
        # calibre gives a file that states no language its interface language or the locale's ('de'
        # on a German system), and an English TXT got language_metadata. und reads as no language.
        try:
            r = subprocess.run([exe, src, tmp, "--no-default-epub-cover"], capture_output=True,
                               text=True, errors="replace", timeout=CONVERT_TIMEOUT,
                               env={**os.environ, "CALIBRE_OVERRIDE_LANG": "und"})
        except subprocess.TimeoutExpired:
            return None, issue("convert_failed", WARN, f"calibre could not convert this file within {CONVERT_TIMEOUT} s.")
        log = (r.stdout + r.stderr)[-2000:]
        if r.returncode or not os.path.exists(tmp):
            if "DRM" in log:
                return None, issue("drm", REPLACE, "The file has DRM. Readers without the shop's key cannot open it.")
            if r.returncode < 0 or r.returncode > 128:  # a signal (out of memory, a shutdown) says nothing about the file
                return None, issue("convert_failed", WARN, "calibre was stopped before it finished. Check this file again.",
                                   log=log[-400:])
            return None, issue("convert_failed", REPLACE, "calibre cannot read this file.", log=log[-400:])
        os.replace(tmp, out)
    return out, None


def read_converted(path, fmt, cache_dir, sha):
    epub, problem = convert(path, cache_dir, sha)
    if problem:
        doc = Doc(fmt, path, converted=True)
        doc.issues.append(problem)
        return doc
    doc = read_epub(epub, converted=True)
    doc.fmt, doc.path = fmt, path
    if fmt in ("mobi", "azw", "azw3", "prc"):
        doc.info["c1"] = mobi_c1(path)
    return doc


# ---------------------------------------------------------------- MOBI text records

def _palmdoc(data):
    """PalmDOC (LZ77) decompression of one MOBI text record."""
    out, i, n = bytearray(), 0, len(data)
    while i < n:
        c = data[i]
        i += 1
        if c == 0 or 9 <= c <= 0x7F:
            out.append(c)
        elif c <= 8:
            out += data[i:i + c]
            i += c
        elif c >= 0xC0:
            out += bytes((32, c ^ 0x80))
        elif i < n:
            c = (c << 8) | data[i]
            i += 1
            dist, k = (c >> 3) & 0x07FF, (c & 7) + 3
            if 0 < dist <= len(out):
                for _ in range(k):
                    out.append(out[-dist])
    return bytes(out)


def _trailing(rec, flags):
    """Bytes of trailing data at the end of a text record (MOBI extra-data flags)."""
    num, f = 0, flags >> 1
    while f:
        if f & 1:
            size, bit, val = len(rec) - num, 0, 0
            while size > 0:
                v = rec[size - 1]
                val |= (v & 0x7F) << bit
                bit, size = bit + 7, size - 1
                if v & 0x80 or bit >= 28:
                    break
            num += val
        f >>= 1
    if flags & 1 and len(rec) > num:
        num += (rec[len(rec) - num - 1] & 3) + 1
    return num


def mobi_c1(path):
    """Control codes U+0080-U+009F in a UTF-8 MOBI's own text. A Windows-1252 dash
    or quote read as Latin-1 becomes one of them; ebook-convert deletes them without a word, and
    the words touch ("we have\x97this" -> "we havethis"). {} when the file is not a plain
    PalmDOC MOBI in UTF-8."""
    import struct
    try:
        with open(path, "rb") as f:
            data = f.read()
        nrec = struct.unpack(">H", data[76:78])[0]
        offs = [struct.unpack(">I", data[78 + 8 * k:82 + 8 * k])[0] for k in range(nrec)] + [len(data)]
        r0 = data[offs[0]:offs[1]]
        # record 0 as calibre's mobi reader reads it: PalmDOC header, then the MOBI header at 16
        comp, = struct.unpack(">H", r0[0:2])
        nt, = struct.unpack(">H", r0[8:10])
        crypt, = struct.unpack(">H", r0[12:14])
        if r0[16:20] != b"MOBI" or crypt != 0 or comp not in (1, 2):
            return {}
        hlen, _, codepage, _, version = struct.unpack(">IIIII", r0[20:40])
        if codepage != 65001:
            return {}
        flags = struct.unpack(">H", r0[0xF2:0xF4])[0] if version >= 5 and hlen >= 0xE4 else 0
        text = bytearray()
        for k in range(1, nt + 1):
            rec = data[offs[k]:offs[k + 1]]
            rec = rec[:len(rec) - _trailing(rec, flags)] if flags else rec
            text += _palmdoc(rec) if comp == 2 else rec
        s = text.decode("utf-8", "replace")
    except (struct.error, IndexError, OSError):
        return {}
    hits = [mm for mm in re.finditer("[\u0080-\u009f]", s)]
    ex = [re.sub(r"<[^>]*>", "", s[max(0, mm.start() - 30):mm.end() + 20]).replace(mm.group(0), f"<U+{ord(mm.group(0)):04X}>")
          for mm in hits[:5]]
    return {"count": len(hits), "examples": ex}


# ---------------------------------------------------------------- PDF

def _run(cmd, timeout=600):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)


def read_pdf(path):
    doc = Doc("pdf", path, reflow=False)
    add = doc.issues.append
    for name in ("pdfinfo", "pdftotext", "pdfimages", "pdftoppm"):
        if not shutil.which(name):
            raise SystemExit(f"{name} not found. Install poppler (poppler-utils).")
    r = _run(["pdfinfo", path])
    if r.returncode:
        if "password" in r.stderr.lower():
            add(issue("drm", REPLACE, "The PDF needs a password to open."))
        else:
            add(issue("damaged", REPLACE, "The PDF cannot be read.", log=r.stderr[-400:]))
        return doc
    info = dict(line.split(":", 1) for line in r.stdout.splitlines() if ":" in line)
    pages = int(info.get("Pages", "0").strip() or 0)
    size = re.findall(r"[\d.]+", info.get("Page size", ""))
    page_w_in = float(size[0]) / 72 if size else 0
    doc.info.update(pages=pages, producer=info.get("Producer", "").strip(),
                    creator=info.get("Creator", "").strip(), title=info.get("Title", "").strip())
    if pages == 0:
        add(issue("damaged", REPLACE, "The PDF has no pages."))
        return doc

    text = _run(["pdftotext", "-enc", "UTF-8", path, "-"]).stdout
    page_texts = text.split("\f")[:pages]
    page_texts += [""] * (pages - len(page_texts))
    for i, t in enumerate(page_texts):
        doc.parts.append(Part(f"page {i + 1}", [("p", " ".join(l.split())) for l in t.splitlines() if l.strip()]))
    chars = sorted(len(t.strip()) for t in page_texts)
    doc.info["chars_per_page"] = chars[len(chars) // 2]

    # A page is a scan when one image spans at least half the page width.
    scan_pages, ppis = set(), []
    for line in _run(["pdfimages", "-list", path]).stdout.splitlines()[2:]:
        f = line.split()
        if len(f) < 14 or not f[0].isdigit():
            continue
        try:
            width, xppi = int(f[3]), int(f[12])
        except ValueError:
            continue
        if xppi > 0 and page_w_in and width / xppi >= 0.5 * page_w_in:
            scan_pages.add(int(f[0]))
            ppis.append(xppi)
    doc.info["scanned_share"] = round(len(scan_pages) / pages, 2)
    scanned = doc.info["scanned_share"] >= 0.5
    doc.info["scanned"] = scanned
    if ppis:
        ppis.sort()
        doc.info["ppi"] = ppis[len(ppis) // 2]
    if scanned and doc.info["chars_per_page"] < 50:
        look = _page_look(path, pages)
        doc.info.update(look)
        if look.get("colour", 0) >= COMIC_SATURATION:
            # A digital comic, lettered in the art: nobody reflows a comic.
            add(issue("image_only", INFO, "Full-colour pages with no text layer: a comic or a picture book, "
                                          "read as pictures by design."))
        else:
            spreads = " Each PDF page holds two printed pages side by side: split them for a tablet." \
                if look.get("landscape") else ""
            add(issue("image_only", WARN, "Scanned PDF with no text layer: no search, no text resize. "
                                          "It reads as page pictures." + spreads))
    if scanned and doc.info.get("ppi", 999) < 150:
        add(issue("low_res", WARN, f"Low-resolution scan ({doc.info['ppi']} ppi). The text may look blurry."))
    # Page images are slow to render (90 s for 344 pages at 500 ppi). Only a scan with no
    # text layer needs them: on any other PDF the text finds duplicate pages.
    if scanned and doc.info["chars_per_page"] < 50 and pages <= MAX_THUMB_PAGES:
        doc.info["thumbs"] = _thumbs(path)
    return doc


def read_djvu(path):
    """A DjVu is a scan: page images, often with an OCR text layer, like a scanned PDF."""
    doc = Doc("djvu", path, reflow=False)
    if not all(shutil.which(n) for n in ("djvused", "djvutxt", "ddjvu")):
        doc.issues.append(issue("not_read", INFO, "djvulibre is not installed here, so the DjVu was not read "
                                                  "(macOS: brew install djvulibre; Debian/Ubuntu: apt install djvulibre-bin)."))
        return doc
    r = _run(["djvused", "-e", "n", path])
    if r.returncode or not r.stdout.strip().isdigit():
        doc.issues.append(issue("damaged", REPLACE, "The DjVu cannot be read.", log=r.stderr[-400:]))
        return doc
    pages = int(r.stdout.strip())
    doc.info.update(pages=pages, scanned=True, scanned_share=1.0)
    text = _run(["djvutxt", path], timeout=900).stdout
    page_texts = (text.split("\f") + [""] * pages)[:pages]
    for i, t in enumerate(page_texts):
        doc.parts.append(Part(f"page {i + 1}", [("p", " ".join(l.split())) for l in t.splitlines() if l.strip()]))
    chars = sorted(len(t.strip()) for t in page_texts)
    doc.info["chars_per_page"] = chars[len(chars) // 2] if chars else 0
    if doc.info["chars_per_page"] < 50:
        doc.issues.append(issue("image_only", WARN, "DjVu scan with no text layer: no search. It reads as page pictures."))
        if pages <= MAX_THUMB_PAGES:
            doc.info["thumbs"] = _djvu_thumbs(path)
    return doc


def _djvu_thumbs(path):
    """Per page: (mean, stddev, 32x48 grey vector), as _thumbs does for a PDF."""
    from PIL import Image, ImageSequence, ImageStat
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        tif = os.path.join(tmp, "p.tif")
        _run(["ddjvu", "-format=tiff", f"-size={THUMB}x{THUMB}", path, tif], timeout=1800)
        if os.path.exists(tif):
            with Image.open(tif) as raw:
                for frame in ImageSequence.Iterator(raw):
                    im = frame.convert("L")
                    st = ImageStat.Stat(im)
                    out.append((st.mean[0], st.stddev[0], list(im.resize((32, 48)).getdata())))
    return out


COMIC_SATURATION = 0.10  # mean HSV saturation of sample pages: a colour comic 0.21-0.22, book scans 0.00-0.01


def _page_look(path, pages, samples=6):
    """Colour and shape of a few pages of a picture-only PDF."""
    from PIL import Image
    sat, wide = [], 0
    with tempfile.TemporaryDirectory() as tmp:
        for k in range(samples):
            n = 1 + (k + 1) * pages // (samples + 1)
            _run(["pdftoppm", "-f", str(n), "-l", str(n), "-scale-to", "160", "-png", path, os.path.join(tmp, f"s{k}")],
                 timeout=120)
            for f in glob.glob(os.path.join(tmp, f"s{k}*.png")):
                with Image.open(f) as raw:
                    im = raw.convert("RGB")
                    w, h = im.size
                    wide += w > 1.2 * h
                    s_band = list(im.convert("HSV").getdata(band=1))
                    sat.append(sum(s_band) / len(s_band) / 255)
    return {"colour": round(sum(sat) / len(sat), 2) if sat else 0, "landscape": wide > samples // 2}


def _thumbs(path):
    """Per page: (mean, stddev, 32x48 grey vector) from a THUMB-px render."""
    from PIL import Image, ImageStat
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        _run(["pdftoppm", "-gray", "-scale-to", str(THUMB), "-png", path, os.path.join(tmp, "p")], timeout=1800)
        for f in sorted(glob.glob(os.path.join(tmp, "p-*.png")), key=lambda p: int(re.findall(r"(\d+)\.png$", p)[0])):
            with Image.open(f) as raw:
                im = raw.convert("L")
                st = ImageStat.Stat(im)
                vec = list(im.resize((32, 48)).getdata())
            out.append((st.mean[0], st.stddev[0], vec))
    return out


# ---------------------------------------------------------------- entry

def read(path, fmt, cache_dir, sha):
    if fmt == "epub":
        return read_epub(path)
    if fmt == "pdf":
        return read_pdf(path)
    if fmt in ("djvu", "djv"):
        return read_djvu(path)
    return read_converted(path, fmt, cache_dir, sha)
