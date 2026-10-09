# Tepat 0.2.0-preview

This preview is intended for evaluation. Word and grammar checks run from the
bundled rule files plus PRPM lookups; flags always need context review, and
factuality has no automated check.

## Windows app

1. Download `tepat-v0.2.0-preview-win64.zip` and extract the whole archive.
2. Open `tepat-v2/tepat-v2.exe`. Keep the `_internal` folder beside the executable.
3. Open http://127.0.0.1:8377 if the browser does not open automatically.
4. Enter Malay text, choose formal or informal register, and select **Semak**.

The extracted app needs well under 100 MB. Checks read the rule files in
`%APPDATA%\tepat` (seeded from the bundled copies on first start) and the local
PRPM cache; word existence is verified against DBP's PRPM service on demand.
The Windows executable is unsigned. Close an older Tepat service using port
8377 before starting this one. The app remains available in the system tray;
use **Keluar** to exit.

## Chrome extension

1. Download and extract `tepat-v0.2.0-preview-chrome.zip`.
2. Open `chrome://extensions`, enable **Developer mode**, then **Load unpacked**.
3. Select the extracted `extension` folder that contains `manifest.json`.
4. Keep the Windows app running. Refresh a normal webpage, select Malay text,
   then use **Semak bahasa** in the context menu, or use the selection popup.

When updating, reload the extension and refresh pages that were already open.
Chrome's internal pages do not allow ordinary content scripts. Page scans process
the first 30,000 UTF-16 units; the rest of a longer page remains unscanned.

## Interpreting results

Red marks show matches to narrow, high-confidence rules. Amber marks request
review. Blue marks identify uncertain forms. The panel describes each signal and
offers PRPM or a source search.

Absence from a dictionary does not establish a spelling error. An attested word
or phrase does not certify grammar, meaning or factuality. Suggested replacements
need human review. The preview does not apply them automatically.

Scans use the local service. **PRPM** sends the requested word to DBP when
clicked; **Cari sumber** opens a Google query when clicked. Failed online lookups
remain unverified.

## Rules and review

New rules are born in the Rule Book tab: describe the language point in plain
words, the built-in LLM translation produces a trial rule, and you accept,
enhance or reject it on the Server Rules tab. Rules sync through the GitHub
repo, so every machine running Tepat stays current.

`rules.json` and `indo_words.json` in `%APPDATA%\tepat` are the live copies;
the files beside the executable only seed the first start. `SHA256SUMS` records
the release asset hashes. The corpus-tools archive contains the cleaning code
and maintained Indonesian policy sources; raw corpora and private evaluation
reports are excluded from that archive.
