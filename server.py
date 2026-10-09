#!/usr/bin/env python3
"""Tepat: Malay checker driven by APPDATA rules (Rule Book products) and PRPM.

word/grammar checks read rules.json (RB-* rules, LLM-translated from human
descriptions) and indo_words.json from %APPDATA%\\tepat; word existence comes
from PRPM lookups (cached in sqlite). data/evidence.sqlite is legacy — when
present it still upgrades scans with spelling/context channels, but nothing
requires it (2026-10-09 user decision: corpus evidence retired).
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

from checker import Checker, utf16_offset
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


def _appdata_dir() -> str:
    """tepat 运行时数据目录：%APPDATA%\\tepat（Windows）/ ~/.tepat（其他）。
    rules、词表、prpm cache、.env 全部住这里——源码/exe 目录不放运行时数据。"""
    base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        "tepat") if os.name == "nt" else \
        os.path.join(os.path.expanduser("~"), ".tepat")
    os.makedirs(base, exist_ok=True)
    return base


def _env_path() -> str:
    """.env 的位置：与全部运行时数据同位（%APPDATA%\\tepat\\.env /
    ~/.tepat/.env）。源码/exe 目录不再放 env（开源项目仓库可能只读；
    用户裁决 2026-10-09）。"""
    return os.path.join(_appdata_dir(), ".env")


# server 实际读取的 .env key 及其字段说明（权威清单——Env tab 的行内文档
# 由 /api/env 的 key_docs 提供，前端不 hardcode key 名或示例值）。
ENV_KEY_DOCS = {
    "TEPAT_ADMIN_TOKEN": "admin machine token — grants review/push on this machine",
    "TEPAT_GH_TOKEN": "GitHub PAT — falls back to the gh_token file beside this .env",
    "TEPAT_GH_REPO": "rules/proposals repo",
    "TEPAT_GH_BRANCH": "rules branch",
    "TEPAT_AUTO_SYNC": "set 0 to disable the 5-minute GitHub auto-sync",
    "TEPAT_EVIDENCE_DB": "path to evidence.sqlite",
    "TEPAT_LLM_URL": "chat-completions endpoint for rule translation",
    "TEPAT_LLM_KEY": "API key for the rule-translation endpoint",
    "TEPAT_LLM_MODEL": "model id for rule translation",
}


def _load_dotenv() -> None:
    """启动时读 tepat 数据目录的 .env（KEY=VALUE 逐行），不覆盖已有环境变量。
    .env 在 .gitignore 里，绝不打包进 release。"""
    path = _env_path()
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
                 "word-overrides.json")


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
    """把 GitHub 上的规则文件拉下来覆盖本地（rules.json 是唯一规则源，
    用户裁决 2026-10-08：Rule Book pack 已并入，双轨废除）。
    返回每个文件的状态；校验失败的文件不落盘（JSON 必须可解析，
    rules.json 每条正则必须可编译）。
    本地领先保护（2026-10-09 Step2 失败根因）：本地 mtime 比上次成功
    sync 晚、且上次写盘后的 push 未确认成功（push 失败/token 缺失）时，
    GitHub 是旧版本——覆盖会把本地刚写的 trial 规则冲掉。此情况跳过该
    文件并标 stale。上次 push 成功则 GitHub 已含本地写入，正常覆盖。"""
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
            else:
                results[name] = {"ok": False, "error": "unexpected file type"}
                continue
        except (json.JSONDecodeError, ValueError) as e:
            results[name] = {"ok": False, "error": f"bad JSON: {e}"}
            continue
        # rules.json 额外校验：每条 regex/plugin 参数可执行（跟 reload 同口径；
        # 2026-10-09 起规则是 plugin 形态，re 字段不再存在）
        if name == "rules.json":
            bad = []
            from checker import RULE_PLUGINS
            for i, r in enumerate(parsed.get("rules", [])):
                try:
                    if r.get("plugin") == "regex_match":
                        re.compile((r.get("params") or {}).get("pattern", ""))
                    # 其他 plugin 的 params 校验交给 reload（缺字段进 config_warnings）
                except re.error as e:
                    bad.append(f"rule {i} ({r.get('id', '?')}): {e}")
            if bad:
                results[name] = {"ok": False, "error": "; ".join(bad)}
                continue
        local = (_word_overrides_path() if name == "word-overrides.json"
                 else _config_path(name))
        # 本地领先保护（内容指纹，2026-10-09 Step2 根因）：本地文件内容与
        # "上次确认与 GitHub 一致"的指纹不同 = 有未同步的本地写入（translate/
        # enhance/accept 落盘且 push 未确认成功）→ GitHub 是旧的，跳过覆盖。
        # push 成功（_gh_push_file）/sync 覆盖成功都会刷新指纹。
        if _local_ahead(str(local)):
            results[name] = {"ok": True, "bytes": len(content),
                             "note": "local newer, sync skipped (pending push)"}
            continue
        Path(local).write_bytes(content)
        results[name] = {"ok": True, "bytes": len(content)}
        _mark_synced(name, str(local))
    # 同步后立即热重载 checker（若已加载）
    if _checker is not None:
        try:
            _checker.reload()
            _log(f"[sync] hot-reloaded checker after GitHub sync")
        except Exception as e:  # noqa: BLE001
            _log(f"[sync] hot-reload after sync failed: {e!r}")
    return results


# 每个规则文件的"已同步基线"内容 sha：与 GitHub 最后一次对齐时的本地内容。
# 本地内容指纹 ≠ 基线 = 有未同步的本地写入（stale）。mtime 在同秒快速连写
# 时分辨不出（Windows），内容指纹没有这个坑。
# 持久化到 APPDATA sync-baseline.json（2026-10-09 第三次丢失根因）：之前基线
# 只在内存，重启即丢；rules+PRPM 模式不跑 load_words()，基线 dict 整个进程
# 生命周期都是空的——本地领先保护从未生效，startup fetch 直接覆盖未推送的
# trial 规则。持久化后跨进程存活；启动时不再盲目标记（旧代码把当前内容
# 无条件视为"与 GitHub 一致"，等于伪造基线）。
_sync_baseline: dict[str, str] = {}


def _baseline_path() -> str:
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        "tepat", "sync-baseline.json") if os.name == "nt" else \
        os.path.join(os.path.expanduser("~"), ".tepat", "sync-baseline.json")


def _load_sync_baseline() -> None:
    """启动时从 APPDATA 读回基线（失败=空基线，视同无保护）。"""
    try:
        data = json.loads(Path(_baseline_path()).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _sync_baseline.update({k: v for k, v in data.items()
                                   if isinstance(k, str) and isinstance(v, str)})
    except (OSError, json.JSONDecodeError):
        pass


def _save_sync_baseline() -> None:
    tmp = _baseline_path() + ".tmp"
    try:
        Path(tmp).write_text(json.dumps(_sync_baseline, indent=1), encoding="utf-8")
        os.replace(tmp, _baseline_path())
    except OSError:
        pass  # 基线写不进盘只影响保护精度，不值得因此崩掉调用方


def _file_sha(path: str) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _local_ahead(local_path: str) -> bool:
    name = os.path.basename(local_path)
    base = _sync_baseline.get(name)
    if base is None:
        return False  # 无基线（首次）→ 以 GitHub 为准
    return _file_sha(local_path) != base


def _mark_synced(repo_path: str, local_path: str | None = None) -> None:
    """本地写入 + push 成功（或 sync 刚覆盖）后调用：把基线刷成当前本地内容
    指纹，表示"GitHub 已含本地内容"。push 失败时不调用——sync 会保护这个
    文件直到 push 成功。"""
    path = local_path or _config_path(repo_path)
    sha = _file_sha(path)
    if sha is not None:
        _sync_baseline[repo_path] = sha
        _save_sync_baseline()


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
    "general": "page-level subjective issue from the popup flag (not tied to a word/rule)",
}
FLAG_KINDS = {
    "underflag":   "should have been flagged but was not (missed / passed wrongly)",
    "mismeaning":  "flagged but the suggestion/meaning is wrong",
    "overflag":    "flagged but the word is fine — false alarm",
    "other":       "something else — see the explanation",
}


def _gh_open_word_flag(word: str, scope: str, kind: str, status: str,
                       definition: str, note: str,
                       rule_id: str = "") -> str:
    """word/rule flag 开成 GitHub Issue。scope 区分 prpm（词）与 grammar（规则），
    kind 三选一；title 三段式 [flag:scope:kind]。不记录来源页面（隐私）。"""
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


class GHPushError(RuntimeError):
    """push 失败（token 缺失/权限不足/网络）——调用方必须把这个错误显示给
    admin，不能静默吞掉。2026-10-09 air 事件：accept 显示成功但 override
    没同步上 GitHub，admin 以为链路通了。"""


def _gh_push_file(repo_path: str, content: str, message: str) -> str:
    """admin 决议/规则更新自动 commit 进 repo（Contents API）。返回 commit url；
    失败抛 GHPushError（本地写入保留，sync 层会保护这份领先写入不被回滚）。
    成功时刷新同步基线。需要 token 的 Contents: Read and write 权限。"""
    import urllib.error as _ue
    import urllib.request as _rq
    token = _gh_token()
    if not token:
        raise GHPushError("no GitHub token — set TEPAT_GH_TOKEN in the Env tab")
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
            raise GHPushError(f"GitHub GET {repo_path}: HTTP {e.code} — "
                              f"check TEPAT_GH_TOKEN / TEPAT_GH_REPO") from e
    except OSError as e:
        raise GHPushError(f"GitHub GET {repo_path}: {e}") from e
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
                url = out.get("commit", {}).get("html_url")
                if url:
                    _mark_synced(repo_path,
                                 str(_word_overrides_path()) if repo_path == "word-overrides.json"
                                 else None)  # GitHub 已含本地内容
                return url
        except _ue.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read()).get("message", "")
            except Exception:  # noqa: BLE001
                pass
            raise GHPushError(f"GitHub PUT {repo_path}: HTTP {e.code} {detail} — "
                              f"token needs Contents: Read and write") from e
        except OSError as e:
            raise GHPushError(f"GitHub PUT {repo_path}: {e}") from e


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


# ── LLM 规则转译（用户裁决 2026-10-09）────────────────────────────────────
# 单次调用、无会话（架构裁决）：任务形状是结构化→结构化（人类例句 + PRPM
# 释义 → rule JSON），不需要 harness/多轮。agent 不能从 0 创造规则——人类
# 提供例句、人类发起 translate/enhance；LLM 只做"例句→规则规格"的翻译。
# 产物默认 trial（conf 降半级），人审 accept/reject；行为规格（例句集）只增
# 不减，每次纠正永久变成回归测试。API 配置走 .env（Env tab 可编辑）：
#   TEPAT_LLM_URL   e.g. https://open.bigmodel.cn/api/paas/v4/chat/completions
#   TEPAT_LLM_KEY   API key
#   TEPAT_LLM_MODEL e.g. glm-4.7


def _llm_config() -> tuple[str, str, str]:
    url = (os.environ.get("TEPAT_LLM_URL") or "").strip().rstrip("/")
    key = (os.environ.get("TEPAT_LLM_KEY") or "").strip()
    model = (os.environ.get("TEPAT_LLM_MODEL") or "").strip()
    if not url or not key or not model:
        raise RuntimeError("LLM not configured — set TEPAT_LLM_URL / "
                           "TEPAT_LLM_KEY / TEPAT_LLM_MODEL in .env (Env tab)")
    return url, key, model


_TRANSLATE_SYSTEM = """You help build a Malay grammar checker. A human describes a language point in plain words — which usage is wrong and which is right, possibly with example sentences embedded in their description. You do the rest in ONE step:
1. Extract test sentences from their description: trigger sentences (clearly wrong, must be flagged) and clear sentences (clearly right, must NOT be flagged). If the human gave no usable sentence for a side, write one faithful to their description.
2. Build a rule that flags the triggers and spares the clears.
3. Mental-test the regex against every extracted sentence.

Output: ONE JSON object, no prose, in exactly this shape:
{
 "desc": "<one-sentence natural-language description of what the rule flags, for human reviewers>",
 "note": "<short Malay note shown to the end user when flagged>",
 "conf": "high|medium|low",
 "plugin": "regex_match",
 "params": {"pattern": "<python regex>", "exceptions": ["<regex>"], "suggestion": "<replacement text or empty>"},
 "examples": {"trigger": ["<wrong sentence>", ...], "clear": ["<right sentence>", ...]}
}
Rules for the regex:
- Python re syntax, case-insensitive matching is applied by the engine.
- Every trigger sentence must match; NO clear sentence may match.
- Use \\b word boundaries around words; keep the pattern as narrow as the examples justify.
- exceptions patterns suppress matches inside their span — use them for legal contexts visible in clear examples.
- If the distinction cannot be expressed by regex (counting items, clause analysis), set "plugin" to null and explain in "desc" starting with "NEEDS-PLUGIN:".
Spelling/conf guidance:
- conf high = structural error with no legal context.
- conf medium = usually wrong but context can make it legal.
- conf low = broad reminder.
- Suggestion must be standard DBP Malay when obvious; empty string otherwise.
The server re-tests your regex against your examples and shows failures to the human — be strict with yourself before answering."""


def _llm_chat(payload: dict) -> dict:
    """POST chat/completions。GLM 系关 thinking（转译不需要推理链，省时省
    token，用户裁决 2026-10-09）；不认 thinking 参数的端点 400 时去掉重试。"""
    import urllib.error as _ue
    import urllib.request as _rq
    url, key, model = _llm_config()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"model": model, "messages": payload["messages"],
            "temperature": 0.1, "max_tokens": 16000,
            "thinking": {"type": "disabled"}}
    req = _rq.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with _rq.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except _ue.HTTPError as e:
        if e.code == 400:  # OpenAI 系端点拒绝未知参数 thinking → 原样重试
            req = _rq.Request(url, data=json.dumps(
                {"model": model, "messages": payload["messages"],
                 "temperature": 0.1, "max_tokens": 16000}).encode(), headers=headers)
            try:
                with _rq.urlopen(req, timeout=180) as resp:
                    return json.loads(resp.read())
            except _ue.HTTPError as e2:
                raise RuntimeError(f"LLM API error {e2.code}: "
                                   f"{e2.read().decode('utf-8', 'replace')[:200]}") from e2
            except OSError as e2:
                raise RuntimeError(f"LLM API unreachable: {e2}") from e2
        raise RuntimeError(f"LLM API error {e.code}: "
                           f"{e.read().decode('utf-8', 'replace')[:200]}") from e
    except OSError as e:
        raise RuntimeError(f"LLM API unreachable: {e}") from e


def _llm_translate_rule(rule_id: str, human_text: str,
                        prpm_defs: dict[str, str]) -> dict:
    """单次 LLM 调用：人类的自然语言描述 + PRPM 释义 → rule spec（dict）。
    例句由 LLM 从描述中提炼（描述里带句子就用原句，没有就按描述撰写），
    落在 spec["examples"]；server 复测 regex 并把失败呈现给人类。
    输出 shape 严格校验，坏产物抛 ValueError（不落盘）。"""
    url, key, model = _llm_config()
    defs = "\n".join(f"- {w}: {d[:300]}" for w, d in prpm_defs.items()) or "- (none)"
    user = f"""Rule id: {rule_id}
Human's description:
\"\"\"{human_text.strip()[:3000]}\"\"\"

PRPM definitions of words appearing in the description:
{defs}

Return the JSON object only."""
    body = _llm_chat({"messages": [
        {"role": "system", "content": _TRANSLATE_SYSTEM},
        {"role": "user", "content": user}]})
    choice = (body.get("choices") or [{}])[0]
    content = choice.get("message", {}).get("content", "")
    if not content and choice.get("finish_reason") == "length":
        raise ValueError("LLM ran out of tokens before answering "
                         "(reasoning model — raise max_tokens)")
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError("LLM returned no JSON object")
    try:
        spec = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned invalid JSON: {e}") from e
    # shape 校验：plugin 产物必须是可执行形态
    if not isinstance(spec, dict) or not spec.get("desc") or not spec.get("note"):
        raise ValueError("LLM output missing desc/note")
    if spec.get("conf") not in ("high", "medium", "low"):
        raise ValueError("LLM output conf must be high/medium/low")
    if spec.get("plugin") is None:  # NEEDS-PLUGIN 路径：合法，前端展示给人类决定
        if not str(spec.get("desc", "")).startswith("NEEDS-PLUGIN:"):
            raise ValueError("plugin null requires NEEDS-PLUGIN: desc")
        return spec
    if spec["plugin"] not in ("regex_match",):
        raise ValueError(f"unknown plugin {spec['plugin']!r}")
    params = spec.get("params") or {}
    if not params.get("pattern"):
        raise ValueError("regex_match requires params.pattern")
    try:
        re.compile(params["pattern"])
    except re.error as e:
        raise ValueError(f"LLM regex invalid: {e}") from e
    # 例句自测：LLM 提炼的 trigger 必中、clear 必不中（行为规格，人类免读
    # regex）。例句缺失按 0 例句处理——只有 pattern 也要求至少能编译。
    ex = spec.get("examples") or {}
    triggers = [str(t).strip()[:300] for t in (ex.get("trigger") or []) if str(t).strip()]
    clears = [str(c).strip()[:300] for c in (ex.get("clear") or []) if str(c).strip()]
    if not triggers:
        raise ValueError("LLM extracted no trigger sentences from the description")
    rx = re.compile(params["pattern"], re.IGNORECASE)
    ex_ex = params.get("exceptions") or []
    fails = []
    for t in triggers:
        if not rx.search(t):
            fails.append(f"does not flag trigger: {t[:60]}")
    for c in clears:
        if rx.search(c) and not any(re.search(x, c, re.IGNORECASE) for x in ex_ex):
            fails.append(f"flags clear sentence: {c[:60]}")
    spec["_self_test"] = fails or "pass"
    spec["_triggers"], spec["_clears"] = triggers, clears
    return spec


def _rule_words_for_prpm(text: str) -> list[str]:
    """描述文本里值得查 PRPM 的词：>3 字符、非功能词、去重、最多 8 个。"""
    from checker import FUNCTION_WORDS
    seen, out = set(), []
    for w in re.findall(r"[a-zA-Z]+", text.lower()):
        if len(w) > 3 and w not in FUNCTION_WORDS and w not in seen:
            seen.add(w)
            out.append(w)
    return out[:8]


_TRIAL_DOWNGRADE = {"high": "medium", "medium": "low", "low": "low"}


def _spawn_trial_rule(spec: dict, triggers: list[str], clears: list[str],
                      origin_note: str, rule_id: str = "") -> dict:
    """LLM 产物 → trial 规则条目（conf 降半级，原级记在 conf_trial，
    人审 accept 恢复）。例句集合并进条目（行为规格随规则走 GitHub）。
    新 system prompt 不再让 LLM 回显 id——规则号由 server 生成。"""
    conf = spec.get("conf", "medium")
    rid = rule_id or spec.get("id")
    entry = {
        "id": rid, "entry_id": rid,  # entry_id=规则号：decide/enhance 定位条目
        "plugin": spec["plugin"],
        "conf": _TRIAL_DOWNGRADE[conf], "conf_trial": conf,
        "status": "trial",
        "desc": spec["desc"], "note": spec["note"],
        "source": origin_note,
        "examples": {"trigger": triggers, "clear": clears},
        "params": spec.get("params", {}),
    }
    if spec.get("_self_test") != "pass":
        entry["self_test_failures"] = spec["_self_test"]
    return entry


_words: set[str] = set()

EVIDENCE_PATH = os.environ.get("TEPAT_EVIDENCE_DB", os.path.join(_ROOT, "data", "evidence.sqlite"))
_checker: Checker | None = None


def _seed_to_appdata(name: str, src: str) -> None:
    """源码/exe 目录的配置文件复制到 APPDATA 作初始值（只补缺，不覆盖）。
    2026-10-09 起 repo 根目录不再放 rules.json/indo_words.json（与 APPDATA
    双份是冗余）——GitHub 是初始分发渠道，首次启动的 sync 立即拉取。此
    函数只剩 exe 附带文件（若有）和 word-overrides 的兼容 seed 作用。"""
    import shutil
    dst = os.path.join(_appdata_dir(), name)
    if not os.path.isfile(dst) and os.path.isfile(src):
        shutil.copyfile(src, dst)


def _config_path(name: str) -> str:
    """运行时配置位置（用户裁决 2026-10-08）：全部规则文件统一
    %APPDATA%\\tepat\\（与 prpm_cache.sqlite 同位）。repo 同步、admin 决议、
    hot-reload 都读写 APPDATA；GitHub 是唯一的初始分发渠道（首次启动的
    sync 立即执行一次，不等 5 分钟周期）。"""
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


# 无 evidence 的词级扫描（用户裁决 2026-10-08）：evidence.sqlite 删除后
# /api/scan 直接 503，但词表信号（indo/casual）只需要 indo_words.json +
# 分词器——不依赖任何数据库。全页 Scan words 走这里；有 evidence 时
# spelling/cold 通道仍由 /api/scan 提供（面板查询用）。
def scan_words_lightweight(text: str, register: str = "formal") -> dict:
    """无 evidence.sqlite 的完整扫描降级通道（2026-10-09 用户裁决：evidence
    语料库抓不准且巨大，弃用——word/grammar 检查全部依赖 APPDATA 的规则
    文件（rules.json RB-* / indo_words.json）+ PRPM API）。
    覆盖：词表信号（indo/casual）+ 规则（regex_match plugin 直判）。
    不覆盖（随 evidence 一起退役）：spelling 冷词、bigram 语境、词根还原。"""
    from text_units import tokens, sentences
    try:
        indo = json.loads(Path(_config_path("indo_words.json")).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        indo = {}
    if "indo_only" in indo and "casual" in indo:
        indo_map = {w: (v.get("ms", "") if isinstance(v, dict) else "")
                    for w, v in indo["indo_only"].items()}
        casual_map = {w: (v.get("ms", "") if isinstance(v, dict) else "")
                      for w, v in indo["casual"].items()}
    else:  # 旧结构兼容
        legacy_sug = indo.get("suggestions", {})
        indo_map = {w: legacy_sug.get(w, "") for w in indo.get("indo_only_words", [])}
        casual_map = {w: "" for w in indo.get("uncertain_words", [])}
        casual_map.update({w: v.get("expansion", "")
                           for w, v in indo.get("register_words", {}).items()})
        casual_map.update({w: "" for w in indo.get("context_words", {})})
    issues = []
    # 规则通道（rules.json，与 checker 的 regex_match 同口径）：admin 的
    # RB-* 规则在无 evidence 模式下照常工作。
    try:
        rules_data = json.loads(Path(_config_path("rules.json")).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        rules_data = {}
    all_sents = list(sentences(text[:30000]))
    for rule in rules_data.get("rules", []):
        if (not isinstance(rule, dict) or rule.get("enabled") is False
                or rule.get("plugin") != "regex_match"
                or rule.get("register", "any") not in {"any", register}
                or rule.get("noflag")):
            continue
        params = rule.get("params") or {}
        pattern = params.get("pattern")
        if not pattern:
            continue
        try:
            rx = re.compile(pattern, re.IGNORECASE)
            exceptions = [re.compile(x, re.IGNORECASE) for x in params.get("exceptions", [])]
        except re.error:
            continue
        level = {"high": "error", "medium": "warning", "low": "info"}[rule.get("conf", "medium")]
        suggestion_tpl = params.get("suggestion", "")
        for start, _end, sent in all_sents:
            excl = [m.span() for ex in exceptions for m in ex.finditer(sent)]
            for m in rx.finditer(sent):
                if any(a <= m.start() and m.end() <= b for a, b in excl):
                    continue
                # 捕获组替换（如 sangat \1）：pattern 带 group 时 m.groups() 有值
                suggestion = ""
                if suggestion_tpl:
                    try:
                        suggestion = m.expand(suggestion_tpl) if "\\1" in suggestion_tpl \
                            else suggestion_tpl
                    except (re.error, IndexError):
                        suggestion = suggestion_tpl
                issues.append({"category": rule.get("category", "grammar"),
                               "level": level, "confidence": rule.get("conf", "medium"),
                               "start": utf16_offset(text, start + m.start()),
                               "end": utf16_offset(text, start + m.end()),
                               "span": sent[m.start():m.end()],
                               "note": rule.get("note", ""),
                               "origin": rule.get("entry_id", rule["id"]),
                               "suggestion": suggestion})
    # 词表通道：句首大写不是专名（checker 的 named 语义）——只有非句首的
    # 大写词才算专有名词跳过。
    sent_starts = {s for s, _e, _t in sentences(text[:30000])}
    for t in tokens(text[:30000]):
        named = t.raw[:1].isupper() and t.start not in sent_starts
        acronym = t.raw.isupper() and len(t.raw) > 1
        if t.word in indo_map and not named and not acronym:
            sug = indo_map.get(t.word, "")
            issues.append({"category": "terminology", "level": "info" if named else "warning",
                           "start": utf16_offset(text, t.start), "end": utf16_offset(text, t.end),
                           "span": t.raw, "note": "Bentuk calon bahasa Indonesia; "
                           "semak makna dan laras sebelum menggantikannya.",
                           "origin": f"indo:{t.word}", "suggestion": sug})
        elif t.word in casual_map and not named and not acronym and register == "formal":
            ms = casual_map.get(t.word, "")
            issues.append({"category": "register",
                           "level": "warning" if ms else "info",
                           "start": utf16_offset(text, t.start), "end": utf16_offset(text, t.end),
                           "span": t.raw, "note": "Bentuk tidak formal; pertimbangkan "
                           "bentuk standard dalam penulisan formal." if ms else
                           "Penggunaan ini memerlukan konteks.",
                           "origin": f"casual:{t.word}", "suggestion": ms})
    issues.sort(key=lambda i: (i["start"], i["end"]))
    return {"engine": "wordlists-rules-v2", "register": register,
            "issues": issues, "total_words": len(issues)}


def _load_rules_json() -> dict | None:
    """rules.json 解析态（proposal 编号与 accept 落盘都走它）。"""
    try:
        return json.loads(Path(_config_path("rules.json")).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _next_rule_no(rules: dict | None) -> int:
    """auto-increment 规则号：现有 rules + pending proposals 里的 RB-NNNN 取最大 +1。
    proposals 也必须算——否则两次提交（未批准时）会撞号。"""
    biggest = 0
    for r in (rules or {}).get("rules", []):
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
    # 词根重查在信号量外（用户裁决 2026-10-08，77/258 卡死根因）：root retry
    # 递归调用 prpm_lookup，若在槽内递归，外层持槽等内层、内层排队等槽，
    # 3 个并发递归即占满全部槽 → 永久死锁。
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


_PRPM_RETRY_DEPTH = threading.local()


def _prpm_root_retry(word: str) -> dict | None:
    """miss 后的词根重查。四条修复规则（实测 PRPM 真值标定 2026-10-07）：
    A. 连字符词拆分重查（litium-ion → litium HIT + ion HIT = 组件全在）
    B. 英文复数 s 剥离（cycles → cycle）
    C. FUNCTION_WORD 短路 hit 不作依据（keadaannya→adaannya→ada 假链）——
       例外：ke-X-an 构词时 X 是虚词合法（keadaannya = ke+ada+an）
    D. 常规剥离链按直接 stem 优先
    递归深度上限 2（77/258 卡死的另一半根因：词根的词根的词根…候选爆炸；
    外层死锁修掉后仍需防止一环 miss 触发整棵剥离树递归）。"""
    from checker import FUNCTION_WORDS
    depth = getattr(_PRPM_RETRY_DEPTH, "depth", 0)
    if depth >= 2:
        return None
    _PRPM_RETRY_DEPTH.depth = depth + 1
    try:
        return _prpm_root_retry_inner(word, FUNCTION_WORDS)
    finally:
        _PRPM_RETRY_DEPTH.depth = depth


def _prpm_root_retry_inner(word: str, FUNCTION_WORDS) -> dict | None:
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
            self._json(200, {"ok": _checker is not None, "engine": "evidence-v2",
                             "grammar": _checker is not None,
                             "words": len(_words),
                             "review_run": _checker.store.metadata["review_run"] if _checker else None,
                             "source_counts": _checker.store.metadata["counts"] if _checker else {},
                             "config_warnings": _checker.config_warnings if _checker else [],
                             "admin": IS_ADMIN_MACHINE,
                             "tray": tray_icon is not None})
        elif self.path == "/api/env":
            # Env tab（admin 专属）：读磁盘上的 .env 原文。非 admin 一律 403——
            # 里面有 admin token / PAT，普通用户机器上不存在也不该看。
            # key_docs：server 实际读取的 key 的字段说明（权威清单在代码读取
            # 处；前端不 hardcode key 名/示例值）。
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            content = ""
            exists = os.path.isfile(_env_path())
            if exists:
                try:
                    content = Path(_env_path()).read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    self._json(500, {"error": f"read failed: {e}"})
                    return
            self._json(200, {"path": _env_path(), "exists": exists, "content": content,
                             "key_docs": ENV_KEY_DOCS})
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
                    if not r.get("plugin"):  # regex 条目才编译校验
                        try:
                            re.compile(r.get("re", ""))
                        except re.error as e:
                            ok, err = False, str(e)
                    rule_views.append({"idx": i, "id": r.get("id"),
                                       "entry_id": r.get("entry_id") or f"idx:{i}",
                                       "conf": r.get("conf"),
                                       "enabled": r.get("enabled", True),
                                       "status": r.get("status", "settled"),
                                       "desc": r.get("desc", r.get("note", "")),
                                       "plugin": r.get("plugin"),
                                       "note": r.get("note"),
                                       "category": r.get("category"),
                                       "source": r.get("source"),
                                       "examples": r.get("examples", {}),
                                       "self_test": r.get("self_test_failures", "pass"),
                                       "params": r.get("params"),
                                       "register": r.get("register", "any"),
                                       "valid": ok, "error": err})
            # 词表文件视图（用户裁决 2026-10-08）：三分类 indo_only / casual /
            # overrides，词表条目带 ms mapping。新旧结构都读（旧 exe 拉新文件
            # 的兼容性在 checker；这里是新前端的视图）。
            wordlists = {"indo_only": {}, "casual": {}}
            try:
                indo = json.loads(Path(_config_path("indo_words.json")).read_text(
                    encoding="utf-8")) if os.path.isfile(_config_path("indo_words.json")) else {}
                if "indo_only" in indo and "casual" in indo:
                    wordlists = {
                        "indo_only": {w: (v.get("ms", "") if isinstance(v, dict) else str(v))
                                      for w, v in indo["indo_only"].items()},
                        "casual": {w: (v.get("ms", "") if isinstance(v, dict) else str(v))
                                   for w, v in indo["casual"].items()},
                    }
                else:  # 旧结构（老文件/GitHub 上还没推新版的窗口期）
                    wordlists = {
                        "indo_only": {w: indo.get("suggestions", {}).get(w, "")
                                      for w in indo.get("indo_only_words", [])},
                        "casual": {**{w: "" for w in indo.get("uncertain_words", [])},
                                   **{w: v.get("expansion", "") for w, v in indo.get("register_words", {}).items()},
                                   **{w: "" for w in indo.get("context_words", {})}},
                    }
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
            overrides = [{"word": w, **rec} for w, rec in
                         sorted(_load_word_overrides().items())]
            self._json(200, {"files": entries, "rule_views": rule_views,
                             "wordlists": wordlists, "overrides": overrides,
                             "loaded": _checker is not None,
                             "config_warnings": _checker.config_warnings if _checker else []})
        elif self.path == "/api/word-overrides":
            # word-overrides.json 只读拉取（admin 下载 commit 进 repo 用）
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            self._json(200, _load_word_overrides())
        elif self.path == "/api/wordlists":
            # 词表词集（extension Word 面板 chip 身份色用）：只给词列表，
            # 不带 mapping，轻量免 token。新旧结构都读。
            try:
                indo = json.loads(Path(_config_path("indo_words.json")).read_text(
                    encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._json(200, {"indo_only": [], "casual": []})
                return
            if "indo_only" in indo and "casual" in indo:
                self._json(200, {"indo_only": sorted(indo["indo_only"]),
                                 "casual": sorted(indo["casual"])})
            else:
                self._json(200, {
                    "indo_only": sorted(indo.get("indo_only_words", [])),
                    "casual": sorted(set(indo.get("uncertain_words", []))
                                     | set(indo.get("register_words", {}))
                                     | set(indo.get("context_words", {})))})
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
                            m = re.match(r"\[flag:(prpm|grammar|general):(underflag|mismeaning|overflag|other)\]\s*(.+)",
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
            # 无 evidence 时降级为词表+规则通道（2026-10-09 用户裁决：evidence
            # 语料库退役，word/grammar 检查全部走 APPDATA 规则 + PRPM）。
            text = str(req.get("text") or "")
            register = str(req.get("register") or "formal")
            if _checker is not None:
                try:
                    self._json(200, scan(text, register))
                except ValueError as e:
                    self._json(400, {"error": str(e)})
                except (RuntimeError, sqlite3.Error):
                    self._json(503, {"error": "Evidence unavailable; scan was not completed"})
            else:
                try:
                    self._json(200, scan_words_lightweight(text, register))
                except ValueError as e:
                    self._json(400, {"error": str(e)})
        elif self.path == "/api/scan-words":
            # 词级全页扫描：不依赖 evidence.sqlite（词表信号即可用）。
            # 有 evidence 时升级用完整 scan 的词级类别。
            text = str(req.get("text") or "")
            register = str(req.get("register") or "formal")
            if _checker is not None:
                try:
                    r = scan(text, register)
                    word_cats = {"terminology", "register", "spelling"}
                    r = {**r, "engine": "evidence-v2-words",
                         "issues": [i for i in r.get("issues", []) if i["category"] in word_cats]}
                    self._json(200, r)
                    return
                except (RuntimeError, sqlite3.Error):
                    pass  # evidence 在但查询失败 → 落到轻量路径
            self._json(200, scan_words_lightweight(text, register))
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
        elif self.path == "/api/rules/decide":
            # 规则人审（admin 专属，用户裁决 2026-10-08）：agent 转译产物默认
            # trial（conf 降半级）。accept → status settled + conf 升回原级；
            # reject → 整条移除（连带同 whitelist）。写盘 → 热重载 → auto-push。
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            decision = str(req.get("decision") or "").strip()
            entry_id = str(req.get("entry_id") or "")
            if decision not in ("accept", "reject") or not entry_id:
                self._json(400, {"error": "decision (accept|reject) and entry_id required"})
                return
            path = Path(_config_path("rules.json"))
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                self._json(500, {"error": f"rules.json unreadable: {e}"})
                return
            idx, target = None, None
            for i, r in enumerate(data.get("rules", [])):
                if r.get("entry_id") == entry_id or \
                        (entry_id.startswith("idx:") and i == int(entry_id[4:])):
                    idx, target = i, r
                    break
            if target is None:
                self._json(404, {"error": f"entry_id {entry_id} not found"})
                return
            if decision == "accept":
                _CONF_UPGRADE = {"high": "high", "medium": "high",
                                 "low": "medium"}  # trial 降过半级，升回
                target["status"] = "settled"
                if target.get("conf_trial"):  # 有原始级别记录才恢复
                    target["conf"] = target.pop("conf_trial")
                path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                encoding="utf-8")
                action = f"{entry_id} trial→settled"
            else:  # reject
                removed = data["rules"].pop(idx)
                path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                encoding="utf-8")
                action = f"{entry_id} removed"
            if _checker is not None:
                try:
                    _checker.reload()
                except Exception as e:  # noqa: BLE001
                    _log(f"[rules] hot-reload after decide failed: {e!r}")
            pushed = None
            push_err = None
            try:
                pushed = _gh_push_file("rules.json",
                                       path.read_text(encoding="utf-8"),
                                       f"rules: {action} (human review)")
            except GHPushError as e:
                push_err = str(e)
                _log(f"[rules] GitHub push failed: {e!r}")
            _log(f"[rules] {action} pushed={bool(pushed)}")
            self._json(200, {"ok": True, "action": action, "pushed": bool(pushed),
                             **({"push_error": push_err} if push_err else {})})
        elif self.path == "/api/rules/translate":
            # LLM 转译入口（admin 发起，用户裁决 2026-10-09）：人类用自己的
            # 话描述语言现象（哪些用法错、哪些对，可夹例句），server 取 PRPM
            # 释义喂给 LLM；例句由 LLM 从描述中提炼，产物以 trial 落盘（conf
            # 降半级）→ 热重载 → auto-push。agent 不从 0 创造规则——描述和
            # 发起动作都是人类给的。
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            human_text = str(req.get("text") or "").strip()[:3000]
            if not human_text:
                self._json(400, {"error": "describe the language point first"})
                return
            rules_data = _load_rules_json()
            if rules_data is None:
                self._json(500, {"error": "rules.json not found or broken"})
                return
            rid = f"RB-{_next_rule_no(rules_data):03d}"
            # PRPM enrichment：描述关键词的 DBP 释义（缓存优先，缺词不强求）
            prpm_defs = {}
            for w in _rule_words_for_prpm(human_text):
                try:
                    r = prpm_lookup(w)
                    if r.get("status") == "hit" and r.get("definition"):
                        prpm_defs[w] = r["definition"][:300]
                except Exception:  # noqa: BLE001 — 释义失败不挡转译
                    continue
            try:
                spec = _llm_translate_rule(rid, human_text, prpm_defs)
            except (RuntimeError, ValueError) as e:
                self._json(502, {"error": f"translation failed: {e}"})
                return
            if spec.get("plugin") is None:
                # NEEDS-PLUGIN：regex 表达不了，不落盘——人类决定 park/设计 plugin
                self._json(200, {"ok": True, "id": rid, "needs_plugin": True,
                                 "desc": spec.get("desc", ""),
                                 "note": "regex cannot express this rule — human decides"})
                return
            triggers, clears = spec["_triggers"], spec["_clears"]
            entry = _spawn_trial_rule(spec, triggers, clears,
                                      f"LLM-translated from human description ({time.strftime('%Y-%m-%d')})",
                                      rule_id=rid)
            rules_data.setdefault("rules", []).append(entry)
            path = Path(_config_path("rules.json"))
            path.write_text(json.dumps(rules_data, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
            if _checker is not None:
                try:
                    _checker.reload()
                except Exception as e:  # noqa: BLE001
                    _log(f"[rules] hot-reload after translate failed: {e!r}")
            pushed = None
            push_err = None
            try:
                pushed = _gh_push_file("rules.json", path.read_text(encoding="utf-8"),
                                       f"rules: add trial {rid} (LLM translation)")
            except GHPushError as e:
                push_err = str(e)
                _log(f"[rules] GitHub push failed: {e!r}")
            _log(f"[rules] translated {rid} trial pushed={bool(pushed)} "
                 f"self_test={entry.get('self_test_failures', 'pass')}")
            self._json(200, {"ok": True, "id": rid, "entry": entry,
                             "pushed": bool(pushed),
                             **({"push_error": push_err} if push_err else {})})
        elif self.path == "/api/rules/enhance":
            # Enhance（admin，用户裁决 2026-10-09）：trial 规则行为不对时，
            # 人类用自然语言补充（哪里不对、正确的用法是什么，可夹例句），
            # LLM 重跑转译并把新例句并入例句集——只增不减，每次纠正永久
            # 变成回归测试。同 entry_id 的旧 trial 被替换（保留原 id）。
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            entry_id = str(req.get("entry_id") or "")
            new_text = str(req.get("text") or "").strip()[:3000]
            if not entry_id or not new_text:
                self._json(400, {"error": "entry_id and a description of what's wrong are required"})
                return
            rules_data = _load_rules_json()
            if rules_data is None:
                self._json(500, {"error": "rules.json not found or broken"})
                return
            target = next((r for r in rules_data.get("rules", [])
                           if r.get("entry_id") == entry_id), None)
            if target is None and entry_id.startswith("idx:"):
                try:
                    target = rules_data["rules"][int(entry_id[4:])]
                except (ValueError, IndexError):
                    target = None
            if target is None:
                self._json(404, {"error": f"entry_id {entry_id} not found"})
                return
            if target.get("status") != "trial":
                self._json(400, {"error": "only trial rules can be enhanced "
                                          "(settled rules are updated via /api/rules/update)"})
                return
            ex = target.get("examples") or {}
            old_triggers = [str(t) for t in (ex.get("trigger") or [])]
            old_clears = [str(c) for c in (ex.get("clear") or [])]
            old_desc = target.get("desc") or target.get("note") or ""
            human_text = (f"Existing rule — {old_desc}. Its example sentences:\n"
                          + "\n".join(f"wrong: {t}" for t in old_triggers)
                          + "\n" + "\n".join(f"right: {c}" for c in old_clears)
                          + f"\n\nHuman's correction:\n\"\"\"{new_text}\"\"\"")
            prpm_defs = {}
            for w in _rule_words_for_prpm(human_text):
                try:
                    r = prpm_lookup(w)
                    if r.get("status") == "hit" and r.get("definition"):
                        prpm_defs[w] = r["definition"][:300]
                except Exception:  # noqa: BLE001
                    continue
            try:
                spec = _llm_translate_rule(str(target["id"]), human_text, prpm_defs)
            except (RuntimeError, ValueError) as e:
                self._json(502, {"error": f"re-translation failed: {e}"})
                return
            if spec.get("plugin") is None:
                self._json(200, {"ok": True, "needs_plugin": True,
                                 "desc": spec.get("desc", ""),
                                 "note": "regex cannot express this rule — human decides"})
                return
            # 例句集只增不减：LLM 提炼的新例句并入旧例句集（去重）
            triggers = list(dict.fromkeys(old_triggers + spec["_triggers"]))[:12]
            clears = list(dict.fromkeys(old_clears + spec["_clears"]))[:12]
            fresh = _spawn_trial_rule(spec, triggers, clears, target.get("source", ""),
                                      rule_id=str(target["id"]))
            fresh["id"] = target["id"]
            fresh["enhanced_from"] = entry_id
            rules_data["rules"][rules_data["rules"].index(target)] = fresh
            path = Path(_config_path("rules.json"))
            path.write_text(json.dumps(rules_data, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
            if _checker is not None:
                try:
                    _checker.reload()
                except Exception as e:  # noqa: BLE001
                    _log(f"[rules] hot-reload after enhance failed: {e!r}")
            pushed = None
            push_err = None
            try:
                pushed = _gh_push_file("rules.json", path.read_text(encoding="utf-8"),
                                       f"rules: enhance trial {target['id']} (new examples)")
            except GHPushError as e:
                push_err = str(e)
                _log(f"[rules] GitHub push failed: {e!r}")
            _log(f"[rules] enhanced {target['id']} (examples {len(triggers)}T/{len(clears)}C) "
                 f"self_test={fresh.get('self_test_failures', 'pass')}")
            self._json(200, {"ok": True, "entry": fresh, "pushed": bool(pushed),
                             **({"push_error": push_err} if push_err else {})})
        elif self.path == "/api/rules/update":
            # Update（admin，用户裁决 2026-10-09）：非行为字段直改——conf/
            # desc/note。行为规格（pattern/例句）不走这里：行为错 = 补例句
            # enhance。写盘 → 热重载 → auto-push。
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            entry_id = str(req.get("entry_id") or "")
            conf = req.get("conf")
            desc = str(req.get("desc") or "").strip()
            note = str(req.get("note") or "").strip()
            if not entry_id:
                self._json(400, {"error": "entry_id required"})
                return
            if conf is not None and conf not in ("high", "medium", "low"):
                self._json(400, {"error": "conf must be high/medium/low"})
                return
            if not conf and not desc and not note:
                self._json(400, {"error": "nothing to update (conf/desc/note)"})
                return
            rules_data = _load_rules_json()
            if rules_data is None:
                self._json(500, {"error": "rules.json not found or broken"})
                return
            target = next((r for r in rules_data.get("rules", [])
                           if r.get("entry_id") == entry_id), None)
            if target is None and entry_id.startswith("idx:"):
                try:
                    target = rules_data["rules"][int(entry_id[4:])]
                except (ValueError, IndexError):
                    target = None
            if target is None:
                self._json(404, {"error": f"entry_id {entry_id} not found"})
                return
            if conf:
                target["conf"] = conf
                target.pop("conf_trial", None)  # 手动定级后不再有"恢复原级"语义
            if desc:
                target["desc"] = desc
            if note:
                target["note"] = note
            path = Path(_config_path("rules.json"))
            path.write_text(json.dumps(rules_data, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
            if _checker is not None:
                try:
                    _checker.reload()
                except Exception as e:  # noqa: BLE001
                    _log(f"[rules] hot-reload after update failed: {e!r}")
            pushed = None
            push_err = None
            try:
                pushed = _gh_push_file("rules.json", path.read_text(encoding="utf-8"),
                                       f"rules: update {target.get('id')} fields")
            except GHPushError as e:
                push_err = str(e)
                _log(f"[rules] GitHub push failed: {e!r}")
            _log(f"[rules] updated {entry_id} fields={list(filter(None, ['conf' if conf else '', 'desc' if desc else '', 'note' if note else '']))}")
            self._json(200, {"ok": True, "entry_id": entry_id, "pushed": bool(pushed),
                             **({"push_error": push_err} if push_err else {})})
        elif self.path == "/api/sync":
            # 手动拉 GitHub 规则文件：全员可用（用户裁决 2026-10-08）——
            # 5 分钟自动同步本来就不分机器跑，手动触发没有理由限制；
            # 拉的是公开 repo，无权限风险。push 类操作才限 admin。
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
            rules_data = _load_rules_json()
            rid = f"RB-{_next_rule_no(rules_data):03d}"
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
            # scope 区分 prpm（PRPM 面板）/ grammar（规则命中）/ general
            # （popup ⚑ 页面级主观问题）；kind 由用户在小弹窗选。
            word = str(req.get("word") or "").strip().lower()[:80]
            kind = str(req.get("kind") or "").strip()
            scope = str(req.get("scope") or "prpm").strip()
            rule_id = str(req.get("rule") or "").strip()[:40]
            if scope == "general":
                # general 不绑词/规则：note 是主体，不查 PRPM
                if not word:
                    word = "page"
                if kind not in FLAG_KINDS:
                    self._json(400, {"error": "kind must be underflag/mismeaning/overflag/other"})
                    return
                note = str(req.get("note") or "").strip()[:500]
                if not note:
                    self._json(400, {"error": "a short explanation is required for general flags"})
                    return
                try:
                    gh_url = _gh_open_word_flag(word, scope, kind, "-",
                                                "-", note, rule_id="")
                except RuntimeError as e:
                    self._json(502, {"error": f"GitHub issue failed: {e}"})
                    return
                _log(f"[flag] general/{kind} -> {gh_url}")
                self._json(200, {"ok": True, "kind": kind, "scope": scope,
                                 "github_issue": gh_url})
                return
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
            # 状态/定义以 server 自己的查询结果为准（不信任前端传值）
            result = prpm_lookup(word)
            status = result.get("status", "unknown")
            definition = result.get("definition", "")
            root = result.get("root", "")
            try:
                gh_url = _gh_open_word_flag(word, scope, kind, status, definition,
                                            (f"{note} | root: {root}" if root else note),
                                            rule_id=rule_id)
            except RuntimeError as e:
                self._json(502, {"error": f"GitHub issue failed: {e}"})
                return
            _log(f"[flag] {word} ({scope}/{kind}) -> {gh_url}")
            self._json(200, {"ok": True, "word": word, "status": status,
                             "kind": kind, "scope": scope,
                             "github_issue": gh_url})
        elif self.path == "/api/wordlists/add":
            # Word Lists tab 添加词（admin 专属，用户裁决 2026-10-08）：两个
            # field——原词 + 标准马来文。写 indo_words.json（APPDATA）→ 热重载
            # → auto-push GitHub（其他机器 5 分钟内拉到）。list 取
            # indo_only|casual；ms 可空（没对应就留空）。
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            lst = str(req.get("list") or "").strip()
            word = str(req.get("word") or "").strip().lower()[:80]
            ms = str(req.get("ms") or "").strip()[:120]
            if lst not in ("indo_only", "casual"):
                self._json(400, {"error": "list must be indo_only or casual"})
                return
            if not word or " " in word:
                self._json(400, {"error": "single word required"})
                return
            path = Path(_config_path("indo_words.json"))
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                self._json(500, {"error": f"indo_words.json unreadable: {e}"})
                return
            if word in data.get(lst, {}):
                self._json(409, {"error": f"'{word}' already in {lst} "
                                          f"(ms: {data[lst][word].get('ms', '')!r})"})
                return
            other = "casual" if lst == "indo_only" else "indo_only"
            if word in data.get(other, {}):
                self._json(409, {"error": f"'{word}' already in {other}"})
                return
            data.setdefault(lst, {})[word] = {"ms": ms}
            data[lst] = {w: data[lst][w] for w in sorted(data[lst])}
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
            if _checker is not None:  # 热重载：加了就生效
                try:
                    _checker.reload()
                except Exception as e:  # noqa: BLE001
                    _log(f"[wordlists] hot-reload failed: {e!r}")
            pushed = None
            push_err = None
            try:  # auto-push：让其他机器自动拉到（失败不阻塞本地添加）
                pushed = _gh_push_file("indo_words.json",
                                       path.read_text(encoding="utf-8"),
                                       f"word lists: add {word!r} to {lst}")
            except GHPushError as e:
                push_err = str(e)
                _log(f"[wordlists] GitHub push failed: {e!r}")
            _log(f"[wordlists] {lst} += {word!r} (ms={ms!r}) pushed={bool(pushed)}")
            self._json(200, {"ok": True, "list": lst, "word": word, "ms": ms,
                             "pushed": bool(pushed),
                             **({"push_error": push_err} if push_err else {})})
        elif self.path == "/api/flags/decide":
            # Word flag 审核决议（用户裁决 2026-10-08，admin 专属）：
            #   accept: status 由 flag kind 推导（underflag → 该词存在 hit；
            #           overflag/mismeaning → 该词不算 miss）+ admin 编辑的
            #           definition → 写 word-overrides.json + close issue 附评论
            #   reject: 只 close issue（附评论），不写 override
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            decision = str(req.get("decision") or "").strip()
            word = str(req.get("word") or "").strip().lower()[:80]
            issue_url = str(req.get("issue") or "").strip()[:300]
            kind = str(req.get("kind") or "").strip()
            if decision not in ("accept", "reject") or not word \
                    or not issue_url.startswith("https://github.com/"):
                self._json(400, {"error": "decision (accept|reject), word and issue required"})
                return
            entry = None
            push_url = None
            push_err = None
            if decision == "accept":
                # 词没有 over/under 属性——真相只有 is a word / not a word。
                # reporter 的立场（kind）推导出词的最终状态。
                status = "hit" if kind == "underflag" else "miss"
                definition = str(req.get("definition") or "").strip()[:1000]
                if not definition:
                    self._json(400, {"error": "definition required"})
                    return
                m = re.search(r"/issues/(\d+)", issue_url)
                entry = {"status": status, "definition": definition,
                         "flag_kind": kind or "unknown",
                         "source_issue": int(m.group(1)) if m else 0,
                         "decided_at": int(time.time()), "decided_by": "admin"}
                overrides = _load_word_overrides()
                overrides[word] = entry
                content = json.dumps(overrides, ensure_ascii=False, indent=1) + "\n"
                _word_overrides_path().write_text(content, encoding="utf-8")
                # 自动 push 进 repo（用户裁决 2026-10-08）：user 只 pull。
                # push 失败（token 权限/网络）不再静默——2026-10-09 air 事件：
                # issue closed + 评论成功让 admin 误以为同步完成。本地写入保留
                # （sync 基线保护），错误显式回传前端。
                try:
                    push_url = _gh_push_file(
                        "word-overrides.json", content,
                        f"word-override: {word} -> {status} (#{entry['source_issue']})")
                except GHPushError as e:
                    push_err = str(e)
                    _log(f"[decide] {word}: push failed: {e}")
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
                             "pushed": push_url if decision == "accept" else None,
                             **({"push_error": push_err} if push_err else {})})
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
            # admin 批准申请：把规则追加进 rules.json（唯一规则源，用户裁决
            # 2026-10-08——Rule Book pack 双轨已废除）。conf 档位换算
            # error/warn/note → high/medium/low，校验后落盘 + 热重载。
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
            rules_data = _load_rules_json()
            if rules_data is None:
                self._json(404, {"error": "rules.json not found or broken"})
                return
            rid = str(target["id"])
            conf_map = {"error": "high", "warn": "medium",
                        "note": "low", "exception": "low"}
            merged = {
                "id": rid, "conf": conf_map.get(str(target["conf"]), "medium"),
                "note": str(target.get("note") or ""), "re": str(target["re"]),
                "source": "Rule Book proposal (accepted via web UI)"}
            if target.get("triggers") or target.get("clears"):
                merged["examples"] = {"trigger": target.get("triggers", []),
                                      "clear": target.get("clears", [])}
            try:
                re.compile(merged["re"])
            except re.error as e:
                self._json(400, {"error": f"proposal regex invalid: {e}"})
                return
            rules_data.setdefault("rules", []).append(merged)
            rules_path = Path(_config_path("rules.json"))
            rules_path.with_suffix(".json.bak").write_bytes(rules_path.read_bytes())
            rules_path.write_text(json.dumps(rules_data, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8")
            with proposals_path.open("w", encoding="utf-8") as f:  # 已处理：清空申请列表
                for p in proposals:
                    if p is not target:
                        f.write(json.dumps(p, ensure_ascii=False) + "\n")
            if _checker is not None:  # 热重载：accept 即生效
                try:
                    _checker.reload()
                except Exception as e:  # noqa: BLE001
                    _log(f"[proposals] hot-reload after accept failed: {e!r}")
            _log(f"[proposals] accepted {rid} -> rules.json ({len(rules_data['rules'])} rules)")
            self._json(200, {"ok": True, "accepted": rid,
                             "rules": len(rules_data["rules"])})
        elif self.path == "/api/env":
            # Env tab 保存（admin 专属）：整文件覆盖写回磁盘 .env。
            # 只写磁盘——进程内环境变量/IS_ADMIN_MACHINE 不动（改 admin token
            # 需要"旧 token 撤权"效果，热应用反而违背启动时判定的初衷）；
            # 下次重启 _load_dotenv 生效。TEPAT_ADMIN_TOKEN 的 hash 比对也
            # 因此保持"改了 token → 重启后才切换 admin 身份"的干净语义。
            if not IS_ADMIN_MACHINE:
                self._json(403, {"error": "admin machine required"})
                return
            content = str(req.get("content") or "")
            if len(content) > 65536:
                self._json(413, {"error": "env file too large (64KB limit)"})
                return
            # KEY=VALUE 行格式校验：非空行必须含 "="，防止手滑写成 JSON/shell。
            bad = [ln for ln in content.splitlines()
                   if ln.strip() and not ln.strip().startswith("#") and "=" not in ln]
            if bad:
                self._json(400, {"error": f"lines without '=': {bad[:3]}"})
                return
            path = _env_path()
            # 写前备份同目录 .env.bak（一次滚动，够找回手误）。
            # 注意用字符串拼接：Path('.env').with_suffix() 对 dotfile 会得到
            # '.env.env.bak' 这种怪名。
            try:
                if os.path.isfile(path):
                    Path(path + ".bak").write_bytes(Path(path).read_bytes())
            except OSError as e:
                _log(f"[env] backup failed (continuing): {e!r}")
            try:
                Path(path).write_text(content, encoding="utf-8")
            except OSError as e:
                self._json(500, {"error": f"write failed: {e}"})
                return
            _log(f"[env] .env overwritten via web UI ({len(content)} chars) — restart to apply")
            self._json(200, {"ok": True, "path": path, "bytes": len(content.encode('utf-8')),
                             "note": "restart tepat to apply changes"})
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

    # evidence.sqlite 是遗留升级包（2026-10-09 用户裁决：语料证据退役）——
    # 没有它服务完整：词表 + RB 规则 + PRPM 就是全部检查通道；有它则额外
    # 提供 spelling/context 通道。不存在属正常形态。
    # 同步基线从 APPDATA 读回（两种模式都要，2026-10-09 第三次丢失根因：
    # rules+PRPM 模式之前基线永远为空 → startup fetch 覆盖未推送的 trial）。
    _load_sync_baseline()
    if os.path.exists(EVIDENCE_PATH):
        _log("[main] loading words and rules...")
        try:
            load_words()
        except Exception as e:  # noqa: BLE001
            _log(f"[main] evidence load failed ({e!r}) — serving rules+PRPM only")
    else:
        _log("[main] no evidence.sqlite — rules+PRPM mode (legacy corpus channels off)")

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
        # 首次启动立即 fetch 一次：GitHub 是规则文件的唯一初始分发渠道
        # （repo/exe 不再 seed，用户裁决 2026-10-09），新机器/空 APPDATA 场景
        # 不该等满 5 分钟才拿到规则。之后按周期轮询。
        try:
            results = _gh_sync_rules()
            ok = sum(1 for v in results.values() if v.get("ok"))
            _log(f"[auto-sync] initial fetch {ok}/{len(results)} files from GitHub")
        except Exception as e:  # noqa: BLE001
            _log(f"[auto-sync] initial fetch failed: {e!r}")
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
