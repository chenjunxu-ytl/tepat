# Tepat — Malay language checker

Local service + Chrome extension that checks Malay text against DBP-standard
rules, word lists and the PRPM dictionary, with an optional LLM layer for the
judgements regex cannot make.

Current release: **v0.4.3** — see [release notes](RELEASE_NOTES.md),
the [install guide](INSTALL.md) and the
[admin guide](ADMIN_GUIDE.md).

## 🎬 Watch first

**Product overview** — what Tepat is and how it fits into the annotation workflow:

assets/videos/tepat-promo.mp4

**User guide** — every feature, step by step: check panel, highlights, deep check, flags:

assets/videos/Tepat-User-Guide.mp4

## What it does

- **Word-level checks** — DBP PRPM dictionary verification (rate-limited,
  sqlite-cached), Indonesian-only vocabulary, casual/informal words, with
  admin-reviewed overrides that beat the dictionary.
- **Grammar rules** — the settled Rule Book, live as rules.json: comparative
  *dari/daripada*, time prepositions, doubled copulas, English calques,
  collocations, spelling, *antara* coordination, dangling modifiers and more.
- **Agent rules (llm_judge)** — language points regex cannot express (word
  order, sentence structure, animacy) run as LLM-judged rules. Each carries a
  regex pre-filter so only sentences with the rule's keyword pay for an LLM
  call; structure rules without a pre-filter run only in the manual deep check.
  *Unsure never flags* — a miss is cheaper than a false alarm.
- **Feedback loop** — every hit carries a flag button; reports land as GitHub
  issues tagged with the rule id, and the review console jumps straight to the
  fix. Corrections permanently become regression examples.
- **Rule creation in plain words** — describe a language point in the Rule
  Book playground; the built-in translation agent produces a rule, self-tests
  it against your examples, and ships it as a trial for review.

Nothing is auto-replaced. Facts are never checked. Every finding needs human
judgement.

## Install

Two paths, same result ([full guide](INSTALL.md)):

- **All-in-one** — download
  [`tepat-v0.4.3-all-in-one.zip`](https://github.com/chenjunxu-ytl/tepat/releases/download/v0.4.3/tepat-v0.4.3-all-in-one.zip),
  run `tepat/tepat-v2/tepat-v2.exe`, load `tepat/extension/` as an unpacked
  extension.
- **Source (no exe)** — download
  [`tepat-v0.4.3-source.zip`](https://github.com/chenjunxu-ytl/tepat/releases/download/v0.4.3/tepat-v0.4.3-source.zip),
  install Python 3.10+, double-click `start.bat`. Nothing to pip install.

Then open http://127.0.0.1:8377, or just select text on any page and
right-click → **Tepat**. The tray icon is the resident entry point
("Tunjuk log" brings the console back).

## How checking works

```
extension (right-click / panel)
        │  HTTP 127.0.0.1:8377
        ▼
local server ──── rules.json ──► regex rules          ← every scan, instant
        │              │
        │              └────────► llm_judge rules     ← pre-filtered (trigger)
        │                                              or deep-only (manual)
        ├─── indo_words.json ─► word lists
        ├─── word-overrides.json ─► admin verdicts (beat PRPM)
        ├─── PRPM (prpm.dbp.gov.my, cached + rate-limited)
        └─── LLM (rule translation & judging; optional per machine)
```

Rules and word lists live in `%APPDATA%\tepat` and sync through this repo:
admin changes push here, every machine pulls within 5 minutes and hot-reloads.
Unpushed local changes are never overwritten by a fetch (content-hash
baseline). See the [admin guide](ADMIN_GUIDE.md) for the full review and
maintenance workflow.

## Repository layout

| Path | What it is |
|---|---|
| `server.py`, `checker.py`, `text_units.py`, `evidence.py` | the local service |
| `extension/` | the Chrome extension |
| `web/index.html` | the dashboard (scan, deep check, review console, env) |
| `rules.json`, `indo_words.json`, `word-overrides.json` | the synced data files |
| `tools/prepare_release.py` | builds release archives |
| `build.bat` / `start.bat` / `create-shortcut.bat` | run helpers |
| `ADMIN_GUIDE.md` / `INSTALL.md` / `RELEASE_NOTES.md` | docs |

## Development

```powershell
python -m unittest discover -s tests -p "test_*.py" -q   # unit tests
.\build.bat                                               # portable exe build
python tools/prepare_release.py                           # release archives
```

The server self-updates on source deployments: the popup's update button
downloads the latest release, replaces the running code and restarts through
`start.bat`'s loop. The extension is updated by reloading the new chrome zip
(browsers do not allow pages to reload extensions).

## Security notes

- The extension talks only to `http://127.0.0.1:8377`; no data leaves the
  machine except DBP dictionary queries and (when configured) LLM calls.
- CORS is limited to the extension's origin — web pages you visit cannot
  reach the local API or read configuration.
- Secrets (GitHub PAT, LLM key, admin token) live in `%APPDATA%\tepat\.env`
  only, are masked in the dashboard, and are never committed.
- The admin token decides which machine may review flags and edit rules; it
  is compared as a hash, never stored in the clear in code.

## Limitations

Grammar coverage is the settled Rule Book plus agent rules under review;
long-distance meaning, rare correct forms, new terminology and factual claims
remain out of scope. Every result needs human judgement.
