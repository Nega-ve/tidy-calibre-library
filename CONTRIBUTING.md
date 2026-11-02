# Contributing

The most useful help is a report of a wrong verdict. Each false alarm found so far became a
rule fix and a regression test, so one good report improves the tool for every library.

- [Report a false alarm](https://github.com/Nega-ve/tidy-calibre-library/issues/new?template=false_alarm.yml): the tool flags a good book.
- [Report a missed defect](https://github.com/Nega-ve/tidy-calibre-library/issues/new?template=missed_defect.yml): the tool passes a damaged book.

Do not attach a book, or long passages of one. Most books are under copyright. A short quote
from the report is enough.

## Set up

You need Python 3.10+ and poppler (`brew install poppler`, or `sudo apt install poppler-utils`).

```sh
git clone https://github.com/Nega-ve/tidy-calibre-library
cd tidy-calibre-library
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/tidy-calibre-library --help
```

The first test run downloads the word list. The two calibre tests are skipped when
`ebook-convert` is missing.

## Find your way

```
tidy_calibre_library/
validate.py   command line, scan, cache, verdicts
   ├─ books.py    file → Doc (parts of (kind, text) blocks); file-level issues
   ├─ checks.py   Doc → metrics, issues, samples
   │    └─ words.py   what is a word; OCR-swap, glued and archaic rules
   ├─ llm.py      the optional review
   ├─ metadata.py the metadata rules
   └─ report.py   HTML, CSV and JSON
```

The first seven files below are in `tidy_calibre_library/`. The others are at the top of the repo.

| File | Job |
|---|---|
| `validate.py` | Command line, cache, verdicts, library notes |
| `books.py` | Reads EPUB, PDF, DjVu and converted formats (and a MOBI's own text records); file-level checks |
| `checks.py` | Text, layout, duplicate, gap and page checks; `LIMITS`, `SIGN_LIMITS`, `STYLE_LIMITS` |
| `words.py` | What is a word; OCR-swap, glued and archaic rules; builds the word list from SCOWL |
| `metadata.py` | Metadata rules (authors, sort values, language, ISBN, cover) |
| `llm.py` | The optional OpenRouter review |
| `report.py` | HTML, CSV and JSON reports |
| `tests.py`, `fixture.txt` | Planted-fault and regression tests |
| `docs/CHECKS.md` | Every check with its limit, and how the limits were set |
| `docs/make_demo.py` | Builds the demo library, the sample report and the pictures |
| `pyproject.toml` | Package and the `tidy-calibre-library` command. The dependencies are in `requirements.txt`, because the Docker image installs from it too |
| `Dockerfile` | Container with calibre, poppler and djvulibre ([change the calibre version](#change-the-calibre-version)) |

The modules are in one package, so the tool adds one top-level name to a Python environment. At
the top level, `llm.py` and `validate.py` clashed with the `llm` and `configobj` packages. A new
module in the folder needs no entry in `pyproject.toml`. Import a module beside it with
`from . import words`. To run a checkout without the install, use
`python3 -m tidy_calibre_library.validate`.

## Change a check

`tests.py` plants each fault into clean text (`fixture.txt`: the opening of *Pride and
Prejudice*, public domain) and checks that only the matching check fires.

1. Write the test first. Build a book with `make_epub(...)` or `make_pdf(...)`, and read it with
   `found(path)`, which returns `{issue code: level}`.

   ```python
   def test_missing_chapter(epub):
       chaps = chapters()
       del chaps[4]
       assert found(epub(chaps)) == {"chapter_gap": WARN}
   ```

2. For a false alarm, build the smallest book that shows it. Do not paste in book text under
   copyright: write a sentence of the same shape.
3. Fix the rule. A comment with a real example explains a rule better than a description
   ("…to the old harbour at Kestr": the file stops inside a word).
4. Update `docs/CHECKS.md` when a limit or a rule changes.

## Change the calibre version

The Dockerfile removes the parts of calibre that converting to EPUB does not use. A new calibre
can need one of them. Then every conversion fails, and each book looks damaged (REPLACE). No CI
job builds the image, so check it by hand for each new calibre version or Python image.

1. In `Dockerfile`, set `CALIBRE` and the two `sha=` values. Get each value from
   `https://calibre-ebook.com/signatures/calibre-VERSION-ARCH.txz.sha512` (ARCH: `arm64`,
   `x86_64`).
2. Build the image and run the tests in it, for both platforms:

   ```sh
   for arch in arm64 amd64; do
     docker build --platform linux/$arch -t tidy-calibre-library:$arch .
     docker run --rm --platform linux/$arch --entrypoint sh tidy-calibre-library:$arch \
       -c 'pip install -q pytest && cd /app && python -m pytest -q -rs tests.py'
   done
   ```

3. Every test must pass, with none skipped. `test_every_format_calibre_writes_reads_clean`
   converts each format. If it fails, run `ebook-convert` on the file in the image and read the
   error: it names the missing library. `ebook-convert --version` can work when conversions fail.

## Keep in mind

- **False alarms cost more than misses.** A REPLACE tells someone to get another edition, so it
  needs strong evidence. When in doubt, give a WARN.
- **Results are cached by the source.** A change to `books.py`, `checks.py` or `words.py` makes
  every book run again. A result about this machine or this run (`NOT_CACHED` in `validate.py`)
  is never cached.
- **Books are untrusted downloads.** XML is parsed with entities and network access off. One bad
  file must never stop a run.
- **It only reads.** Nothing may write to the library.

## Before a pull request

- `.venv/bin/pytest` passes.
- One fault or feature per pull request, with its test.
- Match the code around your change.
- If the report changes, run `python3 docs/make_demo.py` to update the sample report and pictures.
- Add a line to `CHANGELOG.md`.
