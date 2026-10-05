#!/usr/bin/env python3
"""Tepat: source-aware Malay checker, manual rules and contextual usage evidence.

Uses data/evidence.sqlite built from the accepted cleaning run. Indonesian and
register reminders run independently. No automatic correction or factual verdict.
PRPM remains an on-demand dictionary lookup with hit/miss/unreachable states.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from checker import Checker
from evidence import EvidenceStore
from text_units import normalize
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_ROOT = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):  # PyInstaller 打包后的资源路径
    _ROOT = sys._MEIPASS  # noqa: SLF001

WEB_DIR = os.path.join(_ROOT, "web")


def _load_dotenv() -> None:
    """启动时读 exe/源码目录旁的 .env（KEY=VALUE 逐行），不覆盖已有环境变量。
    .env 在 .gitignore 里，绝不打包进 release。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, "frozen", False)
                        else os.path.dirname(sys.executable), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()

CACHE_PATH = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"),
    "tepat", "prpm_cache.sqlite") if os.name == "nt" else \
    os.path.join(os.path.expanduser("~"), ".tepat", "prpm_cache.sqlite")

# Admin 权限：TEPAT_ADMIN_TOKEN 环境变量优先，其次 %APPDATA%\tepat\admin_token。
# 有 token 才能 accept proposals / 直接改 Rule Book；普通用户只能提交申请。
ADMIN_TOKEN = os.environ.get("TEPAT_ADMIN_TOKEN") or ""


def _admin_token() -> str:
    if ADMIN_TOKEN:
        return ADMIN_TOKEN
    path = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        "tepat", "admin_token") if os.name == "nt" else \
        os.path.join(os.path.expanduser("~"), ".tepat", "admin_token")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _is_admin(req_headers, body: dict | None = None) -> bool:
    import hmac as _hmac
    expected = _admin_token()
    if not expected:
        return False
    given = str((body or {}).get("admin_token") or req_headers.get("X-Admin-Token") or "")
    return _hmac.compare_digest(expected, given)


def _proposals_path() -> str:
    base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        "tepat") if os.name == "nt" else \
        os.path.join(os.path.expanduser("~"), ".tepat")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "proposals.jsonl")


# GitHub 同步：proposal 上云用（Issue 形态，admin 在 GitHub 上阅读）。
# 建议给 fine-grained PAT，仅 tepat repo 的 Issues: Write；泄露只可能被刷 Issue。
GH_PROPOSAL_REPO = os.environ.get("TEPAT_GH_REPO", "chenjunxu-ytl/tepat")


def _gh_token() -> str:
    tok = os.environ.get("TEPAT_GH_TOKEN") or ""
    if tok:
        return tok
    path = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        "tepat", "gh_token") if os.name == "nt" else \
        os.path.join(os.path.expanduser("~"), ".tepat", "gh_token")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _gh_open_proposal_issue(rule: dict, regex_valid: bool) -> str:
    """把一条 proposal 开成 GitHub Issue，返回 html_url；失败抛 RuntimeError。"""
    import urllib.request as _rq
    token = _gh_token()
    if not token:
        raise RuntimeError("no GitHub token configured (%APPDATA%\\tepat\\gh_token)")
    rid = rule.get("id", "?")
    title = f"[proposal] {rid}: {(rule.get('note') or '')[:80]}"
    body = ("\n".join(f"**{k}:** {rule.get(k, '')}" for k in
                      ("id", "conf", "re", "note"))
            + f"\n\n**regex_valid:** {regex_valid}"
            + f"\n**submitted_at:** {int(time.time())}"
            + "\n\n_Auto-filed by Tepat web UI._")
    req = _rq.Request(
        f"https://api.github.com/repos/{GH_PROPOSAL_REPO}/issues",
        data=json.dumps({"title": title[:120], "body": body,
                         "labels": ["rule-proposal"]}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    import urllib.error as _ue
    with _rq.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read()).get("html_url", "")

# ── Accepted evidence database ──

_words: set[str] = set()

EVIDENCE_PATH = os.environ.get("TEPAT_EVIDENCE_DB", os.path.join(_ROOT, "data", "evidence.sqlite"))
_checker: Checker | None = None


def _config_path(name: str) -> str:
    if getattr(sys, "frozen", False):
        external = os.path.join(os.path.dirname(sys.executable), name)
        if os.path.isfile(external):
            return external
    return os.path.join(_ROOT, name)


def load_words() -> int:
    global _checker, _words
    store = EvidenceStore(EVIDENCE_PATH)
    _checker = Checker(store, _config_path("rules.json"), _config_path("indo_words.json"))
    _words = store.lexicon  # compatibility: health word count now counts DBP attestation
    print(f"[evidence] loaded {len(_words):,} dictionary forms; run={store.metadata['review_run']}")
    return len(_words)


def word_exists(w: str) -> bool:
    return bool(_checker and _checker.store.lexical(normalize(w))["state"] != "unknown")


def _pack_path() -> Path:
    return Path(_ROOT) / "extension" / "rules-pack.json"


def _load_pack() -> dict | None:
    try:
        return json.loads(_pack_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _next_rule_no(pack: dict | None) -> int:
    """auto-increment 规则号：现有 pack + pending proposals 里的 RB-NNNN 取最大 +1。
    proposals 也必须算——否则两次提交（未批准时）会撞号。"""
    biggest = 0
    for r in (pack or {}).get("rules", []):
        m = re.fullmatch(r"RB-(\d+)", str(r.get("id") or ""))
        if m:
            biggest = max(biggest, int(m.group(1)))
    try:
        with open(_proposals_path(), encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    m = re.fullmatch(r"RB-(\d+)", str(json.loads(line).get("id") or ""))
                except json.JSONDecodeError:
                    continue
                if m:
                    biggest = max(biggest, int(m.group(1)))
    except OSError:
        pass
    return biggest + 1


def _validate_pack(pack) -> list[str]:
    """rules-pack.json 落盘前的最小校验：扩展引擎要消费的结构必须完整。"""
    problems = []
    if not isinstance(pack, dict) or not isinstance(pack.get("meta"), dict):
        return ["pack must be an object with a meta object"]
    if not isinstance(pack.get("rules"), list):
        return ["pack.rules must be an array"]
    if not pack["rules"]:
        problems.append("pack.rules is empty")
    for idx, r in enumerate(pack["rules"]):
        if not isinstance(r, dict):
            problems.append(f"rule {idx}: object required")
            continue
        if not r.get("id"):
            problems.append(f"rule {idx}: empty id")
        if r.get("conf") not in ("error", "warn", "note", "exception"):
            problems.append(f"rule {idx} ({r.get('id') or '?'}): conf must be "
                            f"error/warn/note/exception, got {r.get('conf')!r}")
        pattern = r.get("re")
        if not pattern:
            problems.append(f"rule {idx} ({r.get('id') or '?'}): missing re")
        else:
            try:
                re.compile(pattern)  # 近似校验：JS 与 Python 正则大部分语法重合
            except re.error as e:
                problems.append(f"rule {idx} ({r.get('id') or '?'}): regex {str(pattern)[:40]!r}: {e}")
    return problems


def bigram_exists(a: str, b: str) -> bool:
    return bool(_checker and _checker.store.support(f"{normalize(a)} {normalize(b)}", 2))


def scan(text: str, register: str = "formal") -> dict:
    if _checker is None:
        raise RuntimeError("Evidence not loaded — call load_words() first")
    return _checker.scan(text, register)


# ── PRPM 查询（简化版：hit / miss / unreachable 三态 + sqlite 缓存 + 串行限流）──

_prpm_lock = threading.Lock()  # 全局串行：DBP crawl-delay 语义


def _cache_con() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    con = sqlite3.connect(CACHE_PATH, check_same_thread=False)
    con.execute("CREATE TABLE IF NOT EXISTS cache "
                "(word TEXT PRIMARY KEY, status TEXT, definition TEXT, "
                "fetched_at_ms INTEGER)")
    # 旧 schema 迁移（无 definition 列）
    cols = [r[1] for r in con.execute("PRAGMA table_info(cache)")]
    if "definition" not in cols:
        con.execute("ALTER TABLE cache ADD COLUMN definition TEXT DEFAULT ''")
        con.commit()
    if "parser_version" not in [r[1] for r in con.execute("PRAGMA table_info(cache)")]:
        con.execute("ALTER TABLE cache ADD COLUMN parser_version INTEGER DEFAULT 0")
        con.commit()
    return con


_cache_lock = threading.Lock()
_cache: sqlite3.Connection | None = None


def cache_get_full(word: str) -> dict | None:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = _cache_con()
        row = _cache.execute(
            "SELECT status, definition, fetched_at_ms, parser_version FROM cache WHERE word=?", (word,)).fetchone()
        if not row or row[3] != 2 or row[0] not in {"hit", "miss"}:
            return None
        ttl = 30 * 86400000 if row[0] == "hit" else 86400000
        if int(time.time()*1000) - row[2] > ttl:
            return None
        return {"status": row[0], "definition": row[1] or ""}



def cache_put(word: str, status: str, definition: str = "") -> None:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = _cache_con()
        _cache.execute("INSERT OR REPLACE INTO cache "
                       "(word, status, definition, fetched_at_ms, parser_version) "
                       "VALUES (?,?,?,?,2)",
                       (word, status, definition, int(time.time() * 1000)))
        _cache.commit()


class PrpmUnavailable(Exception):
    pass


def prpm_fetch(url: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "tepat/1.0 (word existence lookup)"})
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5)
    raise PrpmUnavailable(str(last))


def _prpm_definition(page: str) -> str:
    """从 PRPM Cari1 页面提取首条释义（Kamus Dewan/Pelajar 条目文本）。
    完整返回（保留合理上限 2000 字符），配合前端滚动条展示。"""
    m_td = re.search(r'class="tdclass"[^>]*>(.*?)</td>', page, re.DOTALL)
    if not m_td:
        return ""
    td_text = re.sub(r"<[^>]+>", " ", m_td.group(1))
    td_text = td_text.replace("&nbsp;", " ").replace("&amp;", "&")
    td_text = re.sub(r"\s+", " ", td_text).strip()
    if "Carian kata tiada di dalam kamus terkini" in td_text or "Tiada maklumat" in td_text:
        return ""
    if not td_text:
        return ""
    m_def = re.search(r"Definisi\s*:\s*(.*)", td_text)
    if m_def:
        return m_def.group(1).strip()[:2000]
    return td_text[:2000]


def prpm_lookup(word: str) -> dict:
    """三态 + 释义: {'status': 'hit'|'miss'|'unreachable', 'definition': str}"""
    cached = cache_get_full(word)
    if cached:
        # 防御旧缓存坏条目：若记录为 hit 且包含 Carian kata tiada，视为无效重新查询
        if cached["status"] == "hit" and "Carian kata tiada" in (cached.get("definition") or ""):
            cached = None
        else:
            return {"word": word, "status": cached["status"],
                    "definition": cached.get("definition") or "",
                    "from_cache": True}
    with _prpm_lock:
        url = "https://prpm.dbp.gov.my/Cari1?keyword=" + urllib.parse.quote(word)
        try:
            page = prpm_fetch(url)
        except PrpmUnavailable as e:
            return {"word": word, "status": "unreachable", "note": str(e)[:150]}
        
        parsed = parse_prpm(page)
        status = parsed["status"]
        definition = parsed.get("definition", "")
        if status in {"hit", "miss", "warn"}:
            cache_put(word, status, definition)
        time.sleep(1.0)
        return {"word": word, "url": url, **parsed}


def parse_prpm(page: str) -> dict:
    # Missing/challenge/changed dictionary markup is not a dictionary miss.
    match = re.search(r"class=[\"'](?:[^\"']*\s)?tdclass(?:\s[^\"']*)?[\"'][^>]*>(.*?)</td>", page, re.DOTALL | re.IGNORECASE)
    if not match:
        return {"status": "unreachable", "note": "Dictionary section could not be verified"}
    from html import unescape
    body = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", match[1]))).strip()
    if "Adakah anda bermaksud" in body:
        m_sug = re.search(r"(Adakah anda bermaksud\s*:?[^.\n]*)", body, re.IGNORECASE)
        sug_text = m_sug.group(1).strip() if m_sug else body[:200]
        return {"status": "warn", "definition": sug_text}
    if "Carian kata tiada di dalam kamus terkini" in body or "Tiada maklumat" in body:
        return {"status": "miss", "definition": ""}
    if "Definisi" not in body:
        return {"status": "unreachable", "note": "Dictionary definition could not be verified"}
    definition = re.split(r"Definisi\s*:", body, maxsplit=1)[-1].strip()
    if not definition:
        return {"status": "unreachable", "note": "Dictionary definition was empty"}
    return {"status": "hit", "definition": definition[:4000]}




# ── HTTP ──

class Handler(BaseHTTPRequestHandler):
    def finish(self):
        try:
            super().finish()
        finally:
            if _checker is not None:
                _checker.store.close_thread()

    def log_message(self, fmt, *args):  # 安静模式
        pass

    def _json(self, code: int, obj: dict):
        # Browser page truncation can leave a lone UTF-16 surrogate; escaping
        # preserves it safely rather than failing the whole scan response.
        body = json.dumps(obj, ensure_ascii=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: str, ctype: str):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
        elif self.path == "/results.js":
            self._file(os.path.join(_ROOT, "extension", "results.js"), "text/javascript; charset=utf-8")
        elif self.path == "/api/health":
            self._json(200, {"ok": _checker is not None, "engine": "evidence-v2", "words": len(_words),
                             "mode": "full" if _checker is not None else "core",
                             "review_run": _checker.store.metadata["review_run"] if _checker else None,
                             "source_counts": _checker.store.metadata["counts"] if _checker else {},
                             "config_warnings": _checker.config_warnings if _checker else [],
                             "tray": tray_icon is not None})
        elif self.path == "/api/config":
            # 每个 rule 植入的直观视图（用户裁决 2026-10-02）：配置文件原文
            # + 解析态 + 逐条规则视图，web UI 的 Config 区消费。
            entries = []
            for name in ("rules.json", "indo_words.json", "blacklist.json"):
                resolved = _config_path(name)
                entry = {"name": name, "path": str(resolved),
                         "exists": os.path.isfile(resolved)}
                if entry["exists"]:
                    st = os.stat(resolved)
                    entry["mtime"] = st.st_mtime
                    entry["bytes"] = st.st_size
                    entry["content"] = Path(resolved).read_text(encoding="utf-8", errors="replace")
                    try:
                        entry["parsed"] = json.loads(entry["content"])
                    except json.JSONDecodeError as e:
                        entry["parse_error"] = str(e)
                entries.append(entry)
            rule_views = []
            if entries[0].get("parsed"):
                for i, r in enumerate(entries[0]["parsed"].get("rules", [])):
                    ok, err = True, None
                    try:
                        re.compile(r.get("re", ""))
                    except re.error as e:
                        ok, err = False, str(e)
                    rule_views.append({"idx": i, "id": r.get("id"), "conf": r.get("conf"),
                                       "re": r.get("re"), "note": r.get("note"),
                                       "valid": ok, "error": err})
            # Rule Book 插件包（extension/rules-pack.json）：纯 JSON，无 JS 解析。
            pack_meta, pack_views = None, []
            pack = _load_pack()
            if pack:
                pack_meta = {**pack.get("meta", {}), "path": str(_pack_path()),
                             "entries": len(pack.get("rules", []))}
                for i, r in enumerate(pack.get("rules", [])):
                    ok, err = True, None
                    try:
                        re.compile(r.get("re", ""))
                    except re.error as e:
                        ok, err = False, str(e)
                    pack_views.append({"idx": i, "id": r.get("id"), "conf": r.get("conf"),
                                       "re": r.get("re"), "note": r.get("note"),
                                       "valid": ok, "error": err})
            self._json(200, {"files": entries, "rule_views": rule_views,
                             "pack": pack_meta, "pack_views": pack_views,
                             "loaded": _checker is not None,
                             "config_warnings": _checker.config_warnings if _checker else []})
        elif self.path == "/api/pack":
            # Rule Book 插件包（extension/rules-pack.json）：GET 返回解析后的
            # JSON + path。POST（见 do_POST）保存；扩展直接 fetch 此接口。
            pack = _load_pack()
            if pack is None:
                self._json(404, {"error": "rules-pack.json not found or broken"})
                return
            self._json(200, {"path": str(_pack_path()), "pack": pack})
        elif self.path == "/api/proposals":
            # Rule Book 申请列表：读取需要 admin（申请内容对普通用户互相不可见）。
            if not _is_admin(self.headers):
                self._json(403, {"error": "admin token required"})
                return
            proposals = []
            try:
                with open(_proposals_path(), encoding="utf-8") as f:
                    proposals = [json.loads(line) for line in f if line.strip()]
            except (OSError, json.JSONDecodeError):
                pass
            self._json(200, {"proposals": proposals,
                             "admin_configured": bool(_admin_token())})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > 512000:
                raise ValueError("request too large")
        except ValueError:
            self._json(413, {"error": "request too large"})
            return
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(req, dict):
                raise ValueError("object required")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._json(400, {"error": "bad json"})
            return
        if self.path == "/api/scan":
            text = str(req.get("text") or "")
            try:
                self._json(200, scan(text, str(req.get("register") or "formal")))
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except (RuntimeError, sqlite3.Error):
                self._json(503, {"error": "Evidence unavailable; scan was not completed"})
        elif self.path == "/api/prpm":
            raw_word = str(req.get("word") or "").strip().lower()
            word = re.sub(r"[\u00ad\u200b-\u200d\ufeff]", "", raw_word).strip("-'")
            if not word or re.search(r"\s", word):
                self._json(400, {"error": "single word required"})
                return
            self._json(200, prpm_lookup(word))
        elif self.path == "/api/check":  # 任意词手动查：词表 + PRPM 一步到位
            raw_word = str(req.get("word") or "").strip().lower()
            word = re.sub(r"[\u00ad\u200b-\u200d\ufeff]", "", raw_word).strip("-'")
            if not word or re.search(r"\s", word):
                self._json(400, {"error": "single word required"})
                return
            in_corpus = word_exists(word)
            out = {"word": word, "in_corpus": in_corpus,
                   "local_evidence": _checker.store.lookup(word) if _checker else None}
            if out["local_evidence"] is None or out["local_evidence"]["state"] != "dictionary_attested":  # observed usage still needs dictionary lookup
                out["prpm"] = prpm_lookup(word)
            self._json(200, out)
        elif self.path == "/api/evidence":
            query = str(req.get("query") or "").strip()[:240]
            if not query or _checker is None:
                self._json(400, {"error": "query required and evidence must be loaded"})
                return
            self._json(200, {"query": query, "lexical": _checker.store.lookup(query),
                             "references": _checker.store.search(query)})
        elif self.path == "/api/config/reload":
            # 热重载：编辑 rules.json/indo_words.json 后不重启 exe 即生效。
            if _checker is None:
                self._json(409, {"error": "grammar module not loaded; nothing to reload"})
                return
            try:
                _checker.reload()
                self._json(200, {"ok": True,
                                 "rules": len(_checker.rules),
                                 "config_warnings": _checker.config_warnings})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": f"reload failed: {e}"})
        elif self.path == "/api/pack":
            # 保存 Rule Book 插件包：admin 专属（普通用户请走 /api/proposals 申请）。
            # 写前备份 rules-pack.json.bak；JSON 校验失败不落盘。
            if not _is_admin(self.headers, req):
                self._json(403, {"error": "admin token required to edit the Rule Book pack; "
                                         "submit a proposal via /api/proposals instead"})
                return
            pack = req.get("pack")
            if not isinstance(pack, dict):
                self._json(400, {"error": "pack object required"})
                return
            problems = _validate_pack(pack)
            if problems:
                self._json(400, {"error": "pack validation failed", "problems": problems})
                return
            pack_path = _pack_path()
            try:
                if pack_path.exists():
                    pack_path.with_suffix(".json.bak").write_bytes(pack_path.read_bytes())
                pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=1) + "\n",
                                     encoding="utf-8")
            except OSError as e:
                self._json(500, {"error": f"write failed: {e}"})
                return
            count = len(pack.get("rules", []))
            _log(f"[pack] rules-pack.json updated ({count} rule entries)")
            self._json(200, {"ok": True, "backup": True, "entries": count,
                             "meta_id": pack.get("meta", {}).get("id", "?")})
        elif self.path == "/api/proposals":
            # 普通用户提交 Rule Book 申请：免 token。规则号服务端自动分配
            # （RB-NNNN auto-increment，用户不需要也不应该手填 ID）。
            rule = req.get("rule")
            if not isinstance(rule, dict):
                self._json(400, {"error": "rule object required"})
                return
            note = str(rule.get("note") or "").strip()[:300]
            pattern = str(rule.get("re") or "").strip()[:500]
            conf = str(rule.get("conf") or "warn").strip()[:16]
            if not pattern:
                self._json(400, {"error": "pattern (re) required"})
                return
            if conf not in ("error", "warn", "note", "exception"):
                self._json(400, {"error": "conf must be error/warn/note/exception"})
                return
            pack = _load_pack()
            rid = f"RB-{_next_rule_no(pack):03d}"
            ok, err = True, None
            try:
                re.compile(pattern)  # 近似校验，跟保存包同一口径
            except re.error as e:
                ok, err = False, str(e)
            entry = {"id": rid, "conf": conf, "re": pattern, "note": note,
                     "regex_valid": ok, "regex_error": err,
                     "submitted_at": int(time.time())}
            with open(_proposals_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # 云端同步：有 GitHub token 就把申请开成 Issue（admin 在 GitHub 阅读）；
            # 失败不影响本地存储，稍后可在 admin 机器补传。
            gh_url = ""
            gh_error = None
            try:
                gh_url = _gh_open_proposal_issue(entry, ok)
            except Exception as e:  # noqa: BLE001
                gh_error = str(e)[:200]
                _log(f"[proposals] GitHub issue failed: {gh_error}")
            _log(f"[proposals] new proposal {rid} from web UI")
            self._json(200, {"ok": True, "id": rid, "regex_valid": ok, "regex_error": err,
                             "github_issue": gh_url or None,
                             "github_error": gh_error})
        elif self.path == "/api/proposals/accept":
            # admin 批准申请：把规则追加进 rules-pack.json 的 rules 数组尾部。
            if not _is_admin(self.headers, req):
                self._json(403, {"error": "admin token required"})
                return
            submitted_at = req.get("submitted_at")
            proposals_path = Path(_proposals_path())
            proposals = []
            if proposals_path.is_file():
                with proposals_path.open(encoding="utf-8") as f:
                    proposals = [json.loads(line) for line in f if line.strip()]
            target = next((p for p in proposals
                           if str(p.get("submitted_at")) == str(submitted_at)), None)
            if target is None:
                self._json(404, {"error": "proposal not found"})
                return
            pack = _load_pack()
            if pack is None:
                self._json(404, {"error": "rules-pack.json not found or broken"})
                return
            rid = str(target["id"])
            pack.setdefault("rules", []).append({
                "id": rid, "conf": str(target["conf"]),
                "note": str(target.get("note") or ""), "re": str(target["re"])})
            problems = _validate_pack(pack)
            if problems:
                self._json(400, {"error": "merged pack failed validation", "problems": problems})
                return
            pack_path = _pack_path()
            pack_path.with_suffix(".json.bak").write_bytes(pack_path.read_bytes())
            pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=1) + "\n",
                                 encoding="utf-8")
            with proposals_path.open("w", encoding="utf-8") as f:  # 已处理：清空申请列表
                for p in proposals:
                    if p is not target:
                        f.write(json.dumps(p, ensure_ascii=False) + "\n")
            _log(f"[proposals] accepted {rid} -> rules-pack.json")
            self._json(200, {"ok": True, "accepted": rid})
        else:
            self._json(404, {"error": "not found"})


tray_icon = None  # main() 里赋值；/api/health 暴露给 UI 探测


def main() -> int:
    _log(f"[main] starting tepat (PID {os.getpid()})...")
    ap = argparse.ArgumentParser(description="tepat - pemeriksa kewujudan kata")
    ap.add_argument("--port", type=int, default=8377)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-tray", action="store_true",
                    help="skip the system tray icon (debug)")
    args = ap.parse_args()

    _log(f"[main] args: port={args.port}, no_browser={args.no_browser}, no_tray={args.no_tray}")

    import ctypes
    import socket as _socket

    def _port_in_use(port: int) -> bool:
        s = _socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    if os.name == "nt":
        ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\tepat-singleton")
    if _port_in_use(args.port):
        _log(f"[tepat] port {args.port} already serving — exiting WITHOUT opening UI")
        return 0

    # core 运行时没有 evidence.sqlite：保持 PRPM-only 服务（轻量版是一等资产）。
    # 语法数据是可后补的升级包，不是启动前提；放回 data/ 后重启即恢复完整检查。
    if os.path.exists(EVIDENCE_PATH):
        _log("[main] loading words and rules...")
        try:
            load_words()
        except Exception as e:  # noqa: BLE001
            _log(f"[main] evidence load failed ({e!r}) — serving PRPM-only")
    else:
        _log("[main] no evidence.sqlite — PRPM-only core mode; "
             "drop data/evidence.sqlite beside the runtime to enable the grammar module")

    class _ExclusiveServer(ThreadingHTTPServer):
        # Windows 默认允许同端口双重 bind（无 SO_EXCLUSIVEADDRUSE），
        # 两个 tepat 能"同时启动成功"——14:44/14:45 双实例即此因。
        allow_reuse_address = False

        def server_bind(self):
            import socket as _s
            self.socket.setsockopt(_s.SOL_SOCKET, _s.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    httpd = _ExclusiveServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"[tepat] serving on {url}")

    # 系统托盘图标：常驻右下角，证明在运行；菜单 = 打开 UI / 退出。
    # exe 挂了托盘跟着消失——图标本身就是心跳。
    global tray_icon
    if not args.no_tray:
        tray_icon = start_tray(url, args.port)

    # 启动不自动开浏览器（用户裁决 2026-10-02）：托盘在即可，UI 由用户
    # 右键托盘 → Buka UI 主动打开。--no-browser 参数保留但已无实际作用。
    if not args.no_browser and False:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    # 看门狗（2026-10-02）：托盘窗口死了就整体重启托盘线程。
    # 背景：14:25 实例的托盘线程静默死亡（服务照活、图标消失、无报警）。
    # 消息循环自愈管"循环内异常"，看门狗管"整个线程没了"。
    if not args.no_tray:
        def _tray_watchdog():
            dead_count = 0
            def _config_sig():
                sig = []
                for name in ("rules.json", "indo_words.json", "blacklist.json"):
                    try:
                        sig.append((name, os.stat(_config_path(name)).st_mtime))
                    except OSError:
                        sig.append((name, 0))
                return tuple(sig)
            last_cfg = _config_sig()
            while True:
                time.sleep(15)
                if stop_event.is_set():
                    return
                # hot config（用户裁决 2026-10-02）：改文件即生效，不用重启
                cfg = _config_sig()
                if cfg != last_cfg:
                    last_cfg = cfg
                    if _checker is not None:
                        try:
                            _checker.reload()
                            _log(f"[config] hot-reloaded rules ({len(_checker.rules)} rules, "
                                 f"{len(_checker.config_warnings)} warnings)")
                        except Exception as e:  # noqa: BLE001
                            _log(f"[config] hot-reload failed: {e!r}")
                if tray_icon is not None and not tray_icon.alive():
                    dead_count += 1
                    _log(f"[watchdog] tray window dead (x{dead_count}) — restarting tray")
                    tray_icon = start_tray(url, args.port)
                    if tray_icon is None:
                        _log("[watchdog] tray restart failed")
                else:
                    dead_count = 0
        stop_event = threading.Event()
        threading.Thread(target=_tray_watchdog, daemon=True, name="tray-watchdog").start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        if tray_icon is not None:
            tray_icon.stop()
    return 0


def start_tray(url: str, port: int = 8377):
    """右下角托盘图标：纯原生 Win32 Shell_NotifyIconW 实现，无 pystray/pillow 外部依赖。
    左键/右键弹出菜单：Buka UI / Keluar。双击直接打开 UI。"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        shell32 = ctypes.windll.shell32

        # 确保 DefWindowProcW 有正确的 64-bit 签名，防止 64 位指针/lparam 溢出导致崩溃
        LRESULT = ctypes.c_longlong
        user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        WM_TRAYCB = 0x7FFF
        NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
        NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
        WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203
        WM_DESTROY, WM_CLOSE = 0x0002, 0x0010

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD),
                        ("Data3", wt.WORD), ("Data4", wt.BYTE * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT),
                ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                ("szInfo", wt.WCHAR * 256), ("uTimeoutOrVersion", wt.UINT),
                ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", _GUID), ("hBalloonIcon", wt.HICON),
            ]

        shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wt.BOOL

        # 加载图标：优先使用 assets/icon.ico，无需 Pillow
        ico_path = os.path.join(_ROOT, "assets", "icon.ico")
        if not os.path.exists(ico_path):
            ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.ico")

        hinstance = kernel32.GetModuleHandleW(None)
        hicon = None
        if os.path.exists(ico_path):
            cx = user32.GetSystemMetrics(49) or 16  # SM_CXSMICON
            cy = user32.GetSystemMetrics(50) or 16  # SM_CYSMICON
            hicon = user32.LoadImageW(None, ico_path, 1, cx, cy, 0x10)

        if not hicon:
            hicon = user32.LoadIconW(hinstance, 1)

        if not hicon:
            hicon = user32.LoadIconW(None, 32512)  # IDI_APPLICATION

        _log(f"[tray] loaded icon hicon={hicon} from {ico_path}")

        class _TrayState:
            hwnd = None
            nid = None
            stop = False

        state = _TrayState()

        WM_APP_STOP = 0x8000 + 1
        WM_APP_MENU = 0x8000 + 2
        WM_APP_OPEN = 0x8000 + 3
        MENU_OPEN, MENU_EXIT = 100, 101

        _taskbar_created_msg = [user32.RegisterWindowMessageW("TaskbarCreated")]

        def _open_ui():
            import webbrowser
            webbrowser.open(url)

        def _popup_menu_async():
            if state.hwnd:
                user32.PostMessageW(state.hwnd, WM_APP_MENU, 0, 0)

        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == _taskbar_created_msg[0] and _taskbar_created_msg[0]:
                _log("[tray] TaskbarCreated received — re-adding icon")
                if state.nid:
                    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(state.nid))
                return 0
            if msg == WM_TRAYCB:
                if lparam == WM_LBUTTONDBLCLK:
                    user32.PostMessageW(hwnd, WM_APP_OPEN, 0, 0)
                elif lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
                    _popup_menu_async()
                return 0
            if msg == WM_APP_OPEN:
                threading.Thread(target=_open_ui, daemon=True).start()
                return 0
            if msg == WM_APP_MENU:
                menu = user32.CreatePopupMenu()
                user32.AppendMenuW(menu, 0, MENU_OPEN, "Buka UI")
                user32.AppendMenuW(menu, 0x800, 0, None)  # MF_SEPARATOR
                user32.AppendMenuW(menu, 0, MENU_EXIT, "Keluar")
                user32.SetForegroundWindow(hwnd)
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                cmd = user32.TrackPopupMenu(
                    menu, 0x100,
                    pt.x, pt.y, 0, hwnd, None)
                user32.PostMessageW(hwnd, 0, 0, 0)
                user32.DestroyMenu(menu)
                if cmd == MENU_OPEN:
                    threading.Thread(target=_open_ui, daemon=True).start()
                elif cmd == MENU_EXIT:
                    state.stop = True
                    if state.nid:
                        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(state.nid))
                    user32.DestroyWindow(hwnd)
                    os._exit(0)
                return 0
            if msg in (WM_CLOSE, WM_APP_STOP):
                state.stop = True
                if state.nid:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(state.nid))
                user32.DestroyWindow(hwnd)
                os._exit(0)
                return 0
            if msg == WM_DESTROY:
                if state.nid:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(state.nid))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        class _WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT),
                        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                        ("hIcon", wt.HICON), ("hCursor", ctypes.c_void_p),
                        ("hbrBackground", ctypes.c_void_p),
                        ("lpszMenuName", wt.LPCWSTR),
                        ("lpszClassName", wt.LPCWSTR),
                        ("hIconSm", wt.HICON)]

        _wnd_proc_ref = WNDPROC(wnd_proc)
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = _wnd_proc_ref
        wc.lpszClassName = "tepat-tray"
        wc.hInstance = hinstance
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            err = kernel32.GetLastError()
            if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                _log(f"[tray] RegisterClassExW warn err={err}")

        def _tray_thread():
            try:
                hdesk = user32.OpenDesktopW("default", 0, False, 0x01FF)
                if hdesk:
                    user32.SetThreadDesktop(hdesk)
            except Exception as e:
                _log(f"[tray] SetThreadDesktop note: {e!r}")

            user32.CreateWindowExW.restype = wt.HWND
            user32.CreateWindowExW.argtypes = [
                wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wt.HWND, ctypes.c_void_p, wt.HINSTANCE, ctypes.c_void_p]
            state.hwnd = user32.CreateWindowExW(
                0, wc.lpszClassName, "Tepat", 0x80000000, 0, 0, 0, 0,
                None, None, wc.hInstance, None)

            if not state.hwnd:
                _log(f"[tray] CreateWindowExW failed: {kernel32.GetLastError()}")
                return

            try:
                user32.ChangeWindowMessageFilterEx(state.hwnd, _taskbar_created_msg[0], 1, None)
            except Exception:
                pass

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = state.hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAYCB
            nid.hIcon = hicon
            nid.szTip = f"Tepat — berjalan (127.0.0.1:{port})"
            state.nid = nid

            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            if not ok:
                ok = shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            _log(f"[tray] Shell_NotifyIcon NIM_ADD ok={ok} hwnd={state.hwnd}")
            if not ok:
                _log(f"[tray] NIM_ADD failed: {ctypes.WinError()}")

            msg = wt.MSG()
            while True:
                rc = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if rc <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            _log("[tray] message loop exited")

        t = threading.Thread(target=_tray_thread, daemon=True, name="tray")
        t.start()
        _log("[tray] pure win32 tray thread started")

        class _TrayHandle:
            def stop(self):
                try:
                    state.stop = True
                    if state.hwnd:
                        user32.PostMessageW(state.hwnd, WM_APP_STOP, 0, 0)
                except Exception as e:
                    _log(f"[tray] stop failed: {e!r}")

            def alive(self) -> bool:
                try:
                    return bool(state.hwnd) and bool(user32.IsWindow(state.hwnd))
                except Exception:
                    return False

        return _TrayHandle()
    except Exception as e:
        _log(f"[tray] FAILED: {e!r}")
        return None


_LOG_PATH = os.path.join(os.path.dirname(CACHE_PATH), "server.log")


def _log(msg: str) -> None:
    """--noconsole 模式下 stderr 不可见，日志落文件（与缓存同目录）。
    带 PID 前缀——14:48 时间线事故的教训：多实例写同一文件无 PID，
    死亡归因完全无法做。"""
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {msg}\n")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        _log(f"[FATAL] unhandled exception: {traceback.format_exc()}")
        sys.exit(1)
