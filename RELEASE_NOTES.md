# Tepat v0.4.2

Malay word/grammar checker: local Windows runtime + browser extension.
Rules and word lists live in %APPDATA%/tepat and sync through this repo;
PRPM is the online dictionary layer.

## Highlights in 0.4.2

- **Console fully hidden**: starting from bat/shortcut leaves nothing in
  the taskbar — the tray icon is the only entry point, and its menu
  gains "Tunjuk log" to bring the console back when needed.
- **Env tab prefill**: fresh machines open the Env editor with sensible
  defaults already filled in (repo, branch, auto-sync, LLM endpoint and
  model — glm-5.2); only the secrets (GitHub PAT, LLM key) are blank.

## Highlights in 0.4.1

- **One-click self-update**: the update button now updates the server
  itself — download the new source, replace the files, restart
  (start.bat relaunches automatically). Manual trigger, automatic
  process; exe builds fall back to the release page.
- **Env tab for everyone**: users can configure their own machine's
  keys (LLM, GitHub, auto-sync) with the same editor as the admin;
  only the admin token stays locked away.
- **Desktop shortcut**: run `create-shortcut.bat` once to get a Tepat
  icon on the desktop that starts the server minimized.
- Fixed the update button lighting up on fresh 0.4.0 installs
  (missing manifest in the exe, plus a server-side fallback).

## Highlights in 0.4.0

- **Fifteen new rules from the settled Rule Book**: comparative
  dari/daripada, berbanding, beneficiary bagi, time prepositions,
  adalah+verb, mensetujui, memberikan+animate, parallel verb series,
  doubled copulas, plural+reduplication, English calques, collocations,
  spelling, lepas/lepaskan — translated by the server's own agent,
  each with example sentences and a self-test (33-sentence regression
  clean). Two of them run as agent rules (llm_judge) where regex cannot
  judge: parallel verb series and animate recipients.
- **Agent rules (llm_judge)**: language points regex cannot express
  (word order, sentence structure, animacy) run as LLM-judged rules —
  the criterion and its examples are human-reviewed rule data, unsure
  never flags.
- **Rule detail views** show how each rule actually works (impl field),
  and settled rules are editable: Enhance (natural-language correction),
  Edit code (direct plugin/params with example self-test).
- **Wrong-flag feedback loop**: every hit card carries a flag button;
  the report opens a GitHub issue titled with the rule id, and the
  review console jumps straight to the enhance dialog for that rule.
- **Update detection**: the popup pulses a download button when a new
  release exists (extension or server), and the Env tab shows both
  versions.
- **Panel redesign**: check panel and flag panel coexist, hit cards
  carry "Is X correct?" search icons, and clicking a card highlights
  the passage on the page.

## What's in this release

- **All-in-one download** (`tepat-v0.4.0-all-in-one.zip`): runtime and
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
