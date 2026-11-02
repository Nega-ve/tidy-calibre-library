"""The checks on one format's text and pages, and the limits that turn numbers into issues.

Rates are per 10,000 words (_10k) or per cent (_pct). LIMITS hold (warn, replace) or a
single fix limit. They were set from two control groups (see README.md, "How the limits were set"):
Standard Ebooks EPUBs, which are clean, and Internet Archive scans, which are raw OCR.

  text    non-words, OCR swaps, glued and split words, hyphen leftovers, encoding garbage,
          site watermarks, and whether the text is English at all
  layout  (reflowable formats only) lines broken into paragraphs, running heads and page
          numbers left inside the text
  dups    any run of 40+ words that appears twice (a duplicated page, chapter or block)
  gaps    chapter numbers that skip or repeat; for PDFs, printed page numbers that skip
  pdf     look-alike page images on scans without text, blank pages
  end     sample-edition notices, and a last paragraph that stops mid-sentence

A scanned PDF is read as page images, so its text-layer numbers are INFO only.
"""
import posixpath
import random
import re
import statistics
import zlib
from collections import Counter, defaultdict

from . import words as W
from .books import COMIC_SATURATION, FIX, INFO, RANK, REPLACE, WARN, issue

LIMITS = {
    "nonword_pct": (1.5, 4.0),
    "ocr_10k": (5.0, 25.0),
    "glued_10k": (6.0, 30.0),
    "split_10k": (3.0, 20.0),
    "garbage_10k": (3.0, 50.0),  # fix, replace
    "broken_pct": 3.0,           # fix: page splits in one scanned series sit at 3.3-4.4%; no book read as clean is over 2.5
    "hyphen_10k": 3.0,           # fix
    "header_lines": 8,           # fix: repeats of one short line
    "pagenum_paras": 10,         # fix
    "dup_words": (150, 300),     # longest repeated run; a duplicated print page is ~300 words
    "fragment_pct": (0.5, 2.0),  # paragraphs that start or end in a word fragment
    "digit_10k": 1.0,            # warn: "1 was", "0, stop him" (and at least DIGIT_MIN of them)
    "giant_pct": (10.0, 20.0),   # share of words in 400+ word paragraphs that hold 3+ quoted turns
}
DIGIT_MIN = 5
GIANT_WORDS, GIANT_TURNS = 400, 3
# Page photos: one scanned novel had 298 large portrait pictures (one per printed page),
# 61 of them between the halves of a sentence. In 895 other files the most mid-sentence
# pictures were 25, in a finance textbook whose figures are not portrait.
PHOTO_PIXELS, PHOTO_ASPECT = 150_000, (0.55, 0.85)  # w*h, and w/h of a printed page
PHOTO_MIN, PHOTO_PER_PAGE, PHOTO_MID = 30, 0.2, 10
# Preformatted text never wraps. One novella held every print line of its prose in its own
# <pre> block, in a fixed-width font: on a tablet the lines ran off the screen and over each
# other. Verse in such blocks is normal (Gutenberg sets some poetry so); it only matters when
# the lines are long.
PRE_SHARE = 50        # % of the book's words in preformatted blocks
PRE_PROSE = 0.5       # share of line ends that stop mid-sentence: wrapped prose, not verse
PRE_LONG_SHARE = 0.2  # share of lines of PRE_LONG+ characters (books.PRE_LONG) for a WARN
MIN_CAND = 500        # lowercase words a format needs before its rates mean anything
VOCAB_REPEATS = 3     # an unexplained non-word this frequent is the book's own word
TOKEN_CAP = 3         # one OCR-swap word counts at most this often (dialect such as "clumb")
C1_MIN = 20           # control codes for dashes and quotes in a MOBI's own text (bad files: 225-460)
SHINGLE = 8
MIN_RUN = 40
IMG_DUP = 0.97        # cosine of two page thumbnails that are the same page (distinct pages: max 0.90)
PAGE_DUP = 0.9        # share of each page's 8-word runs found on the other page
COVER_PAGES = 2       # first and last pages: a back cover often repeats the front
NEAR_PAGES = 3        # a repeated page this close to its twin is a scanning fault
FRONT_PAGES = 0.08    # the first 8% of a PDF's pages: front matter
BLANK_STD = 6.0       # grey stddev under which a page thumbnail is blank
SAMPLE_WORDS = 400    # the densest cluster of suspect words, for the LLM
CONTEXT_WORDS = 150   # and the scene before it
PAGE_WINDOWS = (0.12, 0.37, 0.62, 0.87)  # where in the main text the LLM reads
WINDOW_WORDS = 750    # about three printed pages per window
WRAP = 50             # chars: a line this long that breaks mid-sentence is a wrapped print line
VERSE_LINES, VERSE_SHORT = 0.8, 0.6  # a poetry book: 99% of paragraphs under 100 chars, 85% of those under 45
GAP_SUPPORT = 5       # numbered pages each side of a page-number jump needs for a REPLACE
BACK_SHARE = 0.5      # back matter holds at most this share of a book's words
BACK_PAGE_WORDS = 5000  # a copyright or "also by" page is shorter: with this much text, the story follows it in one file
SAMPLE_WHOLE = 40000  # words: a free sample is shorter, so a notice in a longer book is about another book
GARBAGE_MIN = 5       # bad characters: in a very short file, one of them is a high rate

TOKEN = re.compile(r"[^\W_]+(?:['’][^\W\d_]+)*")  # Unicode letters: "café" is one word, not "caf"
TERMINAL = set(".!?…:;\"”’'»)]—–-*")
STOP = frozenset("the and of to a in that it was he i his you for had is with she as her not but at "
                 "on be my have him they by this which all from so were are one we me there said no "
                 "what an or been if would their when them".split())
SPLIT_PAIR = re.compile(r"\b([a-z]+) (?=([a-z]+)\b)")
HY_SPACE = re.compile(r"\b([a-z]{2,})-[ \n]+([a-z]{2,})\b")
HY_IN = re.compile(r"\b([a-z]{2,})-([a-z]{2,})\b")
MOJIBAKE = re.compile("â€|Ã[\u0080-¿]|Â[ -¿]|ï¿½")
BAD_CHAR = re.compile("[�-\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
WATERMARK = re.compile(
    r"oceanofpdf|ebook777|allitebooks|it-?ebooks\.|wowebook|freebookspot|pdfdrive|\bz-?library\b|"
    r"\blibgen\b|\bb-ok\.|bookzz|ebookee|epubbud|dbooks\.org|ebooksclub|free\s*e-?books\s*==>|"
    r"for more (?:free )?e-?books|this e-?book (?:was )?(?:brought to you|scanned|proofed|uploaded)|"
    r"universal document converter|"
    # credits of e-text makers: BookDesigner, MKM, scanners' version notes, FB2 converters
    r"created with bookdesigner|bookdesigner@|converted to (?:pdf|lit|mobi|epub|html|rtf)\s+by|->\s*[a-z]{2,}\s*<-|"
    r"scanning errors fixed|paragraph breaks restored", re.I)
SHORT_MARK = re.compile(r"\b(?:scanned|proofed|uploaded|formatted)\s+(?:and \w+ )?by\b|\bdownloaded from\b", re.I)
URL = re.compile(r"(?:https?://|www\.)([\w-]+(?:\.[\w-]+)+)", re.I)
URL_INSTITUTION = re.compile(r"\.(edu|gov|org|int|ac\.[a-z]{2})$|\.edu\.|\.gov\.")
URL_OK = ("gutenberg", "standardebooks", "archive.org", "wikipedia", "creativecommons", "w3.org")
SAMPLE = re.compile(
    r"end of (?:this |the )?(?:free )?(?:sample|preview|excerpt)|this is a (?:free )?(?:sample|preview)\b|"
    r"buy (?:the|this) (?:full|complete) (?:book|version|edition)|purchase the (?:full|complete)|"
    r"reached the end of (?:this|the) (?:free )?(?:sample|preview)", re.I)
PAGENUM = re.compile(r"^[\[(\-–— ]*(?:page\s*)?(\d{1,4})[\])\-–— ]*$", re.I)
BACK_PAGE = re.compile(r"acknowledg|about the author|also by|copyright|credits|permissions|praise for|other books|"
                       r"newsletter|fb2 ?info|generated by|created (?:using|with)", re.I)  # a page or two each
BACK_MATTER = re.compile(BACK_PAGE.pattern + r"|\bnotes\b|endnote|footnote|bibliograph|\bindex\b|glossary|appendix|"
                                             r"further reading|reading group|discussion|excerpt|preview|teaser|"
                                             r"dramatis personae|cast of characters", re.I)
FRONT_MATTER = re.compile(r"^\W*(?:table of )?contents\b|copyright|all rights reserved|\bdedicat|praise for|also by|"
                          r"by the same author|about the (?:author|book)|title ?page|\bcover\b|colophon|"
                          r"acknowledg|foreword|preface|introduction|author['’]?s note|a note (?:on|about|from)|"
                          r"translator['’]?s note|editor['’]?s note|glossary|dramatis personae|cast of characters|"
                          r"family tree|list of (?:illustrations|maps|characters)|epigraph", re.I)
FRONT_NAME = re.compile(r"(?:\b|_)toc(?:\b|_)|prelim|front.?matter|title", re.I)  # file names only
LABELS_IN = re.compile(r"\b(?:chapter|part|book)\s+[0-9ivxlcdm]+\b|\b(?:chapter|part|book)\s+[a-z]+", re.I)
CODE_LINE = re.compile(r"^\s*[#$%>] \S")
DEFINITION = re.compile(r"^[\w'’-]+(?:\s+[\w'’-]+){0,2}\s*[:=—–]\s")
GUTENBERG = re.compile(r"\*{3}\s*(START|END) OF (?:THE|THIS) PROJECT GUTENBERG", re.I)
BACKLINK = re.compile(r"[\s↩\ufe0e\ufe0f]+$|\s*\[\d+\]$")
# A publisher's "more by the author" page ends with its web or mail address, not a cut sentence.
URL_END = re.compile(r"(?:(?:https?://|www\.)\S+|\S+@[\w-]+(?:\.[\w-]+)+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|co\.uk))\W*$", re.I)
APPENDIX = re.compile(r"^\W*appendi(?:x|ces)\b", re.I)
TOC_WORDS = frozenset("chapter chapters prologue epilogue part book index maps map prelude contents interlude "
                      "appendix afterword acknowledgments acknowledgements".split())
URL_TEXT = re.compile(r"(?:https?://|www\.)\S+|\S+\.(?:php|aspx?|cgi|html?)\?\S*", re.I)
CODEISH = re.compile(r"[=_{}<>\\|]|/\w+/|\w+\.\w+\(")
INDEX_LINE = re.compile(r",\s*(?:[0-9]{1,4}|[ivxlc]{1,7})(?:\s*[-–]\s*[0-9]{1,4})?\.?$", re.I)
TRIPLE = re.compile(r"([a-z])\1\1")
COPYRIGHT = re.compile(r"copyright|all rights reserved|first published|\bisbn\b|published by", re.I)
HY_PREFIX = frozenset("pre re co non anti semi multi post sub inter over under self ex de mid pro counter neo proto "
                      "quasi ultra un bi tri mis out well ill half all".split())
OMNIBUS = re.compile(r"omnibus|collection|trilogy|complete|anthology|box(?:ed)? set", re.I)
TEASER_WORDS = 1000   # a first part this short before the story is a teaser page, and its scene comes again
QUOTED_WORDS = 200    # a repeat this short that is quoted (after a colon or a quote mark) is a quotation
QUOTE_GAP = 300       # words between two quoted runs of one quotation (a sentence left out between them)
DUP_FILE_WORDS = 300  # a whole file repeated that is shorter is an epigraph or a copyright page printed twice

# "1" or "l" for "I": only before a word that follows "I", and only where "I" can stand
# (a sentence start, a quote, a comma, "and", "that"...): "a 1 can be" in a maths book is a number.
I_NEXT = frozenset("was am had have do did don't didn't can can't could couldn't would wouldn't will won't "
                   "shall should must might think thought know knew said saw felt feel want wanted went got get "
                   "mean meant guess suppose remember told asked looked heard need love like hope wish just never "
                   "always also really still came come took made found tried began wasn't haven't hadn't "
                   "believe see".split())
DIGIT_I = re.compile(r"(?<![\w.,'’-])[1l]\s+([a-z']+)|(?<![\w.,'’-])[1l]['’](?:m|d|ll|ve)\b")
PRE_I = re.compile(r"(?:^|[.!?\"“‘(—–,;:]\s*|\b(?:and|but|so|then|that|if|when|as|because|what|where|how|why|"
                   r"now|said|says|thought|until|unless|before|after|since|though|while|or|nor|which|who|whom|"
                   r"than|yes|no|oh|well)\s+)$", re.I)
ZERO_O = re.compile(r"(?:^|(?<=[.!?;:—–] )|(?<=[\"“‘(]))0(?:[,!] (?=[A-Za-z])|\s+(?=(?:God|Lord|my|thou|ye)\b))")
QUOTE_TURN = re.compile(r"(?:^|[.!?…—–]\s+|\s)[“\"‘']\s?[A-Z]")

# Chapter headings: "Chapter XII", "BOOK the First", "Stave 3", or a bare "XII." heading.
LABEL = re.compile(r"^(chapter|chap\.|book|part|canto|letter|stave|section|act|scene)\s+(?:the\s+)?"
                   r"([0-9]{1,3}|[ivxlcdm]+|[a-z]+(?:[- ][a-z]+)?)\b", re.I)
BARE = re.compile(r"^([0-9]{1,3}|[IVXLCDM]{1,8})(?:[.:]?$|[.:]\s+\S)")
WORDNUM = re.compile(r"[A-Za-z]+(?:[- ][A-Za-z]+)?")  # a heading that is only "Thirty-Two"
ROMAN = re.compile(r"(?=[mdclxvi])m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})")
ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
ORD = ("_ first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
       "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth").split()
ORD_TENS = "_ _ twentieth thirtieth fortieth fiftieth sixtieth seventieth eightieth ninetieth".split()


# ---------------------------------------------------------------- helpers

def number(s):
    s = s.lower().strip(" .:")
    if s.isdigit():
        return int(s)
    if ROMAN.fullmatch(s):
        vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
        total = 0
        for a, b in zip(s, s[1:] + " "):
            total += -vals[a] if b != " " and vals[a] < vals[b] else vals[a]
        return total
    parts = re.split(r"[- ]", s)
    total = 0
    for p in parts:
        if p in ONES:
            total += ONES.index(p)
        elif p in TENS[2:]:
            total += 10 * TENS.index(p)
        elif p in ORD[1:]:
            total += ORD.index(p)
        elif p in ORD_TENS[2:]:
            total += 10 * ORD_TENS.index(p)
        elif total:
            return total  # "One the storm": the title follows the number
        else:
            return None
    return total or None


def ends_sentence(text):
    t = text.rstrip()
    if t.endswith("-") and not t.endswith("--"):
        return False  # "chil-" ¶ "dren": a word broken at a print line, not a dash
    return bool(t) and t[-1] in TERMINAL


def _level(value, limits, high=REPLACE, low=WARN):
    warn, bad = limits
    return high if value >= bad else low if value >= warn else None


def _cut(text, n=24):
    w = text.split()
    return " ".join(w[:n]) + (" …" if len(w) > n else "")


# ---------------------------------------------------------------- the checks

def check(doc, meta):
    """Metrics, issues, suspects, samples and a MinHash for one format."""
    issues = list(doc.issues)
    _strip_gutenberg(doc)
    blocks, at_img = [], set()  # at_img: blocks that a picture stands right before
    for pi, part in enumerate(doc.parts):
        pics = {im[0] for im in part.images}
        for bi, (kind, text) in enumerate(part.blocks):
            if kind != "c":
                if bi in pics:
                    at_img.add(len(blocks))
                blocks.append((pi, kind, text))
    lang = (meta.get("language") or doc.info.get("language") or "en").lower()
    english = lang.startswith("en")
    fixed = not doc.reflow  # a PDF shows its drawn pages; the text layer is for search only

    # Word stream: every token that is not a bare number, lowercased, with its block.
    stream, block_of, lower_mask = [], [], []
    words_in_block = [0] * len(blocks)
    for bi, (_, _, text) in enumerate(blocks):
        for t in TOKEN.findall(text):
            if t.isdigit():
                continue
            stream.append(t.lower().replace("’", "'"))
            block_of.append(bi)
            lower_mask.append(t.islower() and len(t) >= 3 and t.isascii())
            words_in_block[bi] += 1
    n = len(stream)
    m = {"words": n, "parts": len(doc.parts)}
    code = sum(1 for p in doc.parts for k, _ in p.blocks if k == "c")
    if code:
        m["code_blocks"] = code  # shell commands and listings: not prose, so not checked
    mid = int(0.45 * len(stream))
    result = {"metrics": m, "issues": issues, "suspects": [], "samples": {}, "minhash": minhash(stream),
              "minhash_mid": minhash(stream[mid:mid + 5000]) if len(stream) >= 20000 else []}
    if not n:
        if not issues:
            issues.append(issue("no_text", WARN if fixed else REPLACE, "No text found in this file."))
        if fixed:  # a scan with no text layer still has its page images
            _pdf_pages(doc, m, issues)
        return result

    st = doc.info.get("style") or {}
    if st.get("words"):  # italic runs per 10k words, and shares of words in italics, bold and blockquotes
        m["italic_10k"] = round(1e4 * st["italic_runs"] / st["words"], 1)
        m["italic_pct"] = round(100 * st["italic_words"] / st["words"], 1)
        m["bold_pct"] = round(100 * st["bold_words"] / st["words"], 1)
        m["quote_pct"] = round(100 * st["quote_words"] / st["words"], 1)
    stop = sum(1 for w in stream if w in STOP) / n
    m["english_ratio"] = round(stop, 2)
    if english and n >= 1000 and stop < 0.2:
        issues.append(issue("not_english", WARN, f"The text does not look like English (stopword share {stop:.0%}); "
                                                  f"the metadata says '{lang}'."))
        english = False
    elif not english and n >= 1000 and stop >= 0.3:
        # an English novel tagged 'rus' would otherwise get no word check
        issues.append(issue("language_metadata", WARN, f"The text is English (stopword share {stop:.0%}), but the "
                                                        f"metadata says '{lang}'. The word checks ran anyway."))
        english = True

    c1 = doc.info.get("c1") or {}
    if c1.get("count", 0) >= C1_MIN:
        issues.append(issue("mis_encoded", FIX, f"{c1['count']} dashes and quotes are stored as control codes "
                                                "(U+0080–U+009F: Windows-1252 read as Latin-1). Calibre's conversion "
                                                "deletes them, so the words touch (“we havethis”). Read the MOBI as it "
                                                "is, or get another edition: a calibre conversion loses them.", examples=c1.get("examples", [])))
    text_issues = []
    back = _back_matter(doc)
    if english and doc.reflow:
        _fragments(back, blocks, m, issues, at_img)
    if english:
        result["suspects"], positions = _words(stream, block_of, lower_mask, blocks, m, text_issues, doc)
        _garbage(blocks, n, m, text_issues)
        _digits(back, blocks, n, m, text_issues)
        if fixed:
            for i in text_issues:
                i["level"] = INFO
                i["msg"] += " (PDF text layer only: the reader sees the drawn pages)"
    else:
        positions = []
    issues += text_issues

    _watermarks(blocks, m, issues, doc.reflow, meta)
    if doc.reflow:
        _layout(back, blocks, words_in_block, m, issues, at_img)
        _lost_paragraphs(back, blocks, m, issues)
        _drop_caps(blocks, m, issues)
        _page_photos(doc, m, issues)
        _preformatted(doc, m, issues)
        _chapter_gaps(blocks, m, issues, words_in_block, [p.name for p in doc.parts], back)
        if m.get("broken_pct", 0) < LIMITS["broken_pct"]:  # with broken lines, the last "paragraph" is a fragment
            _ending(back, doc, issues, english, Counter(stream))
    else:
        _page_gaps(doc, m, issues)
        _pdf_pages(doc, m, issues)
    _sample_notice(back, blocks, n, issues)
    _drafts(doc, m, issues)
    if english:
        result["signs"] = _signs(back, blocks, n, m, meta, at_img)
        if doc.reflow:
            _sign_issues(m, result["signs"], issues)
            _style_issues(m, meta, issues)
    _dups(back, stream, block_of, blocks, doc, m, issues, meta)
    if english and doc.reflow:
        result["samples"] = _samples(blocks, words_in_block, positions, doc)
    return result


def _strip_gutenberg(doc):
    """Drop the Project Gutenberg header and licence: boilerplate, and repeated in itself."""
    flat = [(pi, bi, t) for pi, p in enumerate(doc.parts) for bi, (_, t) in enumerate(p.blocks)]
    start = next((k for k, (_, _, t) in enumerate(flat) if (g := GUTENBERG.search(t)) and g.group(1).upper() == "START"), None)
    end = next((k for k, (_, _, t) in enumerate(flat) if (g := GUTENBERG.search(t)) and g.group(1).upper() == "END"), None)
    if start is None and end is None:
        return
    keep = {(pi, bi) for k, (pi, bi, _) in enumerate(flat)
            if (start is None or k > start) and (end is None or k < end)}
    for pi, p in enumerate(doc.parts):
        kept = [bi for bi in range(len(p.blocks)) if (pi, bi) in keep]
        if p.images:  # an image sits before block bi: renumber it among the kept blocks
            p.images = [(sum(k < im[0] for k in kept),) + tuple(im[1:]) for im in p.images
                        if kept and kept[0] <= im[0] <= kept[-1] + 1]
        p.blocks = [p.blocks[bi] for bi in kept]
    doc.parts = [p for p in doc.parts if p.blocks]


def _words(stream, block_of, lower_mask, blocks, m, out, doc):
    kinds = Counter()
    per_token = defaultdict(Counter)  # kind -> token -> count
    fixes = {}
    part_cand, part_bad = Counter(), Counter()
    # A block whose words are mostly not English is a quotation in another language (Spanish verse
    # beside the English in a bilingual edition, Italian in a translation's notes): left out.
    block_cand, block_bad = Counter(), Counter()
    for w, bi, low in zip(stream, block_of, lower_mask):
        if low:
            block_cand[bi] += 1
            block_bad[bi] += W.classify(w)[0] != W.WORD
    foreign = {bi for bi, c in block_cand.items() if c >= 6 and block_bad[bi] > 0.5 * c}
    m["foreign_blocks"] = len(foreign)
    for w, bi, low in zip(stream, block_of, lower_mask):
        if not low or bi in foreign:
            continue
        kind, fix = W.classify(w)
        kinds["cand"] += 1
        part_cand[blocks[bi][0]] += 1
        if kind != W.WORD:
            per_token[kind][w] += 1
            fixes[w] = fix
    vocab = {t for t, c in per_token[W.OTHER].items() if c >= VOCAB_REPEATS}
    bad = {t for k in (W.OCR, W.GLUED, W.OTHER) for t in per_token[k] if t not in vocab}
    positions = []
    for i, (w, bi, low) in enumerate(zip(stream, block_of, lower_mask)):
        if low and w in bad and bi not in foreign:
            positions.append(i)
            part_bad[blocks[bi][0]] += 1
    n, cand = len(stream), kinds["cand"]
    ocr = sum(min(c, TOKEN_CAP) for c in per_token[W.OCR].values())
    glued = sum(per_token[W.GLUED].values())  # no cap: "ofthe" x400 is the fault itself
    other = sum(c for t, c in per_token[W.OTHER].items() if t not in vocab)

    split, split_ex = 0, Counter()
    for _, _, text in blocks:  # a plain space only: "distri- bution" is a hyphen leftover
        for a, b in SPLIT_PAIR.findall(text):
            if W.joins_to_word(a, b):
                split += 1
                split_ex[f"{a} {b}"] += 1
    hyphen, hy_ex = 0, Counter()
    if doc.reflow:
        for _, _, text in blocks:
            for rx, need_bad_part in ((HY_SPACE, False), (HY_IN, True)):
                for a, b in rx.findall(text):
                    if need_bad_part and (a == b or a in HY_PREFIX or W.is_word(f"{a}-{b}")):
                        continue  # "wah-wah", "pre-empt": spellings with a hyphen, not print line breaks
                    if W.is_word(a + b) and (not need_bad_part or not (W.is_word(a) and W.is_word(b))):
                        hyphen += 1
                        hy_ex[f"{a}-{b}"] += 1

    m.update(cand=cand, nonword_pct=round(100 * (ocr + glued + other) / max(1, cand), 2),
             ocr_10k=round(1e4 * ocr / n, 1), glued_10k=round(1e4 * glued / n, 1),
             split_10k=round(1e4 * split / n, 1), hyphen_10k=round(1e4 * hyphen / n, 1))
    worst = [(100 * part_bad[p] / part_cand[p], p) for p in part_cand if part_cand[p] >= 300]
    if worst:
        pct, p = max(worst)
        m["worst_part"] = {"name": doc.parts[p].name, "nonword_pct": round(pct, 2)}

    if cand >= MIN_CAND:
        def top(kind, k=8):
            return [f"{t}→{fixes[t]} ×{c}" if fixes.get(t) else f"{t} ×{c}"
                    for t, c in per_token[kind].most_common(k) if t not in vocab]
        lvl = _level(m["nonword_pct"], LIMITS["nonword_pct"])
        if lvl:
            out.append(issue("nonwords", lvl, f"{m['nonword_pct']:.1f}% of lowercase words are not English words "
                                              f"(limit {LIMITS['nonword_pct'][0]}%).", examples=top(W.OTHER)))
        for code, key, kind, what in (("ocr_swaps", "ocr_10k", W.OCR, "likely OCR errors"),
                                      ("glued", "glued_10k", W.GLUED, "words run together")):
            lvl = _level(m[key], LIMITS[key])
            if lvl and code == "glued" and len(per_token[kind]) < 3:
                lvl = None  # "saidin" x4 in a short story is one invented word, not words run together
            if lvl:
                out.append(issue(code, lvl, f"{sum(per_token[kind].values())} {what} "
                                            f"({m[key]:.0f} per 10,000 words).", examples=top(kind)))
        lvl = _level(m["split_10k"], LIMITS["split_10k"])
        if lvl:
            out.append(issue("split_words", lvl, f"{split} words split in two ({m['split_10k']:.0f} per 10,000 words).",
                             examples=[f"{k} ×{c}" for k, c in split_ex.most_common(8)]))
        if doc.reflow and m["hyphen_10k"] >= LIMITS["hyphen_10k"]:
            out.append(issue("hyphens", FIX, f"{hyphen} hyphens left from print line breaks. Fix: calibre Edit book "
                                       "(search and replace).",
                             examples=[f"{k} ×{c}" for k, c in hy_ex.most_common(8)]))
    suspects = [[t, fixes.get(t), c, k] for k in (W.OCR, W.GLUED, W.OTHER)
                for t, c in per_token[k].most_common(15) if t not in vocab]
    return suspects, positions


def _is_back(part):
    """A glossary, notes, copyright or "also by" page: by its file name, or by a heading of its
    own. A word inside prose does not count: "Richard acknowledged their greetings" can open
    a chapter. Nor does a folder name. A long file that opens with a copyright line or an "also
    by" list is the story: calibre can make a book of a few large files."""
    if BACK_MATTER.search(posixpath.basename(part.name)):
        return True
    heads = [t for k, t in part.blocks[:2] if len(t) <= 60 and (k == "h" or not re.search(r"[.!?…\"”’]\s*$", t))
             and BACK_MATTER.search(t)]
    if not heads:
        return False
    return sum(len(t.split()) for _, t in part.blocks) <= BACK_PAGE_WORDS or not all(BACK_PAGE.search(t) for t in heads)


def _back_matter(doc):
    """Indexes of parts that are glossaries, notes, copyright or "also by" pages. They never make
    up most of a book: index.html can hold all of it, and in "Notes on a Scandal" every file name
    says notes. So a part over BACK_SHARE is not back matter, and if the rest still come to more
    than BACK_SHARE, none of them is."""
    words = [sum(len(t.split()) for _, t in p.blocks) for p in doc.parts]
    limit = BACK_SHARE * sum(words)
    back = {pi for pi, p in enumerate(doc.parts) if _is_back(p) and words[pi] <= limit}
    return back if sum(words[pi] for pi in back) <= limit else set()


def _fragments(back, blocks, m, out, at_img=frozenset()):
    """Text lost or shuffled at breaks: "gifted ar" ¶ "antly" ¶ "tists alive"."""
    n = frag = 0
    examples = []
    prev = ""
    paras = [k for k, (_, kind, text) in enumerate(blocks) if kind == "p" and TOKEN.findall(text)]
    short = lambda k: len(blocks[k][2].split()) <= 8
    for x, k in enumerate(paras):
        pi, kind, text = blocks[k]
        toks = TOKEN.findall(text)
        n += 1
        before, prev = prev, text
        if pi in back or "://" in text or "www." in text or DEFINITION.match(text) or CODE_LINE.match(text):
            continue  # glossaries ("efukfu : worthless man"), links and shell lines are not broken text
        if CODEISH.search(text) or INDEX_LINE.search(text.rstrip()):
            continue  # configuration lines ("nfs_server_enable=YES"), index entries ("Berlin, liv")
        if short(k) and all(short(j) for j in paras[max(0, x - 1):x + 2]):
            continue  # a run of short lines: a list of names, or a stanza
        if k in at_img or (len(before.strip()) == 1 and before.strip().isalpha()):
            continue  # a drop cap: the first letter is a picture, or a paragraph of its own
        if before.rstrip().endswith("-") and toks[0].islower():
            last = TOKEN.findall(before)
            if last and W.is_word(last[-1].lower() + toks[0]):
                continue  # "chil-" ¶ "dren": a broken line (counted there), not lost text
        first, last = toks[0], toks[-1]
        bad_first = _fragment(first)
        bad_last = _fragment(last) and not ends_sentence(text) and not text.rstrip().endswith("-")
        if not (bad_first or bad_last):
            continue
        # A line in Igbo or Tamil is foreign, not broken: the rest of a fragment's block is English.
        rest = [t for t in toks[1 if bad_first else 0:len(toks) - (1 if bad_last else 0)] if t.islower() and t.isalpha()]
        if len(toks) <= 2 or sum(not W.is_word(t) for t in rest) <= 0.25 * max(1, len(rest)):
            frag += 1
            if len(examples) < 5:
                examples.append(f"¶ {_cut(text, 10)}")
    m["fragment_pct"] = round(100 * frag / max(1, n), 2)
    lvl = _level(m["fragment_pct"], LIMITS["fragment_pct"]) if frag >= 5 else None
    if lvl:
        out.append(issue("lost_text", lvl, f"{frag} paragraphs start or end in a broken word: text is lost or "
                                           "out of order at page breaks.", examples=examples))


def _fragment(t):
    """A lowercase non-word that is not text-speak ("xxxx", "hmmm") or a drawn-out word ("darrrk")."""
    return t.islower() and t.isalpha() and t.isascii() and len(set(t)) > 2 and not TRIPLE.search(t) and not W.is_word(t)


def _garbage(blocks, n, m, out):
    """Mojibake (UTF-8 read as Windows-1252: "â€™") can be decoded back, so it is a FIX.
    "�" and control characters have lost the original: WARN, or REPLACE when dense.
    A "?" inside a word whose halves make a word is a soft hyphen ("poi?sonous"),
    counted with the hyphens."""
    moji, found = Counter(), Counter()
    for _, _, text in blocks:
        for g in MOJIBAKE.findall(text):
            moji[g] += 1
        for g in BAD_CHAR.findall(text):
            found[g] += 1
        for mm in re.finditer(r"\b([a-z]+)\?([a-z]+)\b", URL_TEXT.sub(" ", text)):  # "index.php?id=" is a link
            if not W.is_word(mm.group(1) + mm.group(2)):
                found["?"] += 1
    total = sum(found.values())
    m["garbage_10k"] = round(1e4 * total / n, 1)
    m["mojibake_10k"] = round(1e4 * sum(moji.values()) / n, 1)
    lvl = _level(m["garbage_10k"], LIMITS["garbage_10k"], high=REPLACE, low=WARN)
    if lvl and total >= GARBAGE_MIN:
        out.append(issue("garbage", lvl, f"{total} garbled characters (“�”, control codes): the original letters are "
                                         "lost, so no repair can bring them back.",
                         examples=[f"{g!r} ×{c}" for g, c in found.most_common(8)]))
    if m["mojibake_10k"] >= LIMITS["garbage_10k"][0] and sum(moji.values()) >= GARBAGE_MIN:
        out.append(issue("mojibake", FIX, f"{sum(moji.values())} characters decoded in the wrong code page (“â€™” for "
                                          "“’”). Fix: calibre Edit book (search and replace), or another edition.",
                         examples=[f"{g!r} ×{c}" for g, c in moji.most_common(8)]))


def _digits(back, blocks, n, m, out):
    """Digits read as letters: "1 was" for "I was", "0, stop him" for "O, stop him". Blocks with
    other numbers in them (tables, formulas) are left out."""
    hits, examples = 0, []
    for pi, kind, text in blocks:
        if kind != "p" or pi in back:
            continue
        if sum(1 for t in TOKEN.findall(text) if any(c.isdigit() for c in t) and t not in ("1", "0")) >= 2:
            continue
        found = [mm for mm in DIGIT_I.finditer(text) if (mm.group(1) is None or mm.group(1) in I_NEXT)
                 and PRE_I.search(text[max(0, mm.start() - 12):mm.start()])] + list(ZERO_O.finditer(text))
        hits += len(found)
        for mm in found[:1]:
            if len(examples) < 6:
                examples.append(f"…{text[max(0, mm.start() - 25):mm.end() + 20]}…")
    m["digit_10k"] = round(1e4 * hits / n, 1)
    if hits >= DIGIT_MIN and m["digit_10k"] >= LIMITS["digit_10k"]:
        out.append(issue("digit_letters", WARN, f"{hits} digits read as letters (“1 was” for “I was”): "
                                                "the text is raw OCR.", examples=examples))


def _lost_paragraphs(back, blocks, m, out):
    """Paragraph breaks lost: long paragraphs that hold several speakers' dialogue (common in
    old scanned MOBIs). A line break counts as a paragraph break here."""
    giant = total = 0
    examples = []
    for pi, kind, text in blocks:
        if kind != "p" or pi in back:
            continue
        for seg in text.split("\n"):
            w = len(seg.split())
            total += w
            if w > GIANT_WORDS:
                turns = len(QUOTE_TURN.findall(seg))
                if turns >= GIANT_TURNS:
                    giant += w
                    if len(examples) < 3:
                        examples.append(f"{w} words, {turns} quotes: {_cut(seg, 16)}")
    m["giant_pct"] = round(100 * giant / max(1, total), 1)
    lvl = _level(m["giant_pct"], LIMITS["giant_pct"])
    if lvl:
        out.append(issue("lost_paragraphs", lvl, f"{m['giant_pct']:.0f}% of the text sits in paragraphs of "
                                                 f"{GIANT_WORDS}+ words that hold several speakers: paragraph breaks "
                                                 "were lost.", examples=examples))


DRAFT = re.compile(r"\bthis draft\b|\bdraft chapter\b|\bdo not (?:distribute|circulate|cite)\b|\bto be written\b|"
                   r"\bpreliminary draft\b|\bdraft version\b", re.I)
UNRESOLVED = re.compile(r"\?\?|\[\?\]")


def _drafts(doc, m, out):
    """An author's draft: LaTeX marks for a missing reference (`??`, `[?]`) and draft
    lines. A draft is not the published edition; one with a chapter "to be written" is incomplete."""
    pages_with = sum(1 for p in doc.parts if any(UNRESOLVED.search(t) for _, t in p.blocks))
    # a draft line stands alone ("10.1 To be written", "This Draft: February 2008"), not inside prose
    lines = [t for p in doc.parts for _, t in p.blocks if len(t.split()) <= 12 and DRAFT.search(t)]
    m["unresolved_pages"] = pages_with
    unwritten = [t for t in lines if re.search(r"(?:^|\d\s|[\[(])\s*to be written\W*(?:\d{1,4}\W*)?$", t.strip(), re.I)]
    lines = [t for t in lines if t in unwritten or not re.search(r"to be written", t, re.I)]  # "a story not to be written"
    share = pages_with / max(1, len(doc.parts)) if not doc.reflow else 0
    if unwritten or share >= 0.2:
        out.append(issue("draft", WARN, "An author's draft, not the published book: "
                         + (f"{len(unwritten)} part(s) are “to be written”" if unwritten else
                            f"unresolved references (??, [?]) on {pages_with} of {len(doc.parts)} pages") + ".",
                         examples=[_cut(t, 14) for t in (unwritten or lines)[:4]]))
    elif lines:
        out.append(issue("draft", INFO, "An author's draft, not the published book.", examples=[_cut(t, 14) for t in lines[:3]]))


def _drop_caps(blocks, m, out):
    """A chapter's first letter split off: a paragraph that is one capital letter
    before a paragraph that starts in lowercase ("D ¶ espite"), or a capital, a space and a word
    piece ("D rink yourself blind"). Both join back into a word, so it is a FIX."""
    found, ex = 0, []
    for i, (pi, kind, text) in enumerate(blocks):
        if kind != "p":
            continue
        first = i == 0 or blocks[i - 1][0] != pi or blocks[i - 1][1] == "h"
        if len(text) == 1 and text.isupper() and i + 1 < len(blocks) and blocks[i + 1][0] == pi:
            nxt = blocks[i + 1][2]
            head = (TOKEN.findall(nxt) or [""])[0]
            if head.islower() and W.is_word((text + head).lower()) and not W.is_word(head):
                found += 1
                ex.append(f"{text} ¶ {_cut(nxt, 6)}")
        elif first:
            mm = re.match(r"^[“\"‘']?([A-Z]) ([a-z]+)\b", text)
            if mm and W.is_word((mm.group(1) + mm.group(2)).lower()) and not W.is_word(mm.group(2)) \
                    and mm.group(1) not in "AI":
                found += 1
                ex.append(_cut(text, 6))
    m["drop_caps"] = found
    if found >= 3:
        out.append(issue("drop_caps", FIX, f"{found} chapter openings have their first letter split off "
                                           "(“D ¶ espite”, “D rink”). Fix: join them in calibre Edit book.", examples=ex[:5]))


def _page_photos(doc, m, out):
    """Photos of printed pages between the text (some scan software keeps a picture of each page
    next to its OCR text). Illustrations sit between paragraphs; page photos also fall between
    the halves of a sentence, where the print page ended."""
    photos, mid = [], 0
    for part in doc.parts:
        if sum(len(t.split()) for _, t in part.blocks) < 100:
            continue  # a cover, a plate page
        for bi, name, w, h, _ in part.images:
            if not page_shaped(w, h):
                continue
            photos.append(f"{name} {w}x{h}")
            prev = part.blocks[bi - 1][1] if 0 < bi <= len(part.blocks) else ""
            nxt = part.blocks[bi][1] if bi < len(part.blocks) else ""
            mid += bool(prev and nxt and not ends_sentence(prev) and nxt[:1].islower())
    m["page_photos"] = len(photos)
    pages = m["words"] / 250
    if len(photos) >= PHOTO_MIN and len(photos) >= PHOTO_PER_PAGE * pages and mid >= PHOTO_MID:
        out.append(issue("page_photos", REPLACE, f"{len(photos)} photos of printed pages sit inside the text, {mid} of "
                                                 "them between the halves of a sentence. A reader sees each page as a "
                                                 "picture and then as OCR text.", examples=photos[:3]))


def _preformatted(doc, m, out):
    st = doc.info.get("pre") or {}
    if not st.get("words"):
        return
    m["pre_pct"] = round(100 * st["pre_words"] / st["words"])
    if m["pre_pct"] < PRE_SHARE or st["lines"] < 20:
        return
    prose = st["broken"] / max(1, st["joints"])
    long_share = st["long_lines"] / st["lines"]
    font = "a fixed-width font" if st["mono_words"] >= 0.5 * st["pre_words"] else "lines"
    if prose >= PRE_PROSE:
        out.append(issue("preformatted", REPLACE, f"{m['pre_pct']}% of the text is preformatted prose: each print line "
                                                  f"is fixed in {font} that never wraps ({prose:.0%} of lines stop "
                                                  "mid-sentence). On a phone or tablet the lines run off the screen "
                                                  "or overlap."))
    elif long_share >= PRE_LONG_SHARE:
        out.append(issue("preformatted", WARN, f"{m['pre_pct']}% of the text is preformatted verse that never wraps, "
                                               f"and {long_share:.0%} of its lines are long. Open a long poem on the "
                                               "reader: if lines run off the screen, get another edition."))


def _watermarks(blocks, m, out, doc_reflow=True, meta=None):
    hits, domains, domain_parts = Counter(), Counter(), defaultdict(set)
    names = {w for a in (meta or {}).get("authors", []) for w in re.findall(r"[a-z]{4,}", W.ascii_fold(a).lower())}
    for k, (pi, _, text) in enumerate(blocks):
        for h in WATERMARK.findall(text):
            hits[h.lower()] += 1
        # A credit is a line of its own. In a PDF a block is one printed line, so a line inside a
        # sentence ("…prints scanned by a computer, the numbers…") is prose.
        alone = doc_reflow or ((k == 0 or ends_sentence(blocks[k - 1][2]))
                               and (k + 1 == len(blocks) or not blocks[k + 1][2][:1].islower()))
        if len(text.split()) <= 12 and alone:
            for h in SHORT_MARK.findall(text):
                hits[_cut(text, 12)] += 1
        if any(COPYRIGHT.search(blocks[j][2]) for j in range(max(0, k - 3), min(len(blocks), k + 4))):
            continue  # a publisher's address on a copyright page (an omnibus has one per book)
        for d in URL.findall(text):
            d = d.lower()
            if any(n in d.replace("-", "") for n in names):
                continue  # the author's own site, cited in the book
            if not any(ok in d for ok in URL_OK) and not URL_INSTITUTION.search(d):
                domains[d] += 1
                domain_parts[d].add(pi)
    for d, c in domains.items():
        # Spread through the book; a publisher's ad page is not. In a PDF every page is a part,
        # and a footer URL is part of the printed page, so PDFs skip this rule.
        if c >= 5 and len(domain_parts[d]) >= 3 and doc_reflow:
            hits[d] += c
    m["watermarks"] = sum(hits.values())
    if hits:
        out.append(issue("watermark", FIX, f"Download-site marks in the text ({m['watermarks']}). "
                                           "Fix: delete them in calibre Edit book.",
                         examples=[f"{h} ×{c}" for h, c in hits.most_common(6)]))


def _verse(blocks):
    """One verse line per paragraph (a poetry collection): nearly all blocks are short lines, most very
    short. Prose wrapped into one-line paragraphs has lines near the print width instead."""
    lens = [len(t) for _, k, t in blocks if k == "p"]
    lines = [n for n in lens if n < 100]
    return (len(lens) >= 200 and len(lines) >= VERSE_LINES * len(lens)
            and sum(n < 45 for n in lines) >= VERSE_SHORT * len(lines))


def _layout(back, blocks, words_in_block, m, out, at_img=frozenset()):
    joints = broken = 0
    examples = []
    verse = _verse(blocks)
    m["verse"] = verse
    for i in range(len(blocks) - 1):
        (pa, ka, a), (pb, kb, b) = blocks[i], blocks[i + 1]
        if pa in back:
            continue
        if (i + 1) in at_img:
            continue  # a display equation or a figure set between the halves of a sentence, as in print
        if pa == pb and ka == kb == "p" and len(a) >= WRAP and not verse and not DEFINITION.match(b):
            joints += 1
            if not ends_sentence(a) and b[:1].islower():
                broken += 1
                if len(examples) < 4:
                    examples.append(f"…{' '.join(a.split()[-6:])} ¶ {' '.join(b.split()[:6])}…")
        lines = a.split("\n")
        if len(lines) >= 3 and sum(len(x) >= WRAP for x in lines[:-1]) >= 0.6 * (len(lines) - 1):
            for x, y in zip(lines, lines[1:]):
                joints += 1
                if not ends_sentence(x) and y[:1].islower():
                    broken += 1
                    if len(examples) < 4:
                        examples.append(f"…{' '.join(x.split()[-6:])} ⏎ {' '.join(y.split()[:6])}…")
    m["broken_pct"] = round(100 * broken / max(1, joints), 1)
    if joints >= 50 and m["broken_pct"] >= LIMITS["broken_pct"]:
        out.append(issue("broken_lines", FIX, f"{m['broken_pct']:.0f}% of paragraph breaks fall mid-sentence: print "
                                              "lines became paragraphs. Get another edition (calibre's Unwrap lines "
                                              "heuristic misses <br> breaks and joins verse lines).", examples=examples))

    pos, at = [], 0
    for bi, wc in enumerate(words_in_block):
        pos.append(at)
        at += wc
    groups, where, nums = defaultdict(list), defaultdict(list), []
    in_part, last_part = 0, None
    for bi, (pi, kind, text) in enumerate(blocks):
        in_part = in_part + 1 if pi == last_part else 0
        last_part = pi
        if kind != "p":
            continue
        pm = PAGENUM.match(text)
        if pm:
            if in_part >= 2:  # a number that opens a chapter file is the chapter number
                nums.append((int(pm.group(1)), pos[bi]))
            continue
        if len(text) > 60 or ends_sentence(text) or CODE_LINE.match(text):
            continue
        key = _key(text)
        if len(key.split()) >= 2 and not text.rstrip().endswith(","):  # "Dear friend," opens a letter
            groups[key].append(pos[bi])
            where[key].append(bi)
    leaks = {}
    for key, ps in groups.items():
        if len(ps) >= LIMITS["header_lines"] and _regular(ps) and _in_text(blocks, where[key]) \
                and not _countdown([blocks[bi][2] for bi in where[key]]):
            leaks[key] = len(ps)
    m["header_lines"] = sum(leaks.values())
    up = sum(1 for (a, _), (b, _) in zip(nums, nums[1:]) if b > a)
    pagenum = (len(nums) >= LIMITS["pagenum_paras"] and up >= 0.6 * (len(nums) - 1)
               and _regular([p for _, p in nums], 120, 900, 0.7))
    m["pagenum_paras"] = len(nums) if pagenum else 0
    if leaks or pagenum:
        ex = [f"“{k}” ×{c}" for k, c in sorted(leaks.items(), key=lambda kv: -kv[1])[:5]]
        if pagenum:
            ex.append(f"{len(nums)} paragraphs that are only a page number")
        out.append(issue("headers", FIX, "Running heads or page numbers from the print edition sit inside the "
                                         "text. Fix: delete them in calibre Edit book (search and replace).", examples=ex))


def _countdown(texts):
    """Lines whose numbers fall through the book ("13 hours, 38 minutes"): a
    countdown under each chapter heading. Page numbers in a running head rise."""
    nums = [tuple(map(int, re.findall(r"\d+", t))) for t in texts if re.search(r"\d", t)]
    if len(nums) < 0.8 * len(texts) or len(nums) < 3:
        return False
    steps = list(zip(nums, nums[1:]))  # (13, 47) then (13, 45): one hour, fewer minutes
    return sum(b <= a for a, b in steps) >= 0.5 * len(steps) and any(b < a for a, b in steps)


def _in_text(blocks, idxs):
    """A running head falls where a print page ended: inside the running text, with prose on both
    sides. A line that opens or closes a section (a letter's sign-off, a scene's dateline, a
    countdown under a chapter heading), or that sits among other short lines (table cells, a
    chant), is the book's own text."""
    def near(bi, step):
        k = bi + step
        while 0 <= k < len(blocks) and PAGENUM.match(blocks[k][2]):
            k += step
        return blocks[k] if 0 <= k < len(blocks) and blocks[k][0] == blocks[bi][0] else None
    inside = 0
    for bi in idxs:
        a, b = near(bi, -1), near(bi, 1)
        if a and b and a[1] != "h" and len(a[2].split()) >= 8 and len(b[2].split()) >= 8:
            inside += 1
    return inside >= 0.5 * len(idxs)


def _key(text):
    return " ".join(re.sub(r"[^a-z]+", " ", text.lower()).split())


def _regular(ps, lo=100, hi=1500, cv=0.8):
    """Words between repeats are page-like: a typical gap in lo..hi, and not too uneven."""
    gaps = [b - a for a, b in zip(ps, ps[1:])]
    if len(gaps) < 3:
        return False
    mean = statistics.mean(gaps)
    return lo <= statistics.median(gaps) <= hi and statistics.pstdev(gaps) / mean < cv


def _roman(n):
    out = ""
    for v, r in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"),
                 (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= v:
            out, n = out + r, n - v
    return out


def _num_forms(n):
    """23 -> {"23", "xxiii", "twentythree", "twentythird"}: a chapter number as letters only."""
    forms = {str(n), _roman(n)}
    if n < 20:
        forms |= {ONES[n], ORD[n]}
    elif n < 100:
        t, o = divmod(n, 10)
        forms |= {TENS[t] + (ONES[o] if o else ""), TENS[t] + ORD[o] if o else ORD_TENS[t]}
    return {f for f in forms if f and f != "_"}


def _edits(a, b):
    """Levenshtein distance."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def _heading_present(blocks, i, j, missing, label):
    """A heading for a missing number somewhere between blocks i and j: a heading that runs into
    its first sentence, small capitals split into spans ("C HAPTER F OURTEEN"), a misspelt
    number word ("ELEVVEN", "TWENY-ONE"), or an OCR-garbled heading in a scan
    ("CHAPITER SIXtEEN", "CHAPTER CEN", and "CHAPITER" alone, its number lost)."""
    lab = "" if label == "bare" else label
    for k in range(i + 1, j):
        c = re.sub(r"[^a-z0-9]", "", blocks[k][2][:80].lower())
        short = len(blocks[k][2]) <= 40
        cuts = [len(lab)] if c.startswith(lab) else \
            [L for L in range(len(lab) - 1, len(lab) + 2) if lab and short and _edits(c[:L], lab) <= 1]
        for cut in cuts:
            rest = c[cut:]
            if lab and short and not rest and len(missing) == 1:
                return True
            for n in missing:
                for f in _num_forms(n):
                    if rest.startswith(f) and (lab or short):
                        return True
                    if lab and len(f) >= 5 and f.isalpha() and any(
                            _edits(rest[:L], f) <= 2 for L in range(len(f) - 2, len(f) + 3)):
                        return True
                    if lab and short and len(f) >= 3 and f.isalpha() and _edits(rest, f) <= 1:
                        return True
    return False


def _contents(blocks, names):
    """Block indexes of contents lists, which skip or join entries by design: a run of three or
    more heading-like blocks with no text between, a block that holds several chapter labels
    ("Chapter Thirty-Eight Chapter Thirty-Nine Chapter Forty"), and files named as contents or
    front matter (a preface that walks through the chapters)."""
    out, run = set(), []
    for bi, (pi, kind, text) in enumerate(blocks):
        head = " ".join(text.split())[:80]
        if names and (FRONT_NAME.search(names[pi]) or FRONT_MATTER.search(names[pi])):
            out.add(bi)
        if len(LABELS_IN.findall(text[:200])) >= 2:
            out.add(bi)
        if len(text.split()) <= 15 and (LABEL.match(head) or BARE.match(head) or (len(head) <= 25 and WORDNUM.fullmatch(head))):
            run.append(bi)
            continue
        if len(run) >= 3:
            out.update(run)
        run = []
    if len(run) >= 3:
        out.update(run)
    return out


def _chapter_gaps(blocks, m, out, words_in_block=None, names=None, back=frozenset()):
    seqs = defaultdict(list)   # label -> [(number, block index)]
    seen = defaultdict(set)    # block index -> numbers any heading form gives there
    toc = _contents(blocks, names)
    for bi, (pi, kind, text) in enumerate(blocks):
        if len(text) > 120 or bi in toc or pi in back:
            continue  # the notes are headed by chapter, and skip the chapters with no notes
        head = " ".join(text.split())[:80]  # "CHAPTER" <br> "THIRTY-ONE" is one heading
        lm = LABEL.match(head)
        if lm and (kind == "h" or len(head) <= 60):
            num = number(lm.group(2))
            if num:
                label = "chapter" if lm.group(1).lower() in ("chapter", "chap.") else lm.group(1).lower()
                seqs[label].append((num, bi))
                seen[bi].add(num)
            continue
        if kind == "h" or len(head) <= 25:
            bm = BARE.match(head)
            num = number(bm.group(1)) if bm else (number(head) if WORDNUM.fullmatch(head) else None)
            if num:
                if kind == "h" and bm:
                    seqs["bare"].append((num, bi))
                seen[bi].add(num)
    gaps = []
    for label, entries in seqs.items():
        if label in ("act", "scene") or len(entries) < 5:
            continue  # scene numbers restart per act; a short list proves nothing
        steps = list(zip(entries, entries[1:]))
        back = sum(1 for (a, _), (b, _) in steps if b <= a)
        if any(a == b for (a, _), (b, _) in steps) or back > max(2, 0.1 * len(steps)):
            continue  # repeats, or numbers that restart often: sections mixed with chapters
        found = []
        words = words_in_block or [len(t.split()) for _, _, t in blocks]
        lengths = sorted(sum(words[i:j]) for (_, i), (_, j) in steps)
        median = lengths[len(lengths) // 2] if lengths else 0
        for x, ((a, i), (b, j)) in enumerate(steps):
            missing = set(range(a + 1, b))
            if not missing or any(missing & seen[k] for k in range(i + 1, j)):
                continue
            if len(missing) > len(entries):
                continue  # 12 → 101: a hundred per part, not 88 lost chapters
            if x and steps[x - 1][0][0] >= a:
                continue  # 13, 14, 13, 16: a misnumbered heading, not a missing chapter
            if x + 1 < len(steps) and steps[x + 1][1][0] in missing:
                continue  # 8, 19, 10: the same, with the wrong number on the higher side
            if _heading_present(blocks, i, j, missing, label):
                continue  # the heading is there, in a form the sequence did not read
            files = {blocks[k][0] for k in range(i + 1, j)} - {blocks[i][0], blocks[j][0]}
            if len(files) >= len(missing):
                continue  # a chapter file of its own lies between: present, with a heading of another form
            if median and sum(words[i:j]) > 0.9 * (b - a) * median:
                m["lost_headings"] = m.get("lost_headings", 0) + 1
                continue  # the span is as long as b - a chapters: the text is there, only a heading was lost
            found.append(f"{'Heading' if label == 'bare' else label.capitalize()} {a} → {b}")
        if len(found) <= 2:
            gaps += found
    m["chapter_gaps"] = len(gaps)
    if gaps:
        out.append(issue("chapter_gap", WARN, "Chapter numbers skip, so a chapter may be missing. "
                                              "Check the table of contents.", examples=gaps[:8]))


def _toc_like(text):
    """"… Chapter 23 Chapter 24 Chapter 25 Epilogue": a contents list, not prose."""
    toks = re.findall(r"[A-Za-z]+|\d+", text)
    return bool(toks) and sum(t.lower() in TOC_WORDS or t.isdigit() or bool(ROMAN.fullmatch(t.lower()))
                              for t in toks) >= 0.5 * len(toks)


def _ending(back, doc, out, english, counts=None):
    for pi, part in reversed(list(enumerate(doc.parts))):
        wc = sum(len(t.split()) for _, t in part.blocks)
        if wc < 400 or pi in back:
            continue
        # The story ends before an appendix in the same file (a list of noble houses, say).
        cut = next((k for k, (_, t) in enumerate(part.blocks) if len(t) <= 60 and APPENDIX.match(t)), len(part.blocks))
        texts = [BACKLINK.sub("", t).rstrip() for k, t in part.blocks[:cut] if k == "p" and t.strip()]
        last_text = texts[-1] if texts else ""
        # "…to the old harbour at Kestr": the file stops inside a word that the book
        # never uses elsewhere, however short the last paragraph is. The word list is English:
        # in another language any last word looks cut ("Ende", "Конец").
        last = (TOKEN.findall(last_text) or [""])[-1]
        if english and last_text and not ends_sentence(last_text) and not URL_END.search(last_text) and last.isalpha() \
                and not re.search(r"\bthe end(?:\b|[a-z]{1,3}$)", last_text[-40:], re.I) \
                and not ROMAN.fullmatch(last.lower()) \
                and len(last) >= 2 and not W.is_word(last.lower()) and (counts or {}).get(last.lower(), 0) <= 1:
            out.append(issue("cut_off", REPLACE, "The text stops inside a word: the file is cut off.",
                             examples=[f"…{' '.join(last_text.split()[-12:])}"]))
            return
        paras = [t for t in texts if len(t.split()) >= 25 and not _toc_like(t)]
        if paras and sum(t.endswith(",") for t in paras[-20:]) >= 0.3 * len(paras[-20:]):
            return  # an appendix list ("…ARNE, TOLLIVER, rangers,"): entries end in commas
        after = texts[texts.index(paras[-1]) + 1:] if paras else []
        if any(URL_TEXT.search(t) or URL_END.search(t) for t in after):
            return  # "…can be found on" ¶ www.example-author.co.uk
        if any(len(t.split()) >= 8 and ends_sentence(t) for t in after):
            return  # "…was likely only" ¶ "Marten’s imagination, but even so…": a page split, and the story goes on
        if paras and paras[-1] and not URL_END.search(paras[-1]) and (paras[-1][-1].islower() or paras[-1][-1] == ","):
            out.append(issue("cut_off", WARN, "The last paragraph stops mid-sentence. The book may be cut off.",
                             examples=[f"…{' '.join(paras[-1].split()[-12:])}"]))
        return


def _sample_notice(back, blocks, words, out):
    """A notice near the edge of the text. In a long book or in back matter it is most often about
    another book ("End of excerpt" after a teaser), so it is a WARN. In quotes it is speech."""
    n = len(blocks)
    edge = set(range(0, max(1, n // 30))) | set(range(n - max(1, n // 7), n))
    for bi in sorted(edge):
        pi, _, text = blocks[bi]
        mm = SAMPLE.search(text) if len(text) <= 300 else None
        if mm and not _open_at_end(text[:mm.start()]):
            if words >= SAMPLE_WHOLE or pi in back:
                out.append(issue("sample", WARN, "A sample notice in a long book or in its back matter. It may be about "
                                                 "another book: read the end of this one.", examples=[_cut(text)]))
            else:
                out.append(issue("sample", REPLACE, "This looks like a free sample, not the full book.",
                                 examples=[_cut(text)]))
            return


def _quoted(text, words, prev=""):
    """The repeat starts after a colon or inside an open quotation in its block ("…which began
    this way: At the end of", "I chose you, Anna, because you were far down on the list"), or
    opens a paragraph after one that ends with a colon (an afterword that quotes a foreword)."""
    if not words:
        return False
    mm = re.search(r"\W+".join(re.escape(w) for w in words[:6]), text.lower().replace("’", "'"))
    if not mm:
        return False
    before = text[:mm.start()]
    if not before.strip() and prev.rstrip().endswith(":"):
        return True
    return bool(re.search(r"[:“\"‘]\s*$", before)) or _open_at_end(before)


def _dups(back, stream, block_of, blocks, doc, m, out, meta=None):
    seen, runs, cur = {}, [], None
    for i in range(len(stream) - SHINGLE + 1):
        h = hash(tuple(stream[i:i + SHINGLE]))
        j = seen.get(h)
        if j is None:
            seen[h] = i
        if j is None or i - j < SHINGLE:
            if cur:
                runs.append(cur)
                cur = None
            continue
        d = i - j
        if cur and cur[0] == d and cur[2] == i - 1:
            cur[2] = i
        else:
            if cur:
                runs.append(cur)
            cur = [d, i, i]
    if cur:
        runs.append(cur)
    part_words = Counter()
    for pi, _, t in blocks:
        part_words[pi] += len(t.split())
    # A teaser is the first text page: short, with no chapter heading, its scene printed again later.
    first = next((pi for pi in sorted(part_words) if part_words[pi] >= 100), None)
    teaser = first if first is not None and part_words[first] < TEASER_WORDS and doc.parts[first].blocks \
        and not LABEL.match(doc.parts[first].blocks[0][1]) else None
    omnibus = bool(OMNIBUS.search((meta or {}).get("title", "")))
    found, design, same_part, quote_end = [], Counter(), Counter(), {}
    for d, start, last in runs:
        length = last - start + SHINGLE
        if length < MIN_RUN:
            continue
        a, b = blocks[block_of[start - d]][0], blocks[block_of[start]][0]
        spanned = len(set(block_of[start:last + SHINGLE]))
        if length / spanned < 8:
            continue  # a list of short lines repeated (a contents page), not prose
        if a in back and b in back:
            continue  # two copyright or "also by" pages
        if a == teaser and b != a:
            design["a teaser page before the story"] += length
            continue
        bi = block_of[start]
        prev = blocks[bi - 1][2] if bi and blocks[bi - 1][0] == blocks[bi][0] else ""
        if length < QUOTED_WORDS and (_quoted(blocks[bi][2], stream[start:start + 6], prev)
                                      or quote_end.get(b, -QUOTE_GAP) + QUOTE_GAP >= start):
            design["a quoted passage"] += length
            quote_end[b] = last + SHINGLE  # the next paragraphs of the quotation follow within QUOTE_GAP words
            continue
        if omnibus and a != b and length < LIMITS["dup_words"][1]:
            design["a recap across the books of an omnibus"] += length
            continue
        if a == b:
            same_part[a] += 1
        found.append((length, doc.parts[a].name, doc.parts[b].name, " ".join(stream[start:start + 16]), a, b))
    # A chapter file that repeats another file whole (one chapter twice): nothing is
    # lost, and deleting the copy repairs it.
    whole = [f for f in found if doc.reflow and f[4] != f[5] and f[0] >= 0.85 * part_words[f[5]]
             and f[0] >= 0.85 * part_words[f[4]]]  # in a PDF every page is a part: the page check judges
    for f in [f for f in whole if f[0] < DUP_FILE_WORDS]:  # an epigraph printed again at the end
        design["a short page printed twice (an epigraph, a copyright page)"] += f[0]
        found.remove(f)
        whole.remove(f)
    if whole:
        out.append(issue("duplicate_file", FIX, f"{len(whole)} chapter file(s) repeat another file whole; nothing is "
                                                "lost. Fix: in calibre Edit book, delete the copy and its spine entry.",
                         examples=[f"{x[2]} = {x[1]} ({x[0]} words)" for x in whole[:5]], files=[x[2] for x in whole]))
        found = [f for f in found if f not in whole]
    # three or more copies of one short passage in one chapter: a refrain
    same_text = Counter((f[4], f[3][:60]) for f in found if f[4] == f[5])
    refrain = [f for f in found if f[4] == f[5] and same_text[(f[4], f[3][:60])] >= 2 and f[0] < QUOTED_WORDS]
    for f in refrain:
        design["a refrain inside one chapter"] += f[0]
    found = [f[:4] for f in found if f not in refrain]
    if design:
        m["dup_by_design"] = sum(design.values())
        out.append(issue("repeat_by_design", INFO, "Repeated text that looks intended: "
                         + "; ".join(f"{k} ({v} words)" for k, v in design.items()) + "."))
    found.sort(reverse=True)
    m["dup_words"] = found[0][0] if found else 0
    m["dup_total"] = sum(f[0] for f in found)
    lvl = _level(m["dup_words"], LIMITS["dup_words"])
    if lvl and not doc.reflow:
        # In a PDF the page check judges repeated pages. A reprint far away is often on purpose
        # (a front-matter tour of the book's boxes, solutions printed under two problem sets).
        near = any(_page_no(x) and _page_no(y) and abs(_page_no(x) - _page_no(y)) <= NEAR_PAGES
                   for _, x, y, _ in found)
        lvl = WARN if near else INFO
    if lvl:
        out.append(issue("duplicate_text", lvl, f"Repeated text: the longest repeated run is {m['dup_words']} words "
                                                f"({len(found)} runs, {m['dup_total']} words in all).",
                         examples=[f"{l} words: {a} = {b}: “{s} …”" for l, a, b, s in found[:5]]))


def _page_no(name):
    mm = re.match(r"page (\d+)$", name)
    return int(mm.group(1)) if mm else None


def _page_gaps(doc, m, out):
    """Printed page numbers against PDF page order: a jump forward means missing pages."""
    cands = []
    for part in doc.parts:
        lines = [t for _, t in part.blocks]
        found = set()
        for line in lines[:2] + lines[-2:]:
            if len(line) > 60:
                continue
            if re.match(r"^\s*[-–−]\s*\d", line):
                continue  # "-1", "-2": axis labels of a figure at the page top, not page numbers
            for rx in (r"^[\[(\-–—\s]*(\d{1,4})[\])\-–—.\s]*$", r"^(\d{1,4})\s+([A-Z][A-Z' .,]+)$",
                       r"^([A-Z][A-Z' .,]+)\s+(\d{1,4})$"):
                mm = re.match(rx, line)
                if mm:
                    found.add(int(next(g for g in mm.groups() if g.isdigit())))
        cands.append(found)
    numbered = sum(1 for c in cands if c)
    m["numbered_pages"] = numbered
    if numbered < 0.3 * len(cands) or numbered < 10:
        return
    # offset = printed number - page index. A new offset needs 3 of the next 6 numbered
    # pages to agree, so a misread number ("i6" for 16) never moves it.
    det = [(i, {k - i for k in ns}) for i, ns in enumerate(cands) if ns]
    cur, segs = None, []  # segs: [offset, first page, support]
    for j, (i, offs) in enumerate(det):
        if cur in offs:
            segs[-1][2] += 1
            continue
        window = det[j:j + 6]
        votes = {o: sum(o in w for _, w in window) for o in offs}
        o, v = max(votes.items(), key=lambda kv: kv[1])
        if v >= 3:
            cur = o
            segs.append([o, i, 1])
    gaps = [(b[1], b[0] - a[0], b[1] + b[0], min(a[2], b[2])) for a, b in zip(segs, segs[1:]) if b[0] > a[0]]
    scan = doc.info.get("scanned")
    if scan:  # a scanner skips blank pages (a jump of one, or the blank pages before a part or a chapter
        # opens), and OCR misreads page numbers
        gaps = [g for g in gaps if g[1] >= 2 and not any(_chapter_start(doc.parts[k])
                                                         for k in range(max(0, g[0] - 2), g[0] + 1))]
    m["page_gaps"] = len(gaps)
    if gaps:
        # A gap where a chapter starts is usually the blank pages before an odd-page start.
        real = [g for g in gaps if g[3] >= GAP_SUPPORT and not _chapter_start(doc.parts[g[0]])]
        level = REPLACE if real and not scan else WARN
        out.append(issue("page_gap", level, "Printed page numbers skip, so pages may be missing.",
                         examples=[f"{k} page(s) missing before PDF page {i + 1} (printed page {p})"
                                   for i, k, p, _ in gaps[:8]]))


def _chapter_start(part):
    return any(re.search(r"\bchapter\b|^(part|book)\b", t, re.I) for _, t in part.blocks[:3])


def _pdf_pages(doc, m, out):
    pairs = _same_text_pages(doc)
    thumbs = doc.info.pop("thumbs", None)
    if thumbs:
        m["blank_pages"] = sum(1 for _, std, _ in thumbs if std < BLANK_STD)
        idx = [i for i, (_, std, _) in enumerate(thumbs) if std >= BLANK_STD]
        if len(idx) >= 2:
            import numpy as np
            v = np.array([thumbs[i][2] for i in idx], dtype=float)
            v -= v.mean(axis=1, keepdims=True)
            v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-9
            s = np.triu(v @ v.T, 1)
            pictures = [(idx[a] + 1, idx[b] + 1) for a, b in zip(*np.where(s >= IMG_DUP))]
            if doc.info.get("colour", 0) >= COMIC_SATURATION:
                pictures = [(a, b) for a, b in pictures if b - a <= NEAR_PAGES]  # a comic reuses its art
            pairs += pictures
    last = len(doc.parts) - COVER_PAGES
    pairs = sorted({(a, b) for a, b in pairs if COVER_PAGES < a <= last and COVER_PAGES < b <= last})
    m["page_dups"] = len(pairs)
    near = [(a, b) for a, b in pairs if b - a <= NEAR_PAGES]
    far = [(a, b) for a, b in pairs if b - a > NEAR_PAGES]
    if near:  # a scanner repeats a page next to itself
        out.append(issue("duplicate_page", REPLACE, f"{len(near)} page(s) repeat the page just before them.",
                         examples=[f"page {a} = page {b}" for a, b in near[:8]]))
    # Far apart: often on purpose. A first copy in the front matter is the book's own tour of its
    # pages (a textbook shows its feature boxes in the preface).
    front = [(a, b) for a, b in far if a <= FRONT_PAGES * len(doc.parts)]
    far = [x for x in far if x not in front]
    if far:
        out.append(issue("duplicate_page", WARN, f"{len(far)} page(s) repeat a page far away. Check them.",
                         examples=[f"page {a} = page {b}" for a, b in far[:8]]))
    if front:
        out.append(issue("duplicate_page", INFO, f"{len(front)} page(s) of the front matter show a later page "
                                                 "(a preface's tour of the book).",
                         examples=[f"page {a} = page {b}" for a, b in front[:4]]))


def _same_text_pages(doc):
    """Pairs of PDF pages (1-based) whose text is the same."""
    sets = []
    for part in doc.parts:
        w = [t.lower() for _, line in part.blocks for t in TOKEN.findall(line) if not t.isdigit()]
        sets.append({hash(tuple(w[i:i + SHINGLE])) for i in range(len(w) - SHINGLE + 1)} if len(w) >= 50 else set())
    where = defaultdict(list)
    for i, s in enumerate(sets):
        for h in s:
            where[h].append(i)
    shared = Counter()
    for pages in where.values():
        if 1 < len(pages) <= 5:
            for x in range(len(pages)):
                for y in range(x + 1, len(pages)):
                    shared[pages[x], pages[y]] += 1
    # Both ways: a preface that quotes half of a later page is not a duplicate of it.
    return [(a + 1, b + 1) for (a, b), c in shared.items() if c >= PAGE_DUP * max(len(sets[a]), len(sets[b]))]


# ---------------------------------------------------------------- signs of a damaged source
# Small signs that a copy was scanned, stripped or converted badly, found by reading about 650
# books by hand. Each is a count per 10,000 words with up to five examples. SIGN_LIMITS turns
# them into issues.

SPEECH_VERB = (r"(?:said|asked|replied|cried|shouted|whispered|muttered|called|added|snapped|answered|demanded|"
               r"began|continued|yelled|screamed|growled|murmured|laughed|sighed|agreed|insisted|exclaimed)")
SUBJECT = r"(?:he|she|they|we|I|[A-Z][a-z]+)"
CLOSE_TAG = re.compile(r"[,!?…—–-]\s?[’'”\"]\s+(?:" + SPEECH_VERB + r"|" + SUBJECT + r"\s+" + SPEECH_VERB + r")\b")
OPEN_QUOTE = re.compile(r"(?:^|[\s(\[—–])[‘“\"'`](?=[\w.…—–])")
TAG_SUBJECT = re.compile(r"[,!?…—–-]\s?[’'”\"]\s+(?:" + SPEECH_VERB + r"\s+(" + SUBJECT + r")|(" + SUBJECT + r")\s+"
                         + SPEECH_VERB + r")\b")
BARE_TAG = re.compile(r"[,?!] (?:" + SUBJECT + r" " + SPEECH_VERB + r"|" + SPEECH_VERB + r" " + SUBJECT + r")\b")
QUOTE_MARK = re.compile(r"[“”\"‘`]|['’](?=[\s,.;:!?—–)]|$)|(?:^|(?<=[\s(—–]))['’](?=\w)")
CONTRACTION_LOST = re.compile(r"\b(?:dont|didnt|doesnt|isnt|wasnt|arent|werent|couldnt|wouldnt|shouldnt|hasnt|"
                              r"havent|hadnt|Im|Ive|youre|youve|theyre|theyve|weve|thats|whats|theres|hes|shes)\b"
                              r"|\b[A-Z][a-z]+ s (?=[a-z]{2})|\b(?:they|you|we|he|she) (?:d|ll|re|ve)\b")
DASH_WORD = frozenset("and but or nor not then than the that which who what when where it he she they we i his her "
                      "my if so with from no for".split())
NOT_ABBREV = frozenset("etc vs viz cf al approx ca esp incl mr mrs ms dr st no nos vol vols ch pp fig figs ed eds trans co "
                       "inc ltd jr sr lt col gen capt sgt rev prof ft mt op cit ibid seq ff ad bc am pm".split())
SIGN_RX = {
    "hyphen_dash": re.compile(r"\b([A-Za-z]{2,})-([a-z]+)\b(?!-)"),       # "save him-the boat"     
    "space_hyphen": re.compile(r"\b([a-z]{2,})- ([a-z]{2,})\b"),          # "A man- one man"
    "nospace_end": re.compile(r"\b[a-z]{2,}[.!?][\"”’]?[A-Z][a-z]"),        # "said.The"
    # pronouns are left out: He, His, Her are capitalised for God in some books
    "lost_stop": re.compile(r"\b[a-z]{3,} (?:The|This|That|These|Those|There|Then|But|So|When|What|Where|Why|How|Now|"
                            r"Like|Best) [a-z]{2,}"),                                # "don't bother Best we lay low"
    "comma_stop": re.compile(r"\b[a-z]{2,}, (?:All|Just|He|She|They|We|You|The|This|That|There|Take|What|But|So|Then|"
                             r"When|Now|It|His|Her) [a-z]{2,}"),             # "sir, Just in the hall"  
    "title_nospace": re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St)\.[A-Z]"),           # "Mr.Smith" 
    "lone_letter": re.compile(r"\b[a-z]{2,} ([b-hj-np-z]) [a-z]{2,}\b"),     # "climbed the r into the belly"
    "space_punct": re.compile(r"\b[a-z]{2,} [,;:](?=\s)|\b[a-z]{2,} \.(?=\s+[A-Z“\"]|$)"),  # "the others ."
    "stop_inside": re.compile(r"\b([a-z]{2,})\. [a-z]{2,}\b"),              # "make folk. think"
    "camel": re.compile(r"\b(the|and|in|of|to|at|from|between|with|by|for|on)([A-Z][a-z]{2,})\b"),  # "inDresden", not "clawDrill"
    # "n°w", "h,gh", and a degree sign after no number ("the sound ° screeching")
    "symbol_word": re.compile(r"[A-Za-z][°~^•|\\][A-Za-z]|\b[a-z]+,[a-z]+\b|(?<![\d°′″ ])\s?°(?!\s*[CFK]?\d|\s*[CFK]\b)(?=\s|[a-z])"),
    "soft_hyphen": re.compile(r"\b([a-z]+)[¬?]\s*([a-z]+)\b"),                # "consider¬able", "poi?sonous"
    "digit_word": re.compile(r"\b[a-z]+[0-9][a-z]+\b"),                     # "i1n", "y1ears"
    "nonlatin": re.compile(r"[A-Za-z][\u0400-\u04ff\u0590-\u05ff]|[\u0400-\u04ff\u0590-\u05ff][A-Za-z]"),  # not Greek: σY is maths
    "mojibake_letter": re.compile(r"Ð(?=[\s'’\"“”,.!?])|(?<=[\s'’\"“”])Ð|[ÕÒÓÔ](?=[\s,.;:!?])"),  # "andÐ'"
    "backtick": re.compile(r"`"),
    "wrong_way": re.compile(r"[.,!?]“(?=\s)|(?<=\s)”(?=[A-Za-z])"),       # "Get on with it.“ Then"    
    "colon_quote": re.compile(r"[A-Za-z!?][:;]['’](?=\s)"),                 # "Useful;' he remarked"    
    "page_furniture": re.compile(r"file:///|\b\d{1,4} (?:de|of) \d{1,4}\b|\b\d{1,2}/\d{1,2}/\d{2,4},? \d{1,2}:\d{2}"),
    # real words that OCR made from other words, in a context where only the other word fits
    "ocr_pairs": re.compile(r"\b(?:in|on|at|of|into|from|through|across|over|under|by) die (?=[a-z])|\b(?:the|a) comer\b|"
                            r"\bcomer of\b|\bthe doth\b|\b(?:can|could|to|you) sec\b|\bup form\b|\bnot ail\b|"
                            r"\bail (?:the|of|that|this|his|her|my)\b|\bwhile hair\b|\bhe was tail\b"),
    "mixed_case": re.compile(r"(?<!['’-])\b[a-z]{1,3}[A-Z][a-z]{3,}\b"),  # "tWhienter": two text layers
    "capital_i": re.compile(r"\b[A-Z]?[a-z]{2,}I\b"),                        # "PauI": a final l read as I
}


# sign -> (issue code, what it shows, WARN limit per 10k words, REPLACE limit or None, smallest count).
# Limits set on half of a hand-read library and checked on the other half; see README.
SIGN_LIMITS = {
    "lost_open_quote": ("speech_marks", "speeches without their opening quote mark", 2.5, None, 15),
    "split_speech": ("speech_marks", "speeches split in two at a page break", 4.5, None, 10),
    "two_speakers": ("speech_marks", "paragraphs that hold two speakers", 6.0, None, 10),
    "wrong_way": ("speech_marks", "quote marks that face the wrong way", 2.0, None, 10),
    "mixed_quotes": ("speech_marks", "speeches that open with one kind of quote mark and close with another", 10.0, None, 20),
    "colon_quote": ("speech_marks", "speech ends read as ;' or :'", 3.0, 20.0, 10),
    "backtick": ("speech_marks", "backticks used as quote marks", 1.5, None, 10),
    "unquoted_speech": ("speech_marks", "speech with no quote marks", 20.0, 30.0, 30),
    "lost_apostrophe": ("speech_marks", "contractions without their apostrophe", 3.0, 20.0, 10),
    "hyphen_dash": ("dashes", "hyphens where a dash belongs", 4.0, None, 15),
    "space_hyphen": ("dashes", "dashes printed as a hyphen and a space", 10.0, None, 15),
    "nospace_end": ("ocr_punctuation", "sentences with no space after the full stop", 5.0, None, 10),
    "lost_stop": ("ocr_punctuation", "sentence ends with the full stop lost", 4.0, 20.0, 20),
    "comma_stop": ("ocr_punctuation", "commas where a full stop belongs", 3.0, None, 10),
    "title_nospace": ("ocr_punctuation", "titles with no space after them (Mr.Smith)", 10.0, None, 8),
    "lone_letter": ("ocr_punctuation", "single letters left of lost words", 10.0, None, 10),
    "space_punct": ("ocr_punctuation", "spaces before a comma or full stop", 3.0, None, 10),
    "stop_inside": ("ocr_punctuation", "full stops inside a sentence", 3.0, None, 10),
    "camel": ("ocr_punctuation", "words run into a capitalised word (inDresden)", 3.0, None, 10),
    "symbol_word": ("ocr_punctuation", "symbols inside words (n°w, h,gh)", 2.0, 15.0, 10),
    "nonlatin": ("ocr_punctuation", "Cyrillic or Hebrew letters inside English words (Londoн, cafי)", 1.0, 10.0, 5),
    "ocr_pairs": ("ocr_punctuation", "real words that OCR made from others (die air, the comer, can sec)", 1.0, None, 5),
    "mixed_case": ("ocr_punctuation", "words spliced from two text layers (tWhienter)", 1.0, 3.0, 5),
    "capital_i": ("ocr_punctuation", "a final l read as a capital I (PauI)", 1.0, None, 5),
    "digit_word": ("digits_in_words", "digits inside words (i1n, y1ears)", 1.0, None, 20),
    "mojibake_letter": ("mojibake", "dashes or quotes read in the wrong code page (andÐ')", 1.0, None, 5),
    "soft_hyphen": ("hyphens", "print soft hyphens kept as ¬ or ?", 3.0, None, 10),
    "title_heads": ("page_furniture", "running heads with the book's title inside the text", 1.2, None, 10),
    "page_furniture": ("page_furniture", "browser print furniture (file:///, page N of M, dates)", 2.0, None, 20),
}
STYLE_LIMITS = {"italic_10k": 1.0, "italic_min_words": 30000, "bold_pct": 50.0, "quote_pct": 90.0,
                "line_para_words": 10.0, "line_unended_pct": 50.0, "line_min_paras": 200}


def _fiction(meta):
    tags = {t.lower() for t in (meta or {}).get("tags") or []}
    return bool(tags & {"fantasy", "science fiction", "fiction", "romance", "classics", "thriller", "mystery",
                        "horror", "young adult", "star trek", "star wars"})


def _weak_signs(m):
    """Speech, dash and OCR signs at half their WARN limit or more (and 5+ cases): each too small
    to report, together the mark of an old scan or e-text."""
    out = []
    for name, (code, what, warn, _, least) in SIGN_LIMITS.items():
        rate = m.get(f"{name}_10k", 0)
        if code in ("speech_marks", "dashes", "ocr_punctuation") and rate >= 0.5 * warn and rate * m["words"] / 1e4 >= 5:
            out.append(what)
    return out


def _style_issues(m, meta, out):
    """Whole-book markup: no italics in a novel (thoughts and telepathy read as narration), all
    text bold (BookDesigner), every paragraph a blockquote, every printed line a paragraph."""
    L = STYLE_LIMITS
    if "italic_10k" in m and _fiction(meta) and m["words"] >= L["italic_min_words"] and m["italic_10k"] < L["italic_10k"]:
        # Many kept copies have no italics either (old e-texts, fan translations), so alone it is a
        # note. With two weaker signs of an old e-text it is a WARN.
        weak = _weak_signs(m)
        have = {i["code"] for i in out}
        old = len(weak) >= 2 and not have & {"speech_marks", "dashes", "ocr_punctuation"}
        out.append(issue("old_etext" if old else "no_italics", WARN if old else INFO,
                         f"Almost no italics ({m['italic_10k']:.1f} italic runs per 10,000 words): in a novel, thoughts, "
                         "telepathy, titles and emphasis then read as narration."
                         + (" With smaller signs of an old e-text: " + ", ".join(weak) + "." if old else "")))
    if m.get("bold_pct", 0) >= L["bold_pct"]:
        out.append(issue("bold_text", WARN, f"{m['bold_pct']:.0f}% of the text is bold (a BookDesigner or similar "
                                            "export set the body weight)."))
    if m.get("quote_pct", 0) >= L["quote_pct"]:
        out.append(issue("blockquote_text", INFO, f"{m['quote_pct']:.0f}% of the text sits in blockquotes: a reader "
                                                  "indents every paragraph like a quotation."))
    if m.get("para_words", 99) < L["line_para_words"] and m.get("unended_pct", 0) >= L["line_unended_pct"] \
            and m.get("paras_10k", 0) * m["words"] / 1e4 >= L["line_min_paras"]:
        out.append(issue("line_paragraphs", REPLACE, f"Every printed line is a paragraph: {m['para_words']:.0f} words a "
                                                     f"paragraph, {m['unended_pct']:.0f}% of them stop mid-sentence."))


SIGN_LEVEL = {"speech_marks": WARN, "dashes": WARN, "ocr_punctuation": WARN, "digits_in_words": WARN,
              "mojibake": FIX, "hyphens": FIX, "page_furniture": WARN}


def _sign_issues(m, signs, out):
    """Turn sign rates into issues; one issue per code, naming every sign over its limit. Signs that
    another issue already explains stay silent: a wrong code page garbles quotes and dashes too,
    and print lines turned into paragraphs cut speeches in two."""
    by_code = defaultdict(list)
    have = {i["code"] for i in out}
    for name, (code, what, warn, bad, least) in SIGN_LIMITS.items():
        if have & {"garbage", "mojibake"} and code in ("speech_marks", "dashes", "ocr_punctuation"):
            continue
        if name == "split_speech" and "broken_lines" in have:
            continue
        rate = m.get(f"{name}_10k", 0)
        k = round(rate * m["words"] / 1e4)
        if name == "mixed_quotes" and rate < MIXED_SHARE * m.get("quote_marks_10k", 0) / 2:
            continue  # a bad copy mixed the marks in 63% of its speeches; clean copies in 10-23%
        if rate >= warn and k >= least:
            by_code[code].append((bad is not None and rate >= bad, f"{k} {what} ({rate:.1f} per 10,000 words)", name))
    for code, found in by_code.items():
        level = REPLACE if any(r for r, _, _ in found) else SIGN_LEVEL[code]
        examples = [e for _, _, name in found for e in signs.get(name, [])[:3]]
        if code in have:  # "hyphens", "headers": add to the issue the older rule made
            i = next(i for i in out if i["code"] == code)
            i["msg"] += " Also: " + "; ".join(t for _, t, _ in found) + "."
            i.setdefault("evidence", {}).setdefault("examples", []).extend(examples[:4])
            continue
        out.append(issue(code, level, "; ".join(t for _, t, _ in found).capitalize() + ".", examples=examples[:8]))


MIXED_SHARE = 0.4  # mixed speeches as a share of all speeches (quote marks / 2) for speech_marks
MIXED_QUOTES = re.compile(r"(?:^|\s)\"[^\"“”\n]{2,300}?[,.!?…—]”|(?:^|\s)“[^\"“”\n]{2,300}?[,.!?…—]\"(?=\s|$)|"
                          r"(?:^|\s)'[A-Z][^'\"“”\n]{2,300}?[,.!?…—]\"(?=\s|$)")


def _open_at_end(text):
    """The paragraph ends inside an open quotation (double quotes, or single quotes that are not
    apostrophes)."""
    if "“" in text or "”" in text:
        return text.count("“") > text.count("”")
    if '"' in text:
        return text.count('"') % 2 == 1
    opens = [mm.end() for mm in re.finditer(r"(?:^|(?<=[\s(—–]))‘", text)]
    if not opens:
        return False
    return not re.search(r"[’'](?=[\s,.;:!?—–)]|$)", text[opens[-1]:])


def _closes_first(text):
    """A paragraph that closes a quotation before it opens one: the rest of a split speech."""
    close = re.search(r"[^\s][”\"’'](?=[\s,.;:!?—–)]|$)", text)
    if not close:
        return False
    open_ = OPEN_QUOTE.search(text)
    return open_ is None or open_.start() > close.start()


def _signs(back, blocks, n, m, meta, at_img=frozenset()):
    count, ex, spliced = Counter(), defaultdict(list), defaultdict(list)

    def hit(name, text, mm=None, k=1):
        count[name] += k
        if len(ex[name]) < 5:
            if mm is None:
                ex[name].append(_cut(text, 16))
            else:
                ex[name].append(f"…{text[max(0, mm.start() - 30):mm.end() + 25]}…")

    title = re.split(r"[:(]", (meta or {}).get("title", ""))[0].strip()
    head_rx = None
    if len(title) >= 8 and len(title.split()) >= 2:  # a one-word title also occurs in the story
        t = re.escape(title).replace(r"\ ", r"\s+")
        head_rx = re.compile(rf"\d{{1,4}}\W{{0,4}}{t}|{t}\W{{0,4}}\d{{1,4}}|/\s*{t}|{t}\s*/", re.I)
    paras = [(bi, pi, text) for bi, (pi, kind, text) in enumerate(blocks)
             if kind == "p" and pi not in back and not URL_TEXT.search(text) and not CODEISH.search(text)
             and not CODE_LINE.match(text)]
    words = lines = unended = 0
    for k, (_, pi, text) in enumerate(paras):
        w = len(text.split())
        words += w
        lines += text.count("\n")
        unended += w >= 3 and not ends_sentence(text)
        for name, rx in SIGN_RX.items():
            for mm in rx.finditer(text):
                if name == "hyphen_dash" and (mm.group(2) not in DASH_WORD or W.is_word(mm.group(0).lower())):
                    continue
                if name == "space_hyphen" and W.is_word(mm.group(1) + mm.group(2)):
                    continue  # "exam- ple": a print hyphen (counted as hyphens), not a dash
                if name == "soft_hyphen" and not W.is_word(mm.group(1) + mm.group(2)):
                    continue
                if name == "stop_inside" and mm.group(1) in NOT_ABBREV:
                    continue

                if name == "mixed_case" and (W.is_word(mm.group(0).lower()) or re.match(r"(?:mc|mac|de|di|la|le|van|von|o)[A-Z]", mm.group(0))
                                             or W.is_word(re.sub(r"^[a-z]+", "", mm.group(0)).lower())):
                    continue  # "iPhone", "McDonald", "theWorld" (a camel join, counted there)
                if name == "mixed_case":
                    spliced[mm.group(0)].append((text, mm))
                    continue
                hit(name, text, mm)
        if head_rx:
            for mm in head_rx.finditer(text):
                hit("title_heads", text, mm)
        for mm in CONTRACTION_LOST.finditer(text):
            hit("lost_apostrophe", text, mm)
        marks = len(QUOTE_MARK.findall(text))
        count["quote_marks"] += marks
        tag = CLOSE_TAG.search(text)
        if tag and text[:1].isalpha() and not OPEN_QUOTE.search(text[:tag.start()]):
            hit("lost_open_quote", text, tag)  # "Tall man, by the look of him,' said the farmer."
        subjects = {(a or b).lower() for a, b in TAG_SUBJECT.findall(text)}
        if len(subjects) >= 2:
            hit("two_speakers", text)
        if not marks and BARE_TAG.search(text):
            hit("unquoted_speech", text)  # "Who are you people? asked the priest."
        if k + 1 < len(paras) and paras[k + 1][1] == pi and paras[k + 1][0] not in at_img:
            nxt = paras[k + 1][2]
            if _open_at_end(text) and not OPEN_QUOTE.match(nxt) and _closes_first(nxt):
                hit("split_speech", f"{_cut(text[-120:], 10)} ¶ {_cut(nxt, 8)}")
            elif re.search(r",[”\"’']$", text.rstrip()) and re.match(SUBJECT + r"\s+" + SPEECH_VERB + r"\b", nxt):
                hit("split_speech", f"{_cut(text[-120:], 10)} ¶ {_cut(nxt, 8)}")
        # a speech that opens with a straight quote and closes with a curly one ("Arrested?”) or the
        # reverse, or opens with a single quote and closes with a double
        count["mixed_quotes"] += len(MIXED_QUOTES.findall(text))
    for found in spliced.values():
        if len(found) < 3:  # a spelling used 3+ times ("vEnmade") is the author's
            for text, mm in found:
                hit("mixed_case", text, mm)
    for name, c in count.items():
        m[f"{name}_10k"] = round(1e4 * c / max(1, n), 1)
    m["para_words"] = round(words / max(1, len(paras)), 1)
    m["unended_pct"] = round(100 * unended / max(1, len(paras)), 1)
    m["br_10k"] = round(1e4 * lines / max(1, n), 1)
    m["paras_10k"] = round(1e4 * len(paras) / max(1, n), 1)
    return {k: v for k, v in ex.items()}


# ---------------------------------------------------------------- LLM samples, MinHash

def _body(doc, back):
    """The main text as a flat list of (part, block index, kind, text): front and back matter
    left out (contents, copyright, praise, glossary, notes, an author's note...), and list-like
    parts (a contents page, an index). A book that is only lists (verse) keeps every part."""
    flat, keep = [], []
    for pi, p in enumerate(doc.parts):
        first = p.blocks[0][1] if p.blocks else ""
        front = FRONT_MATTER.search(p.name) or FRONT_NAME.search(p.name) or \
            (len(first) <= 60 and FRONT_MATTER.search(first))  # a heading, not "she took cover"
        short = sum(len(t.split()) <= 6 for _, t in p.blocks)
        listy = len(p.blocks) >= 10 and short >= 0.6 * len(p.blocks)
        words = sum(len(t.split()) for _, t in p.blocks)
        rows = [(pi, bi, k, t) for bi, (k, t) in enumerate(p.blocks)]
        flat += rows
        if pi not in back and not front and not listy and words >= 150:
            keep += rows
    rows = keep or flat
    # One big part (a converted MOBI): cut at a front-matter heading in the first 15% of the
    # words and at a back-matter heading in the last 30%.
    total = sum(len(t.split()) for *_, t in rows)
    at, lo, hi = 0, 0, len(rows)
    for i, (_, _, k, t) in enumerate(rows):
        if len(t) <= 60 and (k == "h" or len(t.split()) <= 6):
            if at <= 0.15 * total and FRONT_MATTER.match(t):
                lo = i + 1
            elif at >= 0.7 * total and BACK_MATTER.match(t) and hi == len(rows):
                hi = i
        at += len(t.split())
    return rows[lo:hi] if hi - lo >= 20 else rows


def page_shaped(w, h):
    return w * h >= PHOTO_PIXELS and h and PHOTO_ASPECT[0] <= w / h <= PHOTO_ASPECT[1]


def _render(doc, rows, i, words):
    """About `words` words of reader's view from rows[i]: a blank line between paragraphs,
    ⏎ for a line break inside one, ## for a heading, and [picture WxH, ...] where an image
    shows, with its shape and place, so the reader can tell an ornament from a page photo."""
    out, count = [], 0
    while i < len(rows) and count < words:
        pi, bi, kind, text = rows[i]
        for b, _, w, h, kb in doc.parts[pi].images:
            if b == bi:
                prev = rows[i - 1][3] if i and rows[i - 1][0] == pi else ""
                mid = bool(prev) and not ends_sentence(prev) and text[:1].islower()
                out.append(f"[picture {w}x{h}, {'page-shaped' if page_shaped(w, h) else 'not page-shaped'}, "
                           f"{'mid-sentence' if mid else 'between paragraphs'}]")
        out.append(("## " if kind == "h" else "") + text.replace("\n", " ⏎ "))
        count += len(text.split())
        i += 1
    return "\n\n".join(out), i


def _samples(blocks, words_in_block, positions, doc):
    """Pages for the LLM reader: PAGE_WINDOWS stretches of about three pages each, spread through
    the main text, and the densest cluster of suspect words when it falls outside them."""
    back = _back_matter(doc)
    rows = _body(doc, back)
    if not rows:
        return {}
    starts, at = [], 0
    for *_, t in rows:
        starts.append(at)
        at += len(t.split())
    total = at
    samples, taken = {}, set()
    for k, share in enumerate(PAGE_WINDOWS, 1):
        i = next((j for j, s in enumerate(starts) if s >= share * total), len(rows) - 1)
        text, end = _render(doc, rows, i, WINDOW_WORDS)
        if text and i not in taken:
            samples[f"pages{k}"] = text
            taken |= set(range(i, end))
    if positions:  # positions are in the check's word stream; map them to a block of `blocks`
        cum, at = [], 0
        for wc in words_in_block:
            at += wc
            cum.append(at)
        best, j = (0, positions[0]), 0
        for i, p in enumerate(positions):
            while positions[j] < p - SAMPLE_WORDS:
                j += 1
            if i - j + 1 > best[0]:
                best = (i - j + 1, positions[j])
        bi = next((x for x, c in enumerate(cum) if c > best[1]), len(cum) - 1)
        pi, text = blocks[bi][0], blocks[bi][2]
        i = next((x for x, r in enumerate(rows) if r[0] == pi and r[3] == text), None)
        if i is not None and i not in taken and best[0] >= 3:
            # Start two paragraphs early: the scene around odd text tells the reader whether the
            # story means it (a forensic test in a thriller shows a document's faint impression).
            start = i
            while i > 0 and start - i < 2 and rows[i - 1][0] == pi and (i - 1) not in taken:
                i -= 1
            samples["densest"] = _render(doc, rows, i, SAMPLE_WORDS + CONTEXT_WORDS)[0]
    return samples


MERSENNE = (1 << 61) - 1
# Large random multipliers, fixed so that signatures stay comparable across runs. With a
# small multiplier a*h + b never wraps mod p, every function picks the same smallest
# shingle, and unrelated books "match" (found on a real library).
_rng = random.Random(20261019)
HASHES = [(_rng.randrange(1, MERSENNE), _rng.randrange(0, MERSENNE)) for _ in range(64)]


def minhash(stream):
    shingles = {zlib.crc32(" ".join(stream[i:i + 5]).encode()) for i in range(min(len(stream), 5000) - 4)}
    if not shingles:
        return []
    return [min((a * h + b) % MERSENNE for h in shingles) for a, b in HASHES]


def level_of(issues):
    return max((i["level"] for i in issues), key=RANK.get, default=INFO)
