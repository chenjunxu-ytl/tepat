# Tepat 2.0.0-preview.1

This preview is intended for evaluation. Spelling, terminology and grammar
signals require context review; factuality has no automated check.

## Windows app

1. Download `tepat-v2.0.0-preview.1-win64.zip` and extract the whole archive.
2. Open `tepat-v2/tepat-v2.exe`. Keep the `_internal` folder beside the executable.
3. Open http://127.0.0.1:8377 if the browser does not open automatically.
4. Enter Malay text, choose formal or informal register, and select **Semak**.

Allow approximately 2.5 GB of disk space for the extracted app. Its local evidence
database is approximately 2.42 GB before compression. The Windows executable is
unsigned. Close an older Tepat service using port 8377 before starting this one.
The app remains available in the system tray; use **Keluar** to exit.

## Chrome extension

1. Download and extract `tepat-v2.0.0-preview.1-chrome.zip`.
2. Open `chrome://extensions`, enable **Developer mode**, then **Load unpacked**.
3. Select the extracted `extension` folder that contains `manifest.json`.
4. Keep the Windows app running. Refresh a normal webpage, select Malay text,
   then use **Semak bahasa** in the context menu, or press **Alt+S**.

When updating, reload the extension and refresh pages that were already open.
Chrome's internal pages do not allow ordinary content scripts. Page scans process
the first 30,000 UTF-16 units; the rest of a longer page remains unscanned.

## Interpreting results

Red marks show matches to narrow, high-confidence manual patterns. Amber marks
request review. Blue marks identify uncertain forms. Broad grammar reminders and
sentence usage-density clues appear in the results panel without inline marks.
The panel describes each signal and offers local evidence, PRPM or a source search.

Absence from a dictionary or corpus does not establish a spelling error. An
attested word or phrase does not certify grammar, meaning or factuality. Suggested
replacements need human review. The preview does not apply them automatically.

Scans use the local service. **PRPM** sends the requested word to DBP when clicked;
**Cari sumber** opens a Google query when clicked. Failed online lookups remain
unverified.

## Updating local policies

`rules.json` and `indo_words.json` beside the executable override bundled copies.
Restart the app after editing. Preserve your own policy files before extracting a
future release over them. The old sentence-derived `blacklist.json` is no longer
used for detection.

`SHA256SUMS` records the release asset hashes. The corpus-tools archive contains
the cleaning code and maintained Indonesian policy sources; raw corpora and
private evaluation reports are excluded from that archive.
