# Tepat v2.0.0-preview.1

This pre-release provides a reproducible evaluation baseline for the local Malay
checker and Chrome extension. Grammar coverage and reminder noise still need work.

## Changes

- Source-separated unigram, bigram and trigram evidence from accepted DBP, TD,
  approved Bench correction responses and structurally cleaned Wiki candidates.
  Hansard remains excluded from positive evidence; Wiki remains unverified usage.
- Independent manual rules and Indonesian/register reminders. Dictionary
  attestation, corpus observation and unknown forms have distinct states.
- Exact occurrence highlights and UTF-16 offsets; broad context reminders stay in
  the panel without marking the sentence as an error.
- Retire all 329 legacy sentence-derived blacklist entries from detection. Phrases
  such as `lebih mudah`, `sebenarnya ialah` and `yang laju` no longer trigger alerts
  merely because they occurred inside a previously corrected sentence.
- Local evidence lookup, manual PRPM lookup and a manual Google source search.
  Online failures remain unverified; spelling corrections are never automatic.
- Portable Windows app, unpacked Chrome extension and source provenance/checksums.

## Validation and limits

The corpus cleaner passed 111 engineering tests. The checker passed 20 Python
tests plus JavaScript rendering, offset and overlap checks. The packaged service
and browser were checked with the three reported false alarms, repeated pemeri and
an Indonesian candidate. These checks establish implementation behavior.

The approved holdout contains 32 tasks. Only 12 annotated errors align as exact
substrings in their original responses; five other annotations remain unaligned.
Of the 12 aligned spans, three overlap a reminder, and only one overlaps a warning
or error. Broad reminders count toward the first figure. This is a small alert
coverage audit, with no claim of comprehensive grammar recall or final accuracy.

Thirteen heldout human-corrected responses receive between 0 and 12 signals each,
with no high-confidence error matches. Those signals have not been individually
adjudicated, so a false-positive rate is unavailable. Reminder noise remains a
practical limitation. The detailed evaluation report stays local.

Dictionary gaps, uncertain morphology, names, rare forms, language mixing,
long-distance grammar and context-sensitive terminology can still be missed or
flagged unnecessarily. Factuality is not checked. The Google action is a manual
search; the preview has no autonomous research or semantic grammar model.

## Installation

Use the Windows archive and keep its entire extracted folder together. The
database is approximately 2.42 GB uncompressed. Install the Chrome archive as an
unpacked extension and keep the local Windows service running. See `INSTALL.md`.

## Next evaluation priorities

Audit the remaining signals on corrected texts, and give actionable manual rules
paired trigger/clear examples with enough surrounding context. Use the missed
holdout errors and TD's explicit negative/positive examples to compare a contextual
grammar stage against the current baseline. Keep evaluation data outside positive
n-gram evidence. Frequency and isolated changed bigrams cannot determine grammar.
