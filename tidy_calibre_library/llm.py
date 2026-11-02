"""A second opinion from a small LLM on how the text reads, through OpenRouter.

The checks count what they can see. They miss OCR errors that make real words ("Tail" for
"Taft"), and they cannot always tell dialect from damage. The model reads pages as a reader
sees them: four stretches of about three pages each, spread through the main text (front
and back matter left out), and the densest cluster of suspect words. It counts the errors
and rates the text 1 to 5 on a fixed scale of errors per page.

Privacy and cost: the passages (about 14 pages a book) and the title go to OpenRouter and
the model's provider. With claude-haiku-5.5 ($0.10 / $0.50 per million tokens, Oct 2026) a
book costs about $0.002. The review runs only when --llm asks for it. Answers are cached by
sample text, model and PROMPT_VERSION.

Prompt history: version 3 made the passages longer (about 12 pages in all, with pictures
and paragraph breaks visible) and tied the scale to errors per page, because short samples
rated a book with a photo of every printed page 4/5. Version 4 gave each picture its shape
and place (a scene-break ornament is not a page photo), and made text that the story shows
as damaged the author's. Version 5 added a rating per passage: damage in one passage only
gives a WARN, not a REPLACE (validate.effective).

The model reasons before it answers (about 1,100 hidden tokens on a 3,400-token request).
With max_tokens 400 it ran out before the answer and returned no text, so MAX_TOKENS
leaves room to reason.
"""
import hashlib
import json
import os
import re

import requests

from .books import read_json, write_json

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-haiku-5.5"
MAX_TOKENS = 3000
PROMPT_VERSION = 5
SAMPLE_KEYS = ("pages1", "pages2", "pages3", "pages4", "densest")  # checks._samples
PROBLEMS = ("ocr", "real_word", "digits", "spacing", "broken_lines", "paragraphs", "page_numbers", "headers",
            "page_images", "repeated", "missing", "garbled", "other")

SYSTEM = """You check e-book text for a reader who reads for pleasure. You get passages from one book, \
each about three printed pages, from the start, middle and end of the main text. Judge only how the \
text was reproduced, not the writing.

How the passages show the book: a blank line separates paragraphs; ⏎ is a line break inside a \
paragraph; "## " starts a heading; [picture WxH, shape, place] is an image at that place in the text.

Problems:
- OCR errors: wrong letters ("tlie", "featliers"), digits for letters ("1 was", "0, stop him"), and \
real words that do not fit ("Tail" for the name "Taft", "die" for "the", "dock" for "clock")
- words run together or split; a word cut at the left edge of the page ("oby Temple" for "Toby")
- broken lines: a sentence broken into several paragraphs or lines
- lost paragraphs: several speakers' dialogue run together into one long paragraph
- page numbers, running heads or scanner notes inside the text
- page images: photos of printed pages in the text, usually followed by the OCR text of each page, so a \
reader sees each page twice. Only a "page-shaped" picture can be one, and they come again and again, \
often mid-sentence. Any other picture is an illustration, a map or a scene-break ornament: not a problem.
- repeated or missing text, garbled characters or symbols inside words ("s^ter", "q|~'")
Not problems: old or British spelling, dialect, archaic words, unusual names, poetry line breaks, the \
author's style, illustrations, a passage that starts or ends mid-sentence (the excerpts are cut). \
Spelling that shows how a character speaks is the author's choice, not an error: a lisp ("Thith ith \
a thweet day"), a stutter, an accent, a monster's or alien's voice, or a joke the author makes with \
the text itself. So is text that the story itself shows as damaged or partial: a burnt or torn letter, \
a faded note, words recovered from an impression, a code or cipher. Judge OCR damage by narration and \
by errors that no speaker and no story would produce.

Count every reproduction error you see, then rate by errors per printed page:
5 = no errors
4 = at most one small slip in three pages; a reader would not mind
3 = about one error per page; readable but irritating
2 = several errors per page, or any of: lost paragraphs, page images, broken lines throughout, \
garbled passages; a reader would want another edition
1 = unreadable

Rate the book by all passages together, and also give each passage its own rating on the same scale.

Reply with JSON only:
{"errors": <number of errors you counted>, "rating": <1-5>, "passages": [<a 1-5 rating for each passage, \
in order>], "problems": [<zero or more of: ocr, \
real_word, digits, spacing, broken_lines, paragraphs, page_numbers, headers, page_images, repeated, \
missing, garbled, other>], "examples": [<up to 3 short quotes copied exactly from the passages>], \
"note": "<one sentence>"}"""


class Stop(Exception):
    """The account cannot continue (no key, no credit): stop asking for the rest."""


def key_from_env(env_file=None):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key and env_file and os.path.exists(env_file):
        for line in open(env_file, encoding="utf-8"):
            name, _, value = line.strip().partition("=")
            if name == "OPENROUTER_API_KEY":
                key = value.strip().strip("'\"")
    return key


def _items(reply, name):
    """A list field of the reply. A model can send null or a bare value there."""
    value = reply.get(name)
    return value if isinstance(value, list) else []


def review(samples, title, key, cache_dir, model=MODEL):
    """{"rating", "problems", "examples", "note", "model"} or {"error": ...}."""
    passages = [samples[k] for k in SAMPLE_KEYS if samples.get(k)]
    if not passages:
        return None
    body = f"Book: {title}\n\n" + "\n\n".join(f"=== Passage {i + 1} ===\n{p}" for i, p in enumerate(passages))
    def cached(version):
        tag = hashlib.sha256(f"{version}\n{model}\n{body}".encode()).hexdigest()
        return os.path.join(cache_dir, "llm", f"{tag}.json")
    path = cached(PROMPT_VERSION)
    answer = read_json(path)
    if answer is not None:
        return answer
    try:
        r = requests.post(URL, timeout=120, headers={"Authorization": f"Bearer {key}", "X-Title": "tidy-calibre-library"},
                          json={"model": model, "temperature": 0, "max_tokens": MAX_TOKENS,
                                "messages": [{"role": "system", "content": SYSTEM},
                                             {"role": "user", "content": body}]})
    except requests.RequestException as e:
        return {"error": f"request failed: {e}"}
    if r.status_code in (401, 402, 403):
        raise Stop(f"OpenRouter refused the key ({r.status_code}): {r.text[:200]}")
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    try:
        choice = r.json()["choices"][0]
        text = choice["message"]["content"]
        if not text:
            return {"error": f"no answer (finish_reason {choice.get('finish_reason')}); raise MAX_TOKENS"}
        # strict=False: a quote of shredded text can hold a raw line break inside a string
        out = json.loads(re.search(r"\{.*\}", text, re.S).group(0), strict=False)
        rating = int(out["rating"])
        if not 1 <= rating <= 5:
            raise ValueError(rating)
    except (KeyError, IndexError, AttributeError, ValueError, TypeError) as e:
        return {"error": f"unreadable answer: {e}"}
    per = [int(x) for x in _items(out, "passages") if str(x).isdigit() and 1 <= int(x) <= 5]
    result = {"rating": rating, "errors": out.get("errors"), "passages": per, "pages": round(sum(len(p.split()) for p in passages) / 250),
              "problems": [p for p in _items(out, "problems") if p in PROBLEMS],
              "examples": [str(x)[:200] for x in _items(out, "examples") if x][:3],
              "note": str(out.get("note") or "")[:300], "model": model}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json(path, result)
    return result
