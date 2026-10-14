# Tepat Admin Guide — managing flags, rules and word lists

This document is for the **admin** — the person (or machine role) responsible for
reviewing user feedback, maintaining grammar rules and word lists, and keeping
the deployment healthy. Every section explains *what you do in the UI*, *what
happens behind the scenes*, and *where the data lives*.

---

## 1. Architecture: why things are where they are

Tepat is a two-part system:

```
┌──────────────┐         ┌─────────────────────┐         ┌──────────────┐
│  Extension   │ ──────► │  Local server       │ ──────► │  GitHub repo │
│  (browser)   │  HTTP   │  (python server.py) │  API    │  (the single │
│              │ 127.0.0 │  :8377              │         │  source of   │
└──────────────┘  :8377  └──────────┬──────────┘         │  truth)      │
                                   │                    └──────────────┘
                          ┌────────┴────────┐
                          ▼                 ▼
                    DBP PRPM         LLM (z.ai, glm)
                    dictionary       rule translation &
                    (rate-limited,   sentence judging
                     sqlite-cached)
```

**The GitHub repo is the distribution channel and source of truth** for four
data files (the "sync set"):

| File | What it holds | Who writes it |
|---|---|---|
| `rules.json` | All grammar rules (RB-001…) | Admin, via the web UI |
| `indo_words.json` | Word lists (Indonesian-only, casual) | Admin, via the web UI |
| `word-overrides.json` | Word-level verdicts from flag reviews | Admin, via the web UI |
| `blacklist.json` | Legacy corpus exclusion list | (historical) |

Every admin decision (accept a rule, add a word, accept a flag) is written to
the **local copy** in `%APPDATA%\tepat\` and immediately **pushed to GitHub**
via the Contents API. All other machines pull these files every 5 minutes
(auto-sync) and hot-reload them — no restarts, no redeployment.

If a push fails (missing token, no permission), the local change is kept and
the UI shows a ⚠ warning — the change will reach GitHub only when pushed. The
sync layer protects unpushed local changes from being overwritten by the
5-minute fetch (content-hash baseline, see §8).

### What makes a machine "admin"

The server decides its role **at startup** by comparing the SHA-256 of
`TEPAT_ADMIN_TOKEN` in `.env` against a hash compiled into the server code. A
machine with the correct token is the *admin machine*; everyone else is a
*user machine*:

| Capability | Admin machine | User machine |
|---|---|---|
| Scan / PRPM / deep check | ✓ | ✓ |
| Flag (report problems) | ✓ | ✓ |
| See & edit `.env` | full editor | editor except admin token |
| Review flags (accept/reject) | ✓ | ✗ |
| Translate / enhance / edit rules | ✓ | ✗ |
| Add words to lists | ✓ | ✗ |
| Review console tabs | ✓ (visible) | hidden |

The web UI at `http://127.0.0.1:8377` adapts automatically — admin tabs appear
only on the admin machine.

---

## 2. The three feedback channels (flags) and how to review them

Users report problems from three places; all land as **GitHub issues** on the
repo with the label `word-flag` and a structured title `[flag:scope:kind] target`:

| Scope | Where users trigger it | Title looks like | What it means |
|---|---|---|---|
| `prpm` (word) | Right-click a word → Flag (Word) | `[flag:prpm:underflag] dokter` | A word-level complaint |
| `grammar` | Wrong-flag icon on a hit card, or right-click a phrase | `[flag:grammar:overflag] RB-001` | A rule hit the user disagrees with |
| `general` | Popup ⚑ button | `[flag:general:other] page` | Page-level subjective report |

Kinds: `underflag` (missed a problem), `overflag` (false alarm),
`mismeaning` (wrong suggestion), `other`.

**Duplicate handling**: if the same target (same title) already has an *open*
issue, a new report is appended as a **comment** on that issue instead of
opening a new one — multiple annotators' reasons gather under one issue until
it is closed.

### Reviewing — the three tabs in the web UI

Open `http://127.0.0.1:8377`, click **Word flags / Grammar flags / General
flags** (admin machine only). Each tab shows open flags as decision cards with
kind filter pills, a Closed toggle, and a reload button.

#### Word flags — the full decision flow

Each open flag card shows the word, the reporter's kind, PRPM's own verdict at
report time, and a prefilled definition textarea.

- **Accept** → the reporter's kind decides the word's final status:
  - `underflag` (user says it's a real word) → status `hit`
  - `overflag`/`mismeaning` (user says it's not) → status `miss`

  You can (and should) edit the definition text first — it is what users see
  when the word is looked up. Behind the scenes, the server:
  1. writes the entry into `word-overrides.json`
     (`{word: {status, definition, flag_kind, source_issue, decided_at}}`),
  2. pushes the file to GitHub,
  3. closes the issue with a comment recording the decision.

- **Reject** → no override is written; the issue is closed with a comment.

**How an override affects lookups** (`prpm_lookup`, in priority order):

```
1. word-overrides.json hit?  → use it, never touch PRPM     ← admin truth
2. function word (dan, itu…)? → short-circuit hit
3. sqlite cache hit?          → use cached verdict
4. ask prpm.dbp.gov.my        → cache result (hit/miss/warn)
5. miss → root retry (strip prefixes/suffixes, re-query root)
```

So an accepted override beats PRPM itself — that's the point: PRPM is slow,
sometimes wrong, and cannot be changed; overrides are instant, reviewed by a
human, and sync to every machine.

#### Grammar flags — connecting feedback to rules

A grammar flag raised from a **hit card** carries the rule id (e.g. `RB-001`).
The review card shows a rule badge and two actions:

- **Enhance this rule** — jumps to Server Rules, opens that rule's detail view
  and pops the correction dialog (see §4.2). The normal fix path.
- **Reject — close on GitHub** — for reports you disagree with; closes the
  issue (admin-only endpoint, restricted to this repo's issue URLs).

#### General flags

Page-level subjective reports — no structured action; handle on GitHub
(discuss, close). Shown read-only in the console for completeness.

---

## 3. Grammar rules — the full lifecycle

Rules live in `rules.json` as self-contained entries. Two execution engines
(the `plugin` field):

| plugin | Form | Runs when |
|---|---|---|
| `regex_match` | `params.pattern` + optional `exceptions` + `suggestion` | every scan — pure regex speed |
| `llm_judge` | `params.judge` (plain-language criterion) + optional `params.trigger` (regex pre-filter) | see below |

### llm_judge rules — the trigger split

LLM-judged rules are for what regex cannot express (word order, sentence
structure, animacy). To keep scans cheap, the `trigger` field decides when the
LLM is consulted:

- **With `trigger`** (e.g. `\bkebiasaannya\b`): runs in **every scan**, but
  only sentences matching the trigger regex are sent to the LLM. Everything
  else runs at regex speed. *Example: RB-002 flags "kebiasaannya after a
  verb" — sentences without the word never pay for an LLM call.*
- **Without `trigger`** (**deep-only**): skipped in normal scans entirely;
  judged sentence-by-sentence only when the user clicks **Deep check**.
  Reserved for sentence-structure rules that cannot name a pre-filter
  keyword. *Example: RB-010 (verb series mixing bare nouns).*

The judge itself is conservative by design: it answers `trigger` / `clear` /
`unsure`, and **unsure never flags** — a miss is cheaper than a false alarm.
Verdicts are cached per (criterion-hash, sentence), so re-scans are free.

### 3.1 Creating a rule — Rule Book playground

**UI**: Rule Book tab → describe the language point in plain words (wrong
sentences, right sentences, why) → **✦ Make a rule from this**.

**What happens** (one LLM call, then verification):

1. The description is enriched with PRPM definitions of its keywords.
2. The translation LLM produces a complete spec: description, user-facing
   Malay note, confidence, plugin choice, params (pattern **or**
   judge + trigger), impl (plain-English account of how it works), and
   **example sentences** (trigger/clear) extracted from your description.
3. **Self-test** runs against your examples *before anything is saved*:
   - regex rules: every trigger sentence must match, no clear sentence may;
   - llm_judge rules: the real judge function must classify every example
     correctly (trigger examples → trigger, clear examples → not trigger),
     and the trigger pre-filter must match its own trigger examples
     (otherwise the rule would sleep forever).
4. The rule lands as a **trial** with conf downgraded one level
   (high→medium…) — the original level is remembered in `conf_trial`.
5. The file is written, the checker hot-reloads, and the rule pushes to
   GitHub. It is **live immediately** (at trial level) on every machine.

The examples are not decoration — they are the rule's **behaviour spec and
regression suite**. Every future correction adds examples; they are never
removed.

### 3.2 Reviewing and editing — Server Rules tab

The table lists every rule (filter by level; trial rows are highlighted).
Toggle the **On** switch to mute a rule locally (GitHub sync restores it).
**Open** a rule for the detail view: Natural language / Rule code tabs,
the impl field ("How it works"), examples, and the action buttons.

#### Accept (trial → settled)

Restores the original conf level and marks the rule `settled`. Use it once
you've seen the rule behave in real scans. *18 rules are currently awaiting
review — see the checklist in §7.*

#### Enhance… — correct a rule with words

Describe what's wrong ("it also flags X but that's fine", "it misses Y").
The LLM re-translates the rule; **your sentences are merged into the example
set (never removed)**, the self-test re-runs, and the new version replaces the
trial (same id). Each correction permanently becomes a regression test.

*Example from practice*: RB-018 (antara two-items rule) initially flagged
idiomatic uses like "perbezaan antara bandar dan kampung". One enhance with
those sentences fixed the criterion and left the idioms as permanent clear-side
examples.

#### Edit code… — direct surgical fixes

For when you know the fix yourself: edit the plugin, the params JSON
(pattern/exceptions or judge/trigger), and the impl description. The
self-test runs before saving; failures block the save unless you explicitly
confirm "save anyway" (use for work-in-progress). Available on settled rules
too — this is the path when enhance's re-translation is too blunt.

#### Update… — non-behaviour fields

Change conf (level), desc (reviewer description), note (user-facing text), or
impl. Behaviour (pattern/examples) must go through Enhance/Edit code — the
separation keeps behaviour changes tied to the example suite.

### 3.3 The self-test — why it's the safety net

Every write path (translate, enhance, edit-code) runs the rule against its
example sentences first. Failures are shown verbatim. This is what makes the
maintenance loop safe: **you cannot save a rule that contradicts its own
examples** without an explicit override. One caveat learned in practice: a
passing self-test proves the examples pass — real-world sentences may still
expose gaps, and those become new examples through enhance.

---

## 4. Word lists — adding vocabulary

**UI**: Word Lists tab → choose the list → type word + (optional) standard
Malay equivalent → Add.

Two lists:

- **Indonesian-only** (`indo_only`): words standard in Bahasa Indonesia but
  not DBP-standard Malay — flagged with the standard equivalent where one
  exists (*dokter → doktor*).
- **Casual Malay** (`casual`): informal/spoken forms — flagged in formal
  register (*nak, tak*).

**What happens**: the word is written into `indo_words.json` (sorted, with the
`ms` mapping), the checker hot-reloads, and the file pushes to GitHub — every
machine flags the word within 5 minutes. Duplicate words in either list are
rejected with a hint.

**The Overrides sub-view** shows `word-overrides.json` — the flag-review
verdicts from §2. This is read/edit via the flag flow, not here.

**Word list vs override — when to use which**: word lists are for *vocabulary
that is systematically informal/non-standard* (a class of words). Overrides
are for *individual word verdicts* (this word exists / doesn't, with a
definition) — usually the outcome of a flag review. If you find yourself
adding many overrides for the same phenomenon, that's a word-list change.

---

## 5. Configuration — the Env tab

The `.env` file (`%APPDATA%\tepat\.env`) configures each machine. The Env tab
edits it (users get the same editor minus the admin token); changes apply
after restart.

| Key | Purpose | Default |
|---|---|---|
| `TEPAT_ADMIN_TOKEN` | Makes this machine admin (**locked for users**) | — |
| `TEPAT_GH_TOKEN` | GitHub PAT (Contents read+write) for pushes | falls back to `gh_token` file |
| `TEPAT_GH_REPO` | repo for rules/proposals | `chenjunxu-ytl/tepat` |
| `TEPAT_GH_BRANCH` | branch | `main` |
| `TEPAT_AUTO_SYNC` | `0` disables the 5-min auto-sync | `1` |
| `TEPAT_LLM_URL` / `_KEY` / `_MODEL` | LLM for translate/judge | z.ai endpoint, glm-5.2 |

**Without an LLM key**, everything works except llm_judge rules and rule
translation — the deep check shows one explanatory sentence instead of silently
missing a layer.

---

## 6. Updates and operations

- **Server self-update** (source/bat deployments): the popup's pulsing update
  button (or Env tab → "Update now") downloads the latest source zip, replaces
  the running code, and restarts via start.bat's loop — manual trigger,
  automatic process. Exe builds must be replaced manually.
- **Extension**: cannot be updated by the server (browser security); download
  the chrome zip from the release and reload at `chrome://extensions`.
- **Console visibility**: "Tunjuk log" in the tray menu restores the hidden
  console window (the tray icon is the resident entry point).

---

## 7. Daily/weekly rhythm (recommended)

1. **Triage flags** (Word/Grammar tabs): new open issues → accept/reject.
   Grammar flags with a rule badge take one click to the fix.
2. **Review trials** (Server Rules): accept rules that have behaved, enhance
   the ones that haven't. Current backlog: RB-003…RB-020.
3. **Watch for push warnings** (⚠ in any message bar): a failed push means the
   change is local-only until pushed — fix the token or retry.
4. **After changing `TEPAT_LLM_MODEL`**: llm_judge verdicts were made by the
   old model; re-validate the example suites before trusting old caches
   (restart clears the in-memory verdict cache).

---

## 8. Appendix — the sync safety net (why local work is never lost)

Every machine keeps a content-hash baseline (`sync-baseline.json`) of the last
state confirmed identical to GitHub. The 5-minute fetch **skips** any file
whose local hash differs from the baseline — i.e. any file with unpushed local
changes. This prevents the classic failure where a fetch silently overwrites
an unsynced admin decision. Baselines refresh only on confirmed pushes or
overwrites.

Hot-reload: every write path calls `checker.reload()` after saving, so rules
and word lists apply to the very next scan — no restarts anywhere.
