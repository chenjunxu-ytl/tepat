# Tepat v2 — source-aware Malay checker

Current release: **2.0.0-preview.1**, for evaluation. Grammar coverage is limited
and contextual reminders still need human review. See [release notes](RELEASE_NOTES.md)
and the [portable installation guide](INSTALL.md).

Local Python service + Chrome extension. Highlights investigateable spelling,
terminology/register and grammar signals, with source evidence. Manual rules run
independently of dictionary and corpus matches. No automatic replacement.

## Run

From this directory:

```powershell
python server.py
```

PRPM is part of the Tepat core and starts even when the optional grammar evidence pack is not installed. When grammar data is present, the existing grammar/evidence checker is loaded as an additional capability.

Open http://127.0.0.1:8377. For Chrome, reload `extension/` as an unpacked extension,
then refresh the target page. Select text and use **Semak bahasa**, or use Alt+S.
The extension can scan the first 30,000 UTF-16 units of page text, with exact
occurrence highlights. The standalone page supports formal/informal register.
The service must be running; if an old Tepat owns port 8377, close it first.

## Evidence build

```powershell
python sync_indo.py
python build_evidence.py --review-root ../puzzle/corpus/clean-review/20261002-v3
```

Builds `data/evidence.sqlite` atomically through `evidence.building.sqlite`.
Incomplete builds cannot be loaded. A failed build leaves staging for inspection;
choose a new output or inspect/remove only that staging file before retrying.
Raw data, legacy `puzzle/ngrams2.db`, `words.txt` and `bigrams.txt.xz` are preserved.

- DBP: 26,915 identity-supported headword candidates, dictionary definitions as
  reference, 1,029 candidate example records.
- TD: 865 positive example records; negative/starred examples never enter counts.
  Explanation references can be searched locally.
- Bench: only 39 approved nonempty training correction responses. Original
  responses and holdout never enter counts. A response can contain many sentences.
- Wiki: 2,324,200 structurally cleaned sentence candidates; language/grammar are
  unverified. Supplemental evidence remains distinct from DBP/TD/Bench.
- Hansard: retained by the cleaning pipeline for reference; excluded from this
  runtime evidence index.

The builder stores unigram, bigram and trigram occurrence/document counts,
including single occurrences and function words, and an example with provenance.
Exact repeated sentences are deduplicated within a source. N-grams never cross
sentence, punctuation, unsupported-script, number or document boundaries.
Dictionary attestation, corpus observation and unknown forms are distinct states.
A known root is a clue requiring morphology review, never automatic proof that
an arbitrary derived form is grammatical.

## Rules and Indonesian candidates

`rules.json` stays editable. Each rule has a unique `entry_id`, a rule-book `id`,
category, register, regex, confidence, source and optional exception patterns.
Bad regex/shape entries are skipped and reported. Add `examples.trigger` and
`examples.clear` when extending a rule. Existing context-dependent regex matches
are reminders, not universal grammar verdicts. Adjacent repeated pemeri is the
remaining narrow high-confidence pattern.
Low-confidence manual reminders and sentence usage-density clues remain in the
results panel without inline highlights; actionable rules still mark exact spans.

`puzzle/indo_blacklist.md` and its JSON companion are the maintained source;
`sync_indo.py` checks agreement and produces `indo_words.json` for distribution.
Current policy: 293 Indonesian candidates, 56 uncertain forms, nine shared Malay
SMS abbreviations, five ambiguous chat/unit forms. Candidates retain the original
research status; they have not all been freshly normatively verified. Shared
abbreviations are classified using DBP's SMS guide:
https://eseminar.dbp.gov.my/dokumen/khidmatsms.pdf

Indonesian candidates run independently, even if they exist in Wiki or the old
wordlist. Two-letter/digit forms are supported. Formal SMS reminders differ from
Indonesian reminders; title case and punctuation protect `Dr.`. Suggested
counterparts require local DBP attestation and are never applied automatically.
The old automatically derived `blacklist.json` is preserved as a historical
artifact, but is not loaded by the checker or distributed in new builds. Its
generator marked all original bigrams when a correction changed the token count,
including combinations preserved verbatim in the corrected sentence. Even a
changed combination cannot establish a context-free grammar rule. Validated
patterns belong in the independently maintained rules, with trigger/clear examples.

## Checking scope

Every result reports spelling/terminology/grammar coverage and `factuality:
not_checked`. A low-frequency combination or absent dictionary entry is an
investigation clue. No result certifies the whole sentence as correct.

This version uses local contextual evidence and manual rules. It does not run a
semantic language model or an autonomous web research agent. **Bukti tempatan**
retrieves DBP definitions/examples and TD references. **PRPM** performs a manual
online lookup; **Cari sumber** opens a Google search when clicked. Network/layout
failures remain unverified. Explicit dictionary misses are not spelling verdicts.
Legacy PRPM cache entries are invalidated; new hits expire after 30 days and misses
after one day.

## Validation and package

```powershell
python -m unittest test_core test_evidence test_checker -v
node test_results.js
.\build.bat core
.\build.bat full
```

`build.bat core` produces the lightweight PRPM runtime without the grammar database. `build.bat full` preserves the existing PRPM + grammar package.

The portable app is `dist/tepat-v2/tepat-v2.exe`. Keep its entire folder together;
the database is intentionally outside the binary. External copies of rules and
word policy beside the exe override bundled copies; restart after editing.
Do not use the old `tepat.spec` for this version.

After committing both the checker and its sibling corpus-cleaning repository,
prepare the portable app, Chrome extension, corpus tools and checksums with:

```powershell
python prepare_release.py --mode core
python prepare_release.py --mode full
```

The output is under `releases/v2.0.0-preview.1/`. Core mode creates a lightweight
`*-prpm-win64.zip` asset without grammar data. Full mode keeps the existing
Windows, Chrome-extension and corpus-tool release assets and verifies the completed
evidence database. Preparation does not rebuild evidence or publish to GitHub.

Tests cover source counts, deduplication, prohibited negative/holdout ingestion,
function words, single occurrence support, independent rule/Indonesian matching,
short/digit forms, register/title contexts, Unicode offsets, span overlaps, API
failure states, and PRPM parsing/cache failures. They verify engineering behavior,
not comprehensive linguistic accuracy. Long-distance meaning, rare correct forms,
uncaught Indonesian forms, new terminology and factual claims remain limitations.
