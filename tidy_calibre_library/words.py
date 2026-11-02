"""English words for the OCR checks.

A token is a word when SCOWL lists it (sizes up to 80, every spelling variant; see
fetch()) or when it is very common in today's English (wordfreq zipf >= 4),
which covers modern words SCOWL lacks. wordfreq alone cannot decide this: its corpora
carry OCR errors too ("tbe" scores 2.6, "ofthe" 3.3).

A lowercase non-word is then explained, in this order, as
  ocr     one typical OCR confusion away from a common word: tlie->the, rnore->more,
          fuch->such (long s), l0ve->love
  glued   a function word run into another word: ofthe, andthe, thewindow
  other   anything else: a rare word SCOWL lacks, dialect, a name, or noise
Archaic verb forms ("sittest", "showeth") and dialect that drops a final g ("goin")
are "other". Glued needs a function word because compounds and dialect ("fitten",
"dangersome", "oncommon") split into two common words just as easily.
"""
import hashlib
import io
import os
import re
import tarfile
import tempfile
import unicodedata
from functools import lru_cache

from wordfreq import zipf_frequency

CACHE_HOME = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
WORD_FILES = (os.environ.get("QA_WORDS"), os.path.join(CACHE_HOME, "tidy-calibre-library", "words.txt"))
SCOWL_URL = "https://downloads.sourceforge.net/project/wordlist/SCOWL/2020.12.07/scowl-2020.12.07.tar.gz"
SCOWL_SHA256 = "5587667caa20c4891390c2d42dbb4d5c4c3f41bee77af1457ece3ba23fb859cc"
SCOWL_LIST = re.compile(r"/final/[^/]+-(?:words|contractions)\.(?:10|20|35|40|50|55|60|70|80)$")
COMMON = 4.0  # zipf at which a token is a word even when SCOWL lacks it
TARGET = 3.5  # zipf the corrected word must reach for an "ocr" explanation
PART = 4.5    # zipf each half of a "glued" word must reach

WORD, OCR, GLUED, OTHER = "word", "ocr", "glued", "other"

# (seen, meant): the OCR read `seen` where the page said `meant`. One swap per token.
SWAPS = (("rn", "m"), ("m", "rn"), ("li", "h"), ("h", "li"), ("cl", "d"), ("d", "cl"),
         ("vv", "w"), ("ii", "u"), ("ii", "n"), ("in", "m"), ("m", "in"), ("ri", "n"),
         ("n", "ri"), ("b", "h"), ("h", "b"), ("c", "e"), ("e", "c"), ("u", "n"), ("n", "u"),
         ("l", "i"), ("i", "l"), ("t", "f"), ("f", "t"), ("f", "s"))
DIGITS = {"0": ("o",), "1": ("l", "i"), "5": ("s",)}
MAX_ONES = 8  # each "1" doubles the variants; a token with more is a number ("0b1111…"), not a misread word
ORDINAL = re.compile(r"\d+(st|nd|rd|th|s|d|mo|vo|to)")  # 1st, 1820s, 2d, 12mo, 8vo, 4to
FUNCTION = frozenset("the a an of to in at by for from with and or but as is was be been had has have "
                     "he she it we they his her its their him them my our your that this not would "
                     "could should will shall which who".split())
GLUE_ANCHOR = frozenset("the of and to in that with for from his her was had".split())
ARCHAIC = ("est", "eth", "edst", "st", "th")

_words = None


def ascii_fold(s):
    """"Jörn-Steffen" -> "Jorn-Steffen"."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def fetch(path):
    """Build the word list at path from SCOWL 2020.12.07: sizes 10-80, every spelling variant (US,
    British, Canadian, Australian, archaic), contractions. Proper names, abbreviations and size
    95 (rare junk) are left out. SCOWL is free to copy and use: http://wordlist.aspell.net/"""
    import requests
    print("Downloading the English word list (2.6 MB, once). The mirrors can take a minute.", flush=True)
    for _ in range(3):  # a SourceForge mirror took from 0.7 s to 51 s for this file
        try:
            r = requests.get(SCOWL_URL, timeout=120)
            r.raise_for_status()
        except requests.RequestException as e:
            problem = e
            continue
        if hashlib.sha256(r.content).hexdigest() == SCOWL_SHA256:
            break
        problem = "the file has the wrong sha256, so it was not used"
    else:
        raise SystemExit(f"The word list could not be downloaded: {problem}\n"
                         "Check the network and run again, or set QA_WORDS to a word list file.")
    lines = set()
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        for member in tar:
            if SCOWL_LIST.search(member.name):
                raw = tar.extractfile(member).read().lower()  # Latin-1 bytes, so only A-Z change case
                lines.update(line.decode("latin-1").encode() for line in raw.split(b"\n") if line)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.writelines(line + b"\n" for line in sorted(lines))
    os.chmod(tmp, 0o644)  # mkstemp makes it private; in the Docker image another user reads it
    os.replace(tmp, path)
    return path


def words():
    global _words
    if _words is None:
        path = next((p for p in WORD_FILES if p and os.path.exists(p)), None) or fetch(WORD_FILES[-1])
        with open(path, encoding="utf-8") as f:
            _words = frozenset(f.read().split())
    return _words


@lru_cache(maxsize=None)
def zipf(w):
    return zipf_frequency(w, "en")


def is_word(w):
    w = w.replace("’", "'")
    ws = words()
    if w in ws or (w.endswith("'s") and w[:-2] in ws):
        return True
    return zipf(w) >= COMMON


def _ocr_fix(t):
    best, best_z = None, TARGET
    ws = words()
    for seen, meant in SWAPS:
        i = t.find(seen)
        while i >= 0:
            v = t[:i] + meant + t[i + len(seen):]
            if v in ws:
                z = zipf(v)
                if z >= best_z:
                    best, best_z = v, z
            i = t.find(seen, i + 1)
    return best


def _digit_fix(t):
    if t.count("1") > MAX_ONES:
        return None
    variants = [""]
    for ch in t:
        variants = [v + r for v in variants for r in DIGITS.get(ch, (ch,))]
    good = [v for v in variants if v.isalpha() and is_word(v) and zipf(v) >= TARGET]
    return max(good, key=zipf) if good else None


def _glued_fix(t):
    if len(t) < 5:
        return None
    ws = words()
    for i in range(1, len(t)):
        a, b = t[:i], t[i:]
        if a not in ws or b not in ws:
            continue
        if (a in FUNCTION and b in FUNCTION) or \
                (a in GLUE_ANCHOR and len(b) >= 3 and zipf(b) >= PART) or \
                (b in GLUE_ANCHOR and len(a) >= 3 and zipf(a) >= PART):
            return f"{a} {b}"
    return None


def _archaic(t):
    for suf in ARCHAIC:
        stem = t[:-len(suf)]
        if t.endswith(suf) and len(stem) >= 2 and (is_word(stem) or is_word(stem[:-1]) or is_word(stem + "e")):
            return True
    return False


@lru_cache(maxsize=None)
def classify(t):
    """(kind, fix) for a lowercase token of 3+ characters."""
    if any(c.isdigit() for c in t):
        if t.isdigit() or ORDINAL.fullmatch(t):
            return WORD, None
        fix = _digit_fix(t)
        return (OCR, fix) if fix else (OTHER, None)
    if is_word(t):
        return WORD, None
    fix = _ocr_fix(t)
    if fix:
        return OCR, fix
    if (t.endswith("in") and is_word(t + "g")) or _archaic(t):
        return OTHER, None
    fix = _glued_fix(t)
    if fix:
        return GLUED, fix
    return OTHER, None


def joins_to_word(a, b):
    """True when "a b" is one word split in two: "beauti ful", "th e"."""
    j = a + b
    if len(j) < 3 or not (j in words() and zipf(j) >= TARGET):
        return False
    return not is_word(a) or not is_word(b)
