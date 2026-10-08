#!/usr/bin/env python3
"""Tepat: source-aware Malay checker, manual rules and contextual usage evidence.

Uses data/evidence.sqlite built from the accepted cleaning run. Indonesian and
register reminders run independently. No automatic correction or factual verdict.
PRPM remains an on-demand dictionary lookup with hit/miss/unreachable states.
"""
import argparse
import base64
import hashlib
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

# Admin 权限（机器级，用户裁决 2026-10-05）：启动时一次性判定，不请求级验 token。
#   .env 放明文 TEPAT_ADMIN_TOKEN（gitignored），启动时 hash 后与代码内置 hash 比对；
#   match → 本机即 admin 机器，admin 功能在 UI 直接全开（无需任何输入）；
#   不 match / 未配 → 普通用户机器，admin 功能不下发。
# 代码里只有 hash，明文只存在于 admin 自己的 .env，泄露源码/hash 都推不出明文。
ADMIN_TOKEN_SHA256_EMBEDDED = (
    "3e564295dea3afbd7c7b74adcd8d0374299e8eed590ce248abb3b3a78271cc21"
)


def _decide_admin_machine() -> bool:
    raw = (os.environ.get("TEPAT_ADMIN_TOKEN") or "").strip()
    if not raw:
        return False
    import hmac as _hmac
    return _hmac.compare_digest(ADMIN_TOKEN_SHA256_EMBEDDED,
                                hashlib.sha256(raw.encode("utf-8")).hexdigest())


IS_ADMIN_MACHINE = _decide_admin_machine()  # _load_dotenv 之后立即判定


def _proposals_path() -> str:
    base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        "tepat") if os.name == "nt" else \
        os.path.join(os.path.expanduser("~"), ".tepat")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "proposals.jsonl")


# GitHub 同步：proposal 上云用（Issue 形态，admin 在 GitHub 上阅读）。
# 建议给 fine-grained PAT，仅 tepat repo 的 Issues: Write；泄露只可能被刷 Issue。
GH_PROPOSAL_REPO = os.environ.get("TEPAT_GH_REPO", "chenjunxu-ytl/tepat")

# server rules 云端源（用户裁决 2026-10-05）：规则的真源是 GitHub repo，
# 本地 reload 改为 fetch GitHub main 分支最新内容，而不是读本地文件自嗨。
GH_RULES_BRANCH = os.environ.get("TEPAT_GH_BRANCH", "main")
GH_SYNC_FILES = ("rules.json", "indo_words.json", "blacklist.json",
                 "extension/rules-pack.json", "word-overrides.json")


def _word_overrides_path() -> Path:
    """运行时 override 位置 = %APPDATA%\\tepat\\（与 prpm_cache.sqlite 同位，
    用户裁决 2026-10-08：运行时数据不落 exe/源码目录；打包目录可能只读）。"""
    _seed_to_appdata("word-overrides.json", os.path.join(_ROOT, "word-overrides.json"))
    return Path(_appdata_dir()) / "word-overrides.json"


def _load_word_overrides() -> dict:
    """word flags 审核产物（accept 生成的 override 集）。
    形态 {"word": {"status": "hit|miss", "definition": str, "source_issue": N,
                   "decided_at": ts, "decided_by": "admin"}}。
    prpm_lookup 的首要核实层：命中 override 直接用，不打 PRPM。"""
    try:
        return json.loads(_word_overrides_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _gh_fetch_file(repo_path: str) -> bytes:
    """从 GitHub raw 拉 main 分支的一个文件；失败抛 RuntimeError。"""
    import urllib.error as _ue
    import urllib.request as _rq
    url = (f"https://raw.githubusercontent.com/{GH_PROPOSAL_REPO}/"
           f"{GH_RULES_BRANCH}/{repo_path}")
    req = _rq.Request(url, headers={"Accept": "application/vnd.github.raw"})
    try:
        with _rq.urlopen(req, timeout=15) as resp:
            return resp.read()
    except _ue.HTTPError as e:
        raise RuntimeError(f"GitHub {repo_path}: HTTP {e.code}") from e
    except OSError as e:
        raise RuntimeError(f"GitHub {repo_path}: {e}") from e


def _gh_sync_rules() -> dict:
    """把 GitHub 上的 server rules + rule pack 拉下来覆盖本地。
    返回每个文件的状态；校验失败的文件不落盘（rules.json 必须可解析，
    rules-pack.json 必须过 pack 校验）。"""
    import tempfile
    results = {}
    for name in GH_SYNC_FILES:
        try:
            content = _gh_fetch_file(name)
        except RuntimeError as e:
            # word-overrides.json 尚未进 repo（还没第一个 accept decision）= 正常
            if name == "word-overrides.json" and "HTTP 404" in str(e):
                results[name] = {"ok": True, "bytes": 0, "note": "not in repo yet"}
                continue
            results[name] = {"ok": False, "error": str(e)}
            continue
        # 先校验再落盘
        try:
            if name.endswith(".json"):
                parsed = json.loads(content)
                if name == "extension/rules-pack.json":
                    problems = _validate_pack(parsed)
                    if problems:
                        results[name] = {"ok": False, "error": "; ".join(problems)}
                        continue
            else:
                results[name] = {"ok": False, "error": "unexpected file type"}
                continue
        except (json.JSONDecodeError, ValueError) as e:
            results[name] = {"ok": False, "error": f"bad JSON: {e}"}
            continue
        # rules.json 额外校验每条正则可编译（跟 Checker.reload 同口径）
        if name == "rules.json":
            bad = []
            for i, r in enumerate(parsed.get("rules", [])):
                try:
                    re.compile(r.get("re", ""))
                except re.error as e:
                    bad.append(f"rule {i} ({r.get('id', '?')}): {e}")
            if bad:
                results[name] = {"ok": False, "error": "; ".join(bad)}
                continue
        local = (_word_overrides_path() if name == "word-overrides.json"
                 else _config_path(name) if not name.startswith("extension/")
                 else str(_pack_path()))  # rules-pack 也归 APPDATA
        Path(local).write_bytes(content)
        results[name] = {"ok": True, "bytes": len(content)}
    # 同步后立即热重载 checker（若已加载）
    if _checker is not None:
        try:
            _checker.reload()
            _log(f"[sync] hot-reloaded checker after GitHub sync")
        except Exception as e:  # noqa: BLE001
            _log(f"[sync] hot-reload after sync failed: {e!r}")
    return results


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
    return _gh_create_issue(title[:120], body, ["rule-proposal"])


# word flag 分类（用户裁决 2026-10-08）：三个报告者视角选项 + optional 说明。
# kind 由用户在小弹窗里选（不再按状态自动映射——那套语义跟用户直觉不贴）。
FLAG_SCOPES = {
    "prpm":    "word existence / meaning from the PRPM panel",
    "grammar": "rule hit from the Rule Book / grammar check",
}
FLAG_KINDS = {
    "underflag":   "should have been flagged but was not (missed / passed wrongly)",
    "mismeaning":  "flagged but the suggestion/meaning is wrong",
    "overflag":    "flagged but the word is fine — false alarm",
}


def _gh_open_word_flag(word: str, scope: str, kind: str, status: str,
                       definition: str, note: str, page_url: str,
                       rule_id: str = "") -> str:
    """word/rule flag 开成 GitHub Issue。scope 区分 prpm（词）与 grammar（规则），
    kind 三选一；title 三段式 [flag:scope:kind]。"""
    if kind not in FLAG_KINDS:
        raise RuntimeError(f"unknown flag kind: {kind}")
    if scope not in FLAG_SCOPES:
        raise RuntimeError(f"unknown flag scope: {scope}")
    title = f"[flag:{scope}:{kind}] {rule_id or word}"
    body = (f"**word:** {word}\n"
            f"**scope:** {scope} — {FLAG_SCOPES[scope]}\n"
            f"**kind:** {kind} — {FLAG_KINDS[kind]}\n"
            + (f"**rule:** {rule_id}\n" if rule_id else "")
            + f"**prpm_status:** {status}\n"
            f"**definition:** {(definition or '')[:500]}\n"
            f"**user_note:** {note or '-'}\n"
            f"**page:** {page_url or '-'}\n"
            f"**flagged_at:** {int(time.time())}\n"
            + "\n\n_Auto-filed by Tepat extension._")
    return _gh_create_issue(title[:120], body,
                            ["word-flag", f"flag:{scope}", f"flag:{scope}:{kind}"])


def _gh_closed_flag_issues() -> list[str]:
    """拉取已关闭的 word-flag Issues（对账用）。返回 issue html_url 列表。
    60s 进程内缓存：对账在面板打开时发生，多用户共享一次 API 调用。"""
    global _closed_flags_cache, _closed_flags_at
    now = time.time()
    if _closed_flags_cache is not None and now - _closed_flags_at < 60:
        return _closed_flags_cache
    import urllib.error as _ue
    import urllib.request as _rq
    token = _gh_token()
    if not token:
        raise RuntimeError("no GitHub token configured")
    urls = []
    req = _rq.Request(
        f"https://api.github.com/repos/{GH_PROPOSAL_REPO}/issues"
        f"?labels=word-flag&state=closed&per_page=100",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"})
    with _rq.urlopen(req, timeout=15) as resp:
        for it in json.loads(resp.read()):
            u = it.get("html_url", "")
            if u:
                urls.append(u)
    _closed_flags_cache, _closed_flags_at = urls, now
    return urls


_closed_flags_cache: list[str] | None = None
_closed_flags_at: float = 0.0


def _gh_push_file(repo_path: str, content: str, message: str) -> str | None:
    """admin 决议/规则更新自动 commit 进 repo（Contents API）。返回 commit url；
    失败返回 None（降级为仅本地——手动 git commit 也行）。需要 token 的
    Contents: Read and write 权限。"""
    import urllib.error as _ue
    import urllib.request as _rq
    token = _gh_token()
    if not token:
        return None
    api = f"https://api.github.com/repos/{GH_PROPOSAL_REPO}/contents/{repo_path}"
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "Content-Type": "application/json"}
    # 取当前文件 sha（存在则更新，404 则新建）
    sha = None
    try:
        with _rq.urlopen(_rq.Request(api, headers=headers), timeout=15) as resp:
            sha = json.loads(resp.read()).get("sha")
    except _ue.HTTPError as e:
        if e.code != 404:
            return None
    except OSError:
        return None
    payload = {"message": message[:200],
               "content": base64.b64encode(content.encode("utf-8")).decode(),
               "branch": GH_RULES_BRANCH}
    if sha:
        payload["sha"] = sha
    try:
        req = _rq.Request(api, data=json.dumps(payload).encode(), headers=headers,
                          method="PUT")
        with _rq.urlopen(req, timeout=15) as resp:
            out = json.loads(resp.read())
            return out.get("commit", {}).get("html_url")
    except (_ue.HTTPError, OSError):
        return None


def _gh_comment_issue(issue_url: str, body: str) -> None:
    """在 Issue 上留决议评论（审核留痕）。"""
    import urllib.request as _rq
    token = _gh_token()
    if not token:
        return  # 无 token 时静默跳过评论，close 仍会尝试
    m = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", issue_url)
    if not m:
        return
    req = _rq.Request(
        f"https://api.github.com/repos/{m.group(1)}/issues/{m.group(2)}/comments",
        data=json.dumps({"body": body}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    _rq.urlopen(req, timeout=15).read()


def _gh_close_issue(issue_url: str) -> None:
    """关闭一条 flag Issue（unflag = 撤回审核请求；数据留档不删）。"""
    import urllib.error as _ue
    import urllib.request as _rq
    token = _gh_token()
    if not token:
        raise RuntimeError("no GitHub token configured")
    # html_url -> api url：.../repo/issues/N -> .../repos/repo/issues/N
    m = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", issue_url)
    if not m:
        raise RuntimeError(f"cannot parse issue url: {issue_url}")
    req = _rq.Request(
        f"https://api.github.com/repos/{m.group(1)}/issues/{m.group(2)}",
        data=json.dumps({"state": "closed"}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"},
        method="PATCH")
    with _rq.urlopen(req, timeout=15) as resp:
        if resp.status not in (200, 201):
            raise RuntimeError(f"close failed: HTTP {resp.status}")


def _gh_create_issue(title: str, body: str, labels: list[str]) -> str:
    """GitHub 开 Issue 的公共实现（proposal / word flag 共用）。"""
    import urllib.error as _ue
    import urllib.request as _rq
    token = _gh_token()
    if not token:
        raise RuntimeError("no GitHub token configured (%APPDATA%\\tepat\\gh_token)")
    req = _rq.Request(
        f"https://api.github.com/repos/{GH_PROPOSAL_REPO}/issues",
        data=json.dumps({"title": title, "body": body, "labels": labels}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    with _rq.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read()).get("html_url", "")

# ── Accepted evidence database ──

_words: set[str] = set()

EVIDENCE_PATH = os.environ.get("TEPAT_EVIDENCE_DB", os.path.join(_ROOT, "data", "evidence.sqlite"))
_checker: Checker | None = None


def _appdata_dir() -> str:
    base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        "tepat") if os.name == "nt" else \
        os.path.join(os.path.expanduser("~"), ".tepat")
    os.makedirs(base, exist_ok=True)
    return base


def _seed_to_appdata(name: str, src: str) -> None:
    """源码/exe 目录的配置文件复制到 APPDATA 作初始值（只补缺，不覆盖）。"""
    import shutil
    dst = os.path.join(_appdata_dir(), name)
    if not os.path.isfile(dst) and os.path.isfile(src):
        shutil.copyfile(src, dst)


def _config_path(name: str) -> str:
    """运行时配置位置（用户裁决 2026-10-08）：全部规则文件统一
    %APPDATA%\\tepat\\（与 prpm_cache.sqlite 同位）。源码/exe 目录的同名
    文件只作首次 seed；repo 同步、admin 决议、hot-reload 都读写 APPDATA。"""
    _seed_to_appdata(name, os.path.join(_ROOT, name))
    return os.path.join(_appdata_dir(), name)


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
    """Rule Book pack 的运行时位置：%APPDATA%\\tepat\\rules-pack.json
    （与全部规则文件同位）；源码目录 extension/ 下那份只作 seed。"""
    _seed_to_appdata("rules-pack.json",
                     os.path.join(_ROOT, "extension", "rules-pack.json"))
    return Path(_appdata_dir()) / "rules-pack.json"


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

# DBP 限速（用户裁决 2026-10-07）：并发 3 + 每请求 0.2s 间隔。纯串行 1 req/s
# 太慢（选区多词场景），30 req/s 又是爬虫特征容易被封 IP——3 并发 ≈ 几个用户
# 同时使用的正常流量。缓存命中的词不走这里。
_prpm_sem = threading.Semaphore(3)


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


def _looks_english_entry(definition: str) -> bool:
    """识别 Kamus Inggeris-Melayu 词条文本（KIMD 命中不该算马来语词 hit）。
    KIMD 特征：显式标注，或英文语法标签开头（n/adj/v/adv + 英文例句）。"""
    if not definition:
        return False
    if "Kamus Inggeris-Melayu" in definition:
        return True
    return bool(re.match(r"^(n|adj|v|adv)\s+[a-z]", definition[:8]))


def prpm_lookup(word: str) -> dict:
    """三态 + 释义: {'status': 'hit'|'miss'|'unreachable', 'definition': str}

    core 模式假 miss 防线（用户裁决 2026-10-07，实测 PRPM 不收虚词/partikel 组合）：
      1. 功能词短路——dan/ada/itu 等封闭集直接 hit，不打 PRPM
      2. miss 后自动剥 partikel/后缀重查——nilainya→nilai、apakah→apa；
         词根命中即报 hit，附带 root 信息
    """
    from checker import FUNCTION_WORDS
    # word-overrides 首要核实层（用户裁决 2026-10-08）：admin 审核 accept 的
    # flag 决议直接生效（hal → hit + 人工释义），不再打 PRPM/缓存。
    ov = _load_word_overrides().get(word)
    if ov:
        return {"word": word, "status": ov["status"],
                "definition": ov.get("definition", ""),
                "override": True, "source_issue": ov.get("source_issue")}
    if word in FUNCTION_WORDS:
        return {"word": word, "status": "hit",
                "definition": "Kata tugas (fungsi) — PRPM tidak menyenaraikan kata tugas sebagai entri.",
                "function_word": True}
    cached = cache_get_full(word)
    if cached:
        # 防御旧缓存坏条目：若记录为 hit 且包含 Carian kata tiada，视为无效重新查询
        if cached["status"] == "hit" and "Carian kata tiada" in (cached.get("definition") or ""):
            cached = None
        elif cached["status"] == "hit" and _looks_english_entry(cached.get("definition") or ""):
            # 字典源区分上线前的脏缓存（test/lion 等英文词存成了 hit）：
            # 降级 warn 并把缓存修正，不当作马来语词存在
            fixed = {"status": "warn", "definition": cached["definition"]}
            cache_put(word, "warn", cached["definition"])
            cached = fixed
        if cached and cached["status"] in {"miss", "warn"}:
            # 缓存的 miss/warn 同样走词根重查（nilainya 旧缓存 miss → nilai hit）
            root_hit = _prpm_root_retry(word)
            if root_hit:
                return {"word": word, "status": "hit",
                        "definition": root_hit["definition"],
                        "root": root_hit["root"],
                        "root_note": f"entri untuk akar kata '{root_hit['root']}'",
                        "from_cache": True}
            return {"word": word, "status": cached["status"],
                    "definition": cached.get("definition") or "",
                    "from_cache": True}
        else:
            return {"word": word, "status": cached["status"],
                    "definition": cached.get("definition") or "",
                    "from_cache": True}
    with _prpm_sem:
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
        time.sleep(0.2)  # 槽内小间隔：3 并发 × 0.2s ≈ 均匀 15 req/s 以下
        if status in {"miss", "warn"}:
            # 剥 partikel/akhiran/awalan 后重查词根（PRPM 不收组合形）
            root_hit = _prpm_root_retry(word)
            if root_hit:
                return {"word": word, "url": url, "status": "hit",
                        "definition": root_hit["definition"],
                        "root": root_hit["root"],
                        "root_note": f"entri untuk akar kata '{root_hit['root']}' (bentuk penuh = {root_hit['root']} + imbuhan)"}
        return {"word": word, "url": url, **parsed}


_PARTICLES_FOR_PRPM = ("nya", "lah", "kah", "tah", "pun", "ku", "mu")
_SUFFIXES_FOR_PRPM = ("kan", "an", "i")
_PREFIXES_FOR_PRPM = ("meng", "mem", "men", "di", "ke", "ber", "ter", "se", "pe")


def _strip_particles_suffix(word: str) -> list[str]:
    """PRPM 重查用的轻量剥离候选（无需 evidence 词表——PRPM 自己是 gate）。
    生成剥 partikel → 剥后缀 → 剥前缀的**全部中间形态**（perbezaannya →
    perbezaan、perbeza；dibeli → beli），从长到短，调用方按序试到 hit 为止。"""
    stems = [word]
    # partikel 一层
    for p in _PARTICLES_FOR_PRPM:
        if word.endswith(p) and len(word) > len(p) + 2:
            stems.append(word[:-len(p)])
            break
    # 后缀一层（作用于每个已有 stem，保留剥前形态）
    out = list(stems)
    for base in stems:
        for s in _SUFFIXES_FOR_PRPM:
            if base.endswith(s) and len(base) > len(s) + 2:
                cand = base[:-len(s)]
                out.append(cand)
    # 前缀一层（men- 家族配还原近似；PRPM gate 会兜底）
    finals = list(out)
    for base in out:
        for pre in _PREFIXES_FOR_PRPM:
            if base.startswith(pre) and len(base) > len(pre) + 2:
                finals.append(base[len(pre):])
    # 排序原则（用户裁决 2026-10-07）：**剥 partikel 的直接 stem 最优先**
    # （termasuklah→termasuk 是最保守正确的分析），其次 partikel+后缀 stem，
    # 最后前缀剥离形态按长→短。masuklah 这类"后缀没剥完+前缀剥了"的混合
    # 形态排最后。
    direct = stems[1:]  # partikel 剥离直接形态
    suffix_stems = [w for w in out if w not in stems]
    prefix_stems = [w for w in finals if w not in out]
    ordered, seen = [], {word}
    for group in (direct, suffix_stems, prefix_stems):
        for w in sorted(set(group), key=len, reverse=True):
            if len(w) >= 3 and w not in seen:
                seen.add(w)
                ordered.append(w)
    return ordered[:6]


def _prpm_root_retry(word: str) -> dict | None:
    """miss 后的词根重查。四条修复规则（实测 PRPM 真值标定 2026-10-07）：
    A. 连字符词拆分重查（litium-ion → litium HIT + ion HIT = 组件全在）
    B. 英文复数 s 剥离（cycles → cycle）
    C. FUNCTION_WORD 短路 hit 不作依据（keadaannya→adaannya→ada 假链）——
       例外：ke-X-an 构词时 X 是虚词合法（keadaannya = ke+ada+an）
    D. 常规剥离链按直接 stem 优先"""
    from checker import FUNCTION_WORDS
    # A. 连字符词：组件全部 hit 才算
    if "-" in word and len(word) > 4:
        parts = [p for p in word.split("-") if len(p) >= 2]
        if len(parts) >= 2:
            results = [prpm_lookup(p) for p in parts]
            if all(r["status"] == "hit" for r in results):
                defs = " | ".join(f"{p}: {(r.get('definition') or '')[:60]}"
                                  for p, r in zip(parts, results))
                cache_put(word, "hit", defs)
                return {"root": " + ".join(parts), "definition": defs}
    # B. 英文复数：cycle-s
    if word.endswith("s") and len(word) > 4 and not word.endswith("ss"):
        result = prpm_lookup(word[:-1])
        if result["status"] == "hit":
            cache_put(word, "hit", result.get("definition", ""))
            return {"root": word[:-1], "definition": result.get("definition", "")}
    for root in _strip_particles_suffix(word):
        result = prpm_lookup(root)
        if result["status"] != "hit":
            continue
        if result.get("function_word") or root in FUNCTION_WORDS:
            # C. ke-...-an 构词的虚词词根放行：word = ke + root + (an/annya)
            if word.startswith("ke") and (word == "ke" + root
                                          or word == "ke" + root + "an"
                                          or word == "ke" + root + "annya"):
                pass  # 合法构词，放行
            else:
                continue
        cache_put(word, "hit", result.get("definition", ""))
        return {"root": root, "definition": result.get("definition", "")}
    return None


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
        # PRPM 不收部分常用短词（hal/air 实测无条目）——miss 的 note 如实告知
        # 口径限制，避免把工具使用者教成"PRPM 没有 = 错字"
        return {"status": "miss", "definition": "",
                "note": "PRPM tiada entri. Kamus Dewan tidak menyenaraikan semua "
                        "kata pendek/kata terbitan; ketiadaan bukti bukti salah ejaan."}
    if "Definisi" not in body:
        return {"status": "unreachable", "note": "Dictionary definition could not be verified"}
    # 字典源区分（用户裁决 2026-10-07）：PRPM 同页混排 Kamus Bahasa Inggeris
    # （英→马，任何英文词都命中）和 Kamus Bahasa Melayu（Kamus Dewan/Pelajar）。
    # 马来语存在性检查只认马来语字典——英文词条降级 warn，提示换马来语词。
    if re.match(r"\s*Kamus Bahasa Inggeris", body):
        definition = re.split(r"Definisi\s*:", body, maxsplit=1)[-1].strip()
        return {"status": "warn", "definition": definition[:4000],
                "note": "Entri Inggeris (Kamus Inggeris-Melayu) — bukan kata Melayu; "
                        "pertimbangkan bentuk Melayu (ujian / menguji)."}
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
                             "admin": IS_ADMIN_MACHINE,
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
                    rule_views.append({"idx": i, "id": r.get("id"),
                                       "entry_id": r.get("entry_id") or f"idx:{i}",
                                       "conf": r.get("conf"),
                                       "enabled": r.get("enabled", True),
                                       "note": r.get("note"),
                                       "category": r.get("category"),
                                       "source": r.get("source"),
                                       "examples": r.get("examples", {}),
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
        elif self.path == "/api/word-overrides":
            # word-overrides.json 只读拉取（admin 下载 commit 进 repo 用）
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            self._json(200, _load_word_overrides())
        elif self.path == "/api/flag/sync":
            # flag 对账（用户裁决 2026-10-08）：返回已关闭的 word-flag Issue URL 集。
            # 前端拿它清本地 flaggedWords 里的死记录（云端已 close → 本地撤标记）。
            try:
                closed = _gh_closed_flag_issues()
            except RuntimeError as e:
                self._json(502, {"error": str(e)})
                return
            self._json(200, {"closed": closed, "count": len(closed),
                             "synced_at": int(time.time())})
        elif self.path == "/api/flags/list":
            # admin 审核台（用户裁决 2026-10-08）：全部 word-flag Issues（open +
            # closed，含 scope/kind/word 解析），供 web UI 的 Grammar/Word flags
            # 两个 tab 消费。"已写入规则"= issue body 带 rule= 或 closed 且有
            # proposal 引用——以 label/closed 状态近似表达。
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            import urllib.request as _rq
            token = _gh_token()
            if not token:
                self._json(502, {"error": "no GitHub token configured"})
                return
            items = []
            for state in ("open", "closed"):
                req = _rq.Request(
                    f"https://api.github.com/repos/{GH_PROPOSAL_REPO}/issues"
                    f"?labels=word-flag&state={state}&per_page=100",
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/vnd.github+json"})
                import urllib.error as _ue
                try:
                    with _rq.urlopen(req, timeout=15) as resp:
                        for it in json.loads(resp.read()):
                            m = re.match(r"\[flag:(prpm|grammar):(underflag|mismeaning|overflag)\]\s*(.+)",
                                         it.get("title", ""))
                            body = it.get("body") or ""
                            def body_field(k):
                                mm = re.search(rf"\*\*{k}:\*\*\s*(.+)", body)
                                return mm.group(1).strip() if mm else ""
                            items.append({
                                "number": it.get("number"),
                                "title": it.get("title", ""),
                                "url": it.get("html_url", ""),
                                "state": it.get("state", ""),
                                "scope": m.group(1) if m else "legacy",
                                "kind": m.group(2) if m else "",
                                "target": m.group(3) if m else it.get("title", ""),
                                "definition": body_field("definition"),
                                "user_note": body_field("user_note"),
                                "prpm_status": body_field("prpm_status"),
                                "created_at": it.get("created_at", ""),
                                "closed_at": it.get("closed_at"),
                                "labels": [l.get("name") for l in it.get("labels", [])],
                            })
                except (_ue.HTTPError, OSError) as e:
                    _log(f"[flags] list {state} failed: {e!r}")
            self._json(200, {"flags": items, "count": len(items)})
        elif self.path == "/api/proposals":
            # Rule Book 申请列表：读取需要 admin（申请内容对普通用户互相不可见）。
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin token required"})
                return
            proposals = []
            try:
                with open(_proposals_path(), encoding="utf-8") as f:
                    proposals = [json.loads(line) for line in f if line.strip()]
            except (OSError, json.JSONDecodeError):
                pass
            self._json(200, {"proposals": proposals,
                             "admin": IS_ADMIN_MACHINE})
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
        elif self.path == "/api/config/rule":
            # 单条规则开关（admin 机器）：改 rules.json 里该 entry 的 enabled
            # 并热重载。GitHub sync 会用 repo 版覆盖——toggle 是本地调试行为，
            # 要持久就 commit rules.json。
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            entry_id = str(req.get("entry_id") or "")
            enabled = req.get("enabled")
            if not entry_id or enabled is None:
                self._json(400, {"error": "entry_id and enabled required"})
                return
            path = Path(_config_path("rules.json"))
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                self._json(500, {"error": f"rules.json unreadable: {e}"})
                return
            target = next((r for r in data.get("rules", [])
                           if r.get("entry_id") == entry_id), None)
            if target is None and entry_id.startswith("idx:"):  # 无 entry_id 的旧规则
                try:
                    target = data["rules"][int(entry_id[4:])]
                except (ValueError, IndexError):
                    target = None
            if target is None:
                self._json(404, {"error": f"entry_id {entry_id} not found"})
                return
            target["enabled"] = bool(enabled)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
            if _checker is not None:
                try:
                    _checker.reload()
                except Exception as e:  # noqa: BLE001
                    _log(f"[config] reload after toggle failed: {e!r}")
            _log(f"[config] rule {entry_id} enabled={bool(enabled)}")
            self._json(200, {"ok": True, "entry_id": entry_id, "enabled": bool(enabled),
                             "active_rules": len(_checker.rules) if _checker else 0})
        elif self.path == "/api/sync":
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            try:
                results = _gh_sync_rules()
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": f"sync failed: {e}"})
                return
            ok = all(v.get("ok") for v in results.values())
            self._json(200 if ok else 207,
                       {"ok": ok, "files": results,
                        "repo": GH_PROPOSAL_REPO, "branch": GH_RULES_BRANCH,
                        "checker_reloaded": _checker is not None,
                        "rules": len(_checker.rules) if _checker else 0})
        elif self.path == "/api/pack":
            # 保存 Rule Book 插件包：admin 专属（普通用户请走 /api/proposals 申请）。
            # 写前备份 rules-pack.json.bak；JSON 校验失败不落盘。
            if not IS_ADMIN_MACHINE:
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
            # （RB-NNNN auto-increment）。写入方案照 rule-book 的 Salah/Betul 形态：
            # 用户给 trigger 短语（会命中）和 clear 短语（不该命中），服务端
            # 从 trigger 推导词边界正则——非技术用户不需要懂 regex。
            rule = req.get("rule")
            if not isinstance(rule, dict):
                self._json(400, {"error": "rule object required"})
                return
            note = str(rule.get("note") or "").strip()[:300]
            conf = str(rule.get("conf") or "warn").strip()[:16]
            triggers = [str(t).strip() for t in
                        (rule.get("triggers") if isinstance(rule.get("triggers"), list) else [rule.get("triggers")])
                        if t and str(t).strip()]
            clears = [str(c).strip() for c in
                      (rule.get("clears") if isinstance(rule.get("clears"), list) else [rule.get("clears")])
                      if c and str(c).strip()]
            pattern = str(rule.get("re") or "").strip()[:500]  # 高级用户可直供正则
            if not triggers and not pattern:
                self._json(400, {"error": "at least one trigger phrase (or a regex) is required"})
                return
            if conf not in ("error", "warn", "note", "exception"):
                self._json(400, {"error": "conf must be error/warn/note/exception"})
                return
            pack = _load_pack()
            rid = f"RB-{_next_rule_no(pack):03d}"
            if not pattern and triggers:
                # 从 trigger 短语推导正则：转义 + 词边界；多短语 OR 起来
                parts = []
                for t in triggers[:5]:
                    esc_t = re.escape(t.strip())
                    parts.append(rf"\b{esc_t}\b" if esc_t[:2] != r"\b" else esc_t)
                pattern = "|".join(parts)
            ok, err = True, None
            try:
                rx = re.compile(pattern)  # 近似校验，跟保存包同一口径
            except re.error as e:
                ok, err = False, str(e)
            # 用 trigger/clear 实测推导出的正则：trigger 应命中、clear 不应命中
            mismatches = []
            if ok:
                for t in triggers:
                    if not rx.search(t):
                        mismatches.append(f"does not match trigger: {t[:50]}")
                for c in clears:
                    if rx.search(c):
                        mismatches.append(f"should not match: {c[:50]}")
            entry = {"id": rid, "conf": conf, "re": pattern, "note": note,
                     "triggers": triggers[:5], "clears": clears[:5],
                     "regex_valid": ok, "regex_error": err,
                     "self_test": mismatches or "pass",
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
                             "self_test": mismatches or "pass",
                             "github_issue": gh_url or None,
                             "github_error": gh_error})
        elif self.path == "/api/flag":
            # 词级 flag（用户裁决 2026-10-08）：免 token 反馈通道。
            # scope 区分 prpm（PRPM 面板）与 grammar（Rule Book 规则命中）；
            # kind 三选一由用户在小弹窗选；状态/定义以 server 查询为准。
            word = str(req.get("word") or "").strip().lower()[:80]
            kind = str(req.get("kind") or "").strip()
            scope = str(req.get("scope") or "prpm").strip()
            rule_id = str(req.get("rule") or "").strip()[:40]
            if not word:
                self._json(400, {"error": "word required"})
                return
            if scope == "grammar" and not rule_id:
                self._json(400, {"error": "rule id required for grammar flags"})
                return
            if kind not in FLAG_KINDS:
                self._json(400, {"error": "kind must be underflag/mismeaning/overflag"})
                return
            note = str(req.get("note") or "").strip()[:500]
            page_url = str(req.get("page") or "").strip()[:300]
            # 状态/定义以 server 自己的查询结果为准（不信任前端传值）
            result = prpm_lookup(word)
            status = result.get("status", "unknown")
            definition = result.get("definition", "")
            root = result.get("root", "")
            try:
                gh_url = _gh_open_word_flag(word, scope, kind, status, definition,
                                            (f"{note} | root: {root}" if root else note),
                                            page_url, rule_id=rule_id)
            except RuntimeError as e:
                self._json(502, {"error": f"GitHub issue failed: {e}"})
                return
            _log(f"[flag] {word} ({scope}/{kind}) -> {gh_url}")
            self._json(200, {"ok": True, "word": word, "status": status,
                             "kind": kind, "scope": scope,
                             "github_issue": gh_url})
        elif self.path == "/api/flags/decide":
            # Word flag 审核决议（用户裁决 2026-10-08，admin 专属）：
            #   accept: status(hit/miss) + definition 来源（issue 里的解释/
            #           user_note/自写）→ 写 word-overrides.json + close issue
            #           附决议评论；下次 sync 该文件进 repo，全端热生效
            #   reject: 只 close issue（附评论），不写 override
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            decision = str(req.get("decision") or "").strip()
            word = str(req.get("word") or "").strip().lower()[:80]
            issue_url = str(req.get("issue") or "").strip()[:300]
            if decision not in ("accept", "reject") or not word \
                    or not issue_url.startswith("https://github.com/"):
                self._json(400, {"error": "decision (accept|reject), word and issue required"})
                return
            entry = None
            push_url = None
            if decision == "accept":
                status = str(req.get("status") or "hit").strip()
                if status not in ("hit", "miss"):
                    self._json(400, {"error": "status must be hit or miss"})
                    return
                definition = str(req.get("definition") or "").strip()[:1000]
                source = str(req.get("source") or "custom").strip()
                if not definition:
                    self._json(400, {"error": "definition required (pick a source or write one)"})
                    return
                m = re.search(r"/issues/(\d+)", issue_url)
                entry = {"status": status, "definition": definition,
                         "source": source, "source_issue": int(m.group(1)) if m else 0,
                         "decided_at": int(time.time()), "decided_by": "admin"}
                overrides = _load_word_overrides()
                overrides[word] = entry
                _word_overrides_path().write_text(
                    json.dumps(overrides, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
            # close issue（附决议评论）
            comment = (f"**Decision: {decision}**"
                       + (f"\n- status: `{entry['status']}`\n- definition: {entry['definition'][:300]}"
                          f"\n- written to `word-overrides.json` (hot-applied on sync)"
                          if entry else "\n- no override written"))
            try:
                _gh_comment_issue(issue_url, comment)
                _gh_close_issue(issue_url)
            except RuntimeError as e:
                self._json(502, {"error": f"github failed: {e}"})
                return
            _log(f"[decide] {word}: {decision}"
                 + (f" -> override {entry['status']}" if entry else " (rejected)"))
            self._json(200, {"ok": True, "word": word, "decision": decision,
                             "override": entry,
                             "pushed": push_url if decision == "accept" else None})
        elif self.path == "/api/unflag":
            # unflag = 关闭对应 GitHub Issue（撤回审核请求；数据留档）。
            # issue url 由前端从 chrome.storage.local 的 flaggedWords 带来。
            word = str(req.get("word") or "").strip().lower()[:60]
            issue_url = str(req.get("issue") or "").strip()[:300]
            if not word or not issue_url.startswith("https://github.com/"):
                self._json(400, {"error": "word and issue url required"})
                return
            try:
                _gh_close_issue(issue_url)
            except RuntimeError as e:
                self._json(502, {"error": f"close issue failed: {e}"})
                return
            _log(f"[unflag] {word} -> closed {issue_url}")
            self._json(200, {"ok": True, "word": word, "closed": issue_url})
        elif self.path == "/api/proposals/accept":
            # admin 批准申请：把规则追加进 rules-pack.json 的 rules 数组尾部。
            if not IS_ADMIN_MACHINE:
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
            merged = {
                "id": rid, "conf": str(target["conf"]),
                "note": str(target.get("note") or ""), "re": str(target["re"])}
            if target.get("triggers") or target.get("clears"):
                merged["examples"] = {"trigger": target.get("triggers", []),
                                      "clear": target.get("clears", [])}
            pack.setdefault("rules", []).append(merged)
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
    # 2026-10-08：每 20 个周期（≈5 分钟）自动 fetch GitHub 规则/override，
    # user 侧免手动（TEPAT_AUTO_SYNC=0 可关）。失败静默重试下轮。
    auto_sync = os.environ.get("TEPAT_AUTO_SYNC", "1") != "0"

    def _rules_sync_thread():
        while not stop_event.is_set():
            for _ in range(20):  # 20 × 15s = 5min，可被退出打断
                if stop_event.is_set():
                    return
                time.sleep(15)
            try:
                results = _gh_sync_rules()
                ok = sum(1 for v in results.values() if v.get("ok"))
                if ok:
                    _log(f"[auto-sync] fetched {ok}/{len(results)} files from GitHub")
            except Exception as e:  # noqa: BLE001
                _log(f"[auto-sync] failed: {e!r}")

    if not args.no_tray:
        def _tray_watchdog():
            # 赋值 tray_icon（重启托盘）需要 global 声明，否则整个函数里
            # tray_icon 被当成局部变量，第一次读就 UnboundLocalError。
            global tray_icon
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
        if auto_sync:
            threading.Thread(target=_rules_sync_thread, daemon=True,
                             name="rules-auto-sync").start()

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
