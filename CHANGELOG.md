# Changelog

## 0.1.0 (2026-11-02)

The first public release.

### Added

- Checks for OCR damage, broken lines, lost or repeated text, missing chapters and pages, DRM and
  broken files, with one verdict per book: KEEP, WARN, FIX or REPLACE.
- HTML, CSV and JSON reports, metadata rules for calibre libraries, and an optional LLM review.
- A `tidy-calibre-library` command, and install with `pipx`.
- The word list now downloads on the first run. `fetch-words.sh` is gone, so curl, tar and iconv
  are no longer needed.
- A Docker image of 656 MB, with calibre's own Linux build without the parts that converting to
  EPUB does not use. The image on the linuxserver calibre desktop was 3.6 GB.

### Fixed

- **Install**
  - The tool crashed in a Python environment that also held the `llm` or `configobj` package,
    because its seven modules were at the top level. They are now one package,
    `tidy_calibre_library`.
  - `--env-file` now defaults to `.env` in the current folder, not next to the program.
- **Conversions**
  - Conversions that started together on a new calibre config (each Docker run) could fail, and a
    good book got `convert_failed` REPLACE. calibre now runs once before the workers start.
  - calibre gave a file that states no language (TXT, RTF, PDB) the locale's language, and an
    English book on a German system got `language_metadata` WARN. Now calibre gives it `und`,
    and `und` counts as no language.

Faults found by a review of the code.

- **Hidden damage**
  - A damaged file inside an EPUB gave `checker_error`. It now gives `damaged` REPLACE, and the
    other files are still checked.
  - A scan with no text layer skipped the repeated-page check.
  - A file named `index.html`, or one that opened with "Also by…", skipped most checks. Back
    matter can no longer be most of a book.
  - A jump of 4 or more chapters gave no `chapter_gap`.
  - An undeclared prefix such as `<o:p>` left the book unchecked.
- **Runs that stopped**
  - A token such as `0b1111…` could use all memory.
  - A `null` field in an LLM reply, a truncated cache file, a bad `metadata.opf`, a book added
    during the read and a dead worker process all stopped the run.
- **Wrong verdicts**
  - A class in a CSS selector's ancestor made a chapter preformatted (REPLACE).
  - The last word of a book not in English counted as cut off (REPLACE).
  - A sample notice about another book gave REPLACE. It is now a WARN in a book of 40,000+ words
    or in back matter, and no issue inside quotes.
  - A same-document link in the nav gave `toc_broken`.
- **Cache**
  - A failed conversion, a missing tool or a checker error stayed in the cache. These now run
    again. A failed conversion now has the code `convert_failed`, not `damaged`.
  - Two copies of one file could convert into one temp file.
- **Metadata**
  - All authors and titles in non-Latin scripts shared one key.
  - The tool suggested "Jr. Martin Luther King" and "Malcolm X.".
  - A library path with `#`, `?` or `%20` turned off the database rules.
