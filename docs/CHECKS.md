# What tidy-calibre-library checks

The reference: every check with its limit, the metadata rules, the repairs, the optional LLM
review, and how the limits were set. For a first try, the [README](../README.md) is enough.

## What it checks

| Group | Check (issue code) | How | Level |
|---|---|---|---|
| File | `damaged`, `drm`, `missing_files`, `malformed_xhtml`, `toc_broken`, `no_toc`, `fixed_layout` | zip CRC (the undamaged files are still checked); DRM files (`rights.xml`, non-font `encryption.xml`); every manifest and spine file exists; strict XHTML parse; TOC links resolve | REPLACE…WARN |
| | `convert_failed` | calibre cannot read the file (REPLACE), or it timed out or was stopped (WARN); tried again on the next run | REPLACE / WARN |
| | `dup_spine`, `duplicate_file` | the reading order lists one file twice, or a chapter file of 300+ words repeats another file whole: nothing is lost. A shorter page printed twice (an epigraph, a copyright page) is `repeat_by_design` | FIX |
| | `mis_encoded` | a UTF-8 MOBI whose own text holds U+0080–U+009F (Windows-1252 dashes and quotes read as Latin-1). `ebook-convert` deletes them without a word, so the tool reads the MOBI's text records itself | FIX at 20 |
| Text | `nonwords` | lowercase words of 3+ letters that are not in SCOWL (size 80, all spelling variants) and not common in wordfreq; a word used 3+ times is the book's own word; a block mostly in another language is left out | WARN 1.5% · REPLACE 4% |
| | `ocr_swaps` | non-words one OCR confusion from a common word: `tlie`→the, `rnore`→more, `fuch`→such, `l0ve`→love; one word counts at most 3 times | WARN 5 · REPLACE 25 per 10k words |
| | `glued`, `split_words` | `ofthe`, `thewindow` (a function word is required, and 3+ different words); `beauti ful` | WARN 6 / 3 · REPLACE 30 / 20 per 10k |
| | `mojibake` | UTF-8 read as Windows-1252 (`â€™`), Mac Roman read as Latin-1 (`andÐ'`) | FIX at 3 per 10k |
| | `garbage` | `�`, control characters, `?` inside a word whose halves are no word: the letters are lost | WARN 3 · REPLACE 50 per 10k |
| | `watermark` | download-site marks; scanner and e-text credits on a short line of their own (`Scanned by …`, `created with BookDesigner`); a `.com`-type URL repeated 5+ times in 3+ chapters (not on a copyright page, not the author's site) | FIX |
| | `digit_letters` | `1`/`l` for `I` before words such as *was*, *had*, *think*; `0,` for `O,`; blocks with other numbers are skipped | WARN at 5+ and 1 per 10k |
| | `speech_marks` | per 10k words: speeches without an opening quote (2.5), split at a page break (4.5), two speakers in one paragraph (6), quotes facing the wrong way (2), mixed straight and curly marks (10, in 40%+ of speeches), `;'` for `."` (3; REPLACE 20), backticks (1.5), speech with no quote marks (20; REPLACE 30), contractions without apostrophes (3; REPLACE 20) | WARN / REPLACE |
| | `dashes` | a hyphen where a dash belongs (`he paused-and then`, 4 per 10k); `a man- one man` (10) | WARN |
| | `ocr_punctuation` | per 10k words: no space after a full stop (5), a lost full stop (4; REPLACE 20), a comma for a full stop (3), a full stop inside a sentence (3), a space before punctuation (3), `inLondon` (3), symbols inside words (`n°w`, `h,gh`: 2; REPLACE 15), Cyrillic or Hebrew letters in English words (1), real words made by OCR in context (`die air`, `the comer`: 1), words spliced from two text layers (`tWhienter`: 1; REPLACE 3), a final `l` read as `I` (1) | WARN / REPLACE |
| | `digits_in_words` | `i1n`, `y1ears` | WARN at 20 and 1 per 10k |
| | `not_english`, `language_metadata` | stopword share under 20% in a book tagged English; 30%+ in a book tagged another language (the word checks then run anyway). A book with no language, or `und`, counts as English | WARN |
| Layout | `broken_lines` | paragraph or `<br>` breaks mid-sentence in prose-width lines (50+ characters); a break at a picture (a display equation) does not count | FIX at 3% |
| | `line_paragraphs` | every printed line is a paragraph: under 10 words a paragraph, half of them ending mid-sentence | REPLACE |
| | `hyphens` | `exam- ple` left from print line breaks; soft hyphens kept as `¬` or `?`; not `pre-empt` or `wah-wah` | FIX at 3 per 10k |
| | `headers` | a short line repeated 8+ times at page-like spacing with prose on both sides (not `Dear friend,`, a dateline, table cells or a countdown); page-number paragraphs | FIX |
| | `page_furniture` | the book's title beside a page number inside the text; browser print lines (`file:///`, `126 of 432`, dates) | WARN |
| | `drop_caps` | a chapter's first letter split off (`D ¶ espite`, `D rink`) | FIX at 3 |
| | `lost_paragraphs` | paragraphs of 400+ words with 3+ quoted turns; a line break counts as a paragraph break | WARN 10% · REPLACE 20% of the words |
| | `preformatted` | 50%+ of the words in `<pre>`-like blocks that never wrap: REPLACE for prose at print width, WARN for long verse lines | REPLACE / WARN |
| | `page_photos` | page-shaped pictures inside text chapters: 30+, 0.2+ per page, 10+ of them mid-sentence (a photo of each printed page beside its OCR text) | REPLACE |
| | `lost_text` | paragraphs that start or end in a broken word (`gifted ar` ¶ `antly`); not glossaries, links, shell lines, index entries, lists of names, verse or drop caps | WARN 0.5% · REPLACE 2% |
| | `bold_text`, `old_etext`, `no_italics`, `blockquote_text` | 50%+ of the text bold; a novel with almost no italics plus two smaller signs of an old e-text (WARN), or alone (INFO); 90%+ in blockquotes (INFO) | WARN / INFO |
| Duplicates | `duplicate_text` | any run of 40+ words that appears twice; not lists, back matter, a teaser page, a short quoted passage, a refrain, or a recap in an omnibus (those are `repeat_by_design`, INFO) | WARN 150 · REPLACE 300 words |
| | `duplicate_page` | PDF/DjVu: two pages whose text matches both ways, or look-alike page images; covers skipped; a copy in the first 8% (a preface's tour) is INFO | REPLACE within 3 pages · WARN farther |
| Gaps | `chapter_gap` | a chapter number that skips in a clean sequence of headings, with no heading of the missing number in any form (misspelt, run into the first sentence, small capitals, OCR-garbled), no chapter file between, and no sign that only a heading was lost. Not a misnumbered heading (8, 19, 10), or a jump past more chapters than the book has (12 → 101) | WARN |
| | `page_gap` | PDF/DjVu: printed page numbers jump forward (3 of 6 pages must agree); in a scan, a one-page jump or a jump at a chapter opening is skipped blank pages | REPLACE · WARN in scans |
| End | `sample`, `cut_off` | "end of sample" notices, not inside quotes (WARN in a book of 40,000+ words or in back matter, where a notice is most often about another book); a last paragraph of 25+ words that ends in lowercase or a comma (not back matter, an appendix list or a web address, and not when more story follows); an English story that stops inside a word the book never uses is REPLACE | REPLACE / WARN |
| PDF | `image_only`, `low_res`, `draft` | a scan with no text layer (full-colour pages are a comic: INFO); a scan under 150 ppi; an author's draft (`To be written`, unresolved `??` references) | WARN / INFO |
| Book | `unreadable_format`, `bad_extra_format`, `ocr_copy`, `formats_differ` | only LIT or similar; one bad format (with its reason); the OCR text of a scan beside it (INFO); word counts that differ by 15%+ | REPLACE…INFO |
| Library | `same_text`, `same_title`, `wrong_book`, `orphan_folder` | MinHash of the first 5,000 words (and of the middle, for a wrong book); title and author, with subtitles and volume numbers; folders that `metadata.db` does not list | INFO / WARN |

A PDF shows its drawn pages, so its text-layer numbers are INFO only. `<pre>` code blocks are
not checked. The Project Gutenberg header and licence are removed before any check. Books not
tagged English skip the checks that read English: the Text group except `watermark` and
`language_metadata`, and `line_paragraphs`, `hyphens`, `page_furniture`, `lost_text` and the
style checks. The other file, layout, duplicate, gap, page and end checks still run.

Back matter (notes, a glossary, a copyright page) skips the text and layout checks. It is
found by its file name or by a heading in its first two blocks. It can never be most of a
book: a file that holds over half of the words, or a group of marked files that does, counts
as the story.

### Metadata rules

Listed in a Metadata section at the top of the report and in each book's card. They are INFO
and never change a verdict. The values come from `metadata.db`.

| Check | Example | Fix in calibre |
|---|---|---|
| `meta_initials` | `George R. R. Martin` → `George R.R. Martin` | Manage authors: rename |
| `meta_last_first` | `Austen, Jane` → `Jane Austen` | Manage authors: rename |
| `meta_spelling` | `China Mieville` and `China Miéville` (accents win, else the spelling on most books) | Manage authors: rename to the other spelling (calibre merges them) |
| `meta_author_sort` | `Guin, Ursula K. Le` → `Le Guin, Ursula K.` | Edit metadata: the author sort button |
| `meta_unknown` | `Jane Doe & Unknown` | Edit metadata: remove "Unknown" |
| `meta_language` | no language | Edit metadata: Languages |
| `meta_isbn` | an ISBN with a wrong check digit | Edit metadata: Ids |
| `meta_no_cover` | no cover | Edit metadata: Download cover |
| `meta_stale_opf` | the book's `metadata.opf` differs from the database, so the book checks read old data | `calibredb backup_metadata` (with calibre closed) |

calibre's own author sort splits surname particles (`Guin, Ursula K. Le`); this rule keeps
them. To make calibre do the same, set the tweak `author_use_surname_prefixes = True`.

## Fixing what it finds

| Issue | In calibre |
|---|---|
| `malformed_xhtml`, `toc_broken`, `missing_files` | Edit book → Tools → Check book → Fix all |
| `bad_extra_format` | Book details → right-click the bad format → Delete |
| `unreadable_format` | Convert to EPUB, or get an EPUB |
| `dup_spine`, `duplicate_file` | Edit book: delete the copy and its `<itemref>` in the OPF spine |
| `watermark`, `headers`, `hyphens`, `mojibake`, `drop_caps` | Edit book → Search & replace (regex mode), or get a cleaner edition |
| `broken_lines`, `lost_paragraphs`, `lost_text`, REPLACE verdicts | Get another edition |

Do not trust calibre's Heuristic processing → **Unwrap lines** for `broken_lines`: it misses
`<br>`-wrapped prose and joins verse lines into one line per stanza. After a fix, run the
tool again. The changed file has a new hash, so it is checked again.

## LLM review (optional, paid)

The checks count what they can see. They cannot see an OCR error that makes a real word
(`modem` for *modern*), and they cannot always tell dialect from damage. With `--llm`, a
small model on [OpenRouter](https://openrouter.ai) reads pages as a reader sees them: four
stretches of about 3 pages at 12%, 37%, 62% and 87% of the main text (contents, copyright,
notes and the like left out), and the densest cluster of suspect words. Paragraph breaks,
line breaks and pictures (with their shape and place) are visible to it. It counts errors
and rates the book and each passage 1–5 on a fixed scale of errors per page.

| Rating | Effect |
|---|---|
| 1–2 | Adds a REPLACE (`llm_poor`); a WARN (`llm_local`) when only one passage is bad: a damaged chapter, or a document inside the story |
| 3 | Adds a WARN (`llm_fair`) |
| 4–5 | Moves each text-only issue (non-words, OCR swaps, glued and split words) one step down: REPLACE → WARN, WARN → INFO. `lost_paragraphs` moves down at 4–5, and at 3 when the reader did not report lost paragraphs. Structure, duplicate, gap and page-photo issues never change. |

- **Privacy:** the passages (about 14 pages a book) and the title are sent to OpenRouter and
  the model's provider. Do not use `--llm` on books you may not share.
- **Cost:** about $0.001–0.002 a book with the default model (Oct 2026 prices).
- **Key:** set `OPENROUTER_API_KEY` in the environment. Or put `OPENROUTER_API_KEY=...` in a
  private file (`chmod 600`) and give it with `--env-file FILE`. A file named `.env` in the
  current folder needs no option: in a git checkout, run `cp .env.example .env`.
- Answers are cached by passage text, model and prompt version, so a re-run costs nothing.

## How the limits were set

| Control group | Files | Result |
|---|---|---|
| Clean: Standard Ebooks and Project Gutenberg EPUBs | 39 | 0 REPLACE, 0 FIX; 2 WARN for dialect poetry (the LLM review clears these) |
| Raw OCR: Internet Archive scans | 4 | 0 REPLACE (good OCR: 0.3–0.9% non-words) |
| A real library, every flag checked by hand | ~750 books | Each false alarm became a rule fix and a regression test |

Then about 650 books of one library were read by hand (about 6 pages each) and given a
verdict. The limits were tuned on one half and measured on the other. Against the hand
verdicts: 1 of 45 hand-REPLACE books was rated KEEP (real-word OCR, which no rule here
sees), and 9 of 500 hand-KEEP books got a WARN (each a real but minor fault). No hand-KEEP
book got a FIX or a REPLACE.

These figures come from the code before the fixes in version 0.1.0 (see
[CHANGELOG.md](../CHANGELOG.md)). Those fixes can add a WARN or a REPLACE where a fault used to
stay hidden.

Measured, not guessed:

- Non-words in clean prose: 0.02–0.63%. Dialect poetry: 1.0–2.8%.
- `ebook-convert` adds no errors to normal files: 166 books with both an EPUB and a
  calibre-made MOBI gave the same metrics in 161–166 pairs, and the word count ratio was
  1.000–1.002. The exception is mis-encoded MOBIs (see `mis_encoded`).
- Structure checks (TOC, XHTML) are skipped for converted files, because calibre rebuilds
  them. So a converted MOBI can look cleaner than its EPUB.

## Limits of this tool

- English only.
- It cannot see an abridged edition, a bad translation, or most OCR errors that make real
  words (only a short list of pairs in context). The LLM review sees about 14 pages.
- It cannot find a partial book by its length: short stories and novellas are short too.
- `chapter_gap` is a WARN, because a missing heading is not always a missing chapter.
- A 150–300 word repeat is a WARN: it can be a duplicated page or a quotation.
- Missing italics alone is a note: many good copies of old books have none.
