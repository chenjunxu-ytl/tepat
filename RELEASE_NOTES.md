# Tepat v0.3.0

Malay word/grammar checker: local Windows runtime + browser extension.
Rules and word lists live in %APPDATA%/tepat and sync through this repo;
PRPM is the online dictionary layer.

## What's in this release

- **All-in-one download** (`tepat-v0.3.0-all-in-one.zip`): runtime and
  extension in one archive — unpack, run `tepat/tepat-v2/tepat-v2.exe`,
  load `tepat/extension/` as an unpacked extension. No separate downloads.
- **Rule Book pipeline**: describe a language point in plain words, the
  built-in LLM translation produces a trial rule (regex + examples +
  self-test), and you accept, enhance (natural-language correction) or
  reject it. Accepted rules sync to every machine through this repo.
- **Flag anything**: right-click any selection (word or phrase, any
  length) to flag it — Word / Grammar / General scopes, switchable in the
  panel. Decisions land in word-overrides.json and close the issue.
- **Rules + word lists + PRPM** is the whole checking surface; the
  multi-GB evidence corpus is retired. First launch fetches policy data
  from this repo automatically.
- Sync protection: locally-edited rules are never overwritten by a
  fetch until the pending push succeeds.
- SHA256 checksums in SHA256SUMS; factuality is not checked.
