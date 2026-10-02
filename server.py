#!/usr/bin/env python3
"""tepat — 词存在性高亮工具 (2026-10-01).

定位（user ruling）：不做裁决，只把"怪词"高亮出来让人调查。
逻辑照搬 Anna mech_flags 的存在性检查（去掉 agent 裁决层）：

  词 → 本地词表 (puzzle ngrams2.db words 表导出) → hit = 正常
      → miss → ms/id 后缀规则变体 (mengerahken→mengerahkan 型)
      → miss → affix 剥离变体（合法屈折 → 不高亮）
      → miss → PRPM 主词典（点击时按需查，带本地 sqlite 缓存 + 1s 串行限流）
      → hit = 黄标(词典有、语料无) / miss = 红标(两边都无)
      → PRPM 不可达 = 灰标（未验证 ≠ 不存在，mech_flags 同语义）

CLI: python server.py [--port 8377] [--no-browser]
打包: PyInstaller --onefile --add-data "words.txt;." --add-data "web;web"
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

WORDS_PATH = os.path.join(_ROOT, "words.txt")
BIGRAMS_PATH = os.path.join(_ROOT, "bigrams.txt.xz")
WEB_DIR = os.path.join(_ROOT, "web")
CACHE_PATH = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"),
    "tepat", "prpm_cache.sqlite") if os.name == "nt" else \
    os.path.join(os.path.expanduser("~"), ".tepat", "prpm_cache.sqlite")

# ── 与 mech_flags 一致的规则表（ms/id 后缀 + 词缀剥离 + 碎片防护）──

FUNC = set("""yang dan atau ke dari dalam pada dengan untuk bagi kepada tentang
di atas bawah antara selepas sebelum semasa ketika itu ini mereka kami kita
saya awak dia beliau nya adalah ialah ada lah kah pun akan sudah telah sedang
belum tidak bukan jangan boleh dapat mesti harus perlu nak hendak sana sini
juga lagi memang sahaja saja kerana sebab jika kalau mungkin""".split())

WORD_RE = re.compile(r"[a-zà-öø-ÿā-žʼ'-]+")
MIN_WORD_LEN = 3

_MS_ID_SUFFIX_MAP = [("ken", "kan"), ("nye", "nya"), ("sj", "sahaja")]
_AFFIX_STRIP = [
    ("meng", "k"), ("meng", ""), ("meny", "s"), ("men", "t"),
    ("mem", "p"), ("mem", ""), ("memper", ""), ("menge", ""),
    ("ber", ""), ("ter", ""), ("di", ""), ("ke", ""), ("pe", ""),
    ("peng", "k"), ("peny", "s"), ("pen", "t"), ("pem", "p"),
]
_SUFFIX_STRIP = ["kan", "an", "i", "nya", "lah", "kah", "pun"]

# ── 词表加载 ──

_words: set[str] = set()
_bigrams: set[str] = set()
_words_lock = threading.Lock()


def load_words() -> int:
    global _words
    t0 = time.time()
    with open(WORDS_PATH, encoding="utf-8") as f:
        _words = set(w.strip() for w in f if w.strip())
    print(f"[words] loaded {len(_words)} words in {time.time() - t0:.1f}s")
    if os.path.exists(BIGRAMS_PATH):
        t0 = time.time()
        import lzma
        with lzma.open(BIGRAMS_PATH, "rt", encoding="utf-8") as f:
            _bigrams.update(l.strip() for l in f if l.strip())
        print(f"[bigrams] loaded {len(_bigrams)} bigrams "
              f"(c>=2) in {time.time() - t0:.1f}s")
    return len(_words)


def word_exists(w: str) -> bool:
    return w in _words


def bigram_exists(a: str, b: str) -> bool:
    """冷缝判据（puzzle 同语义）：a+b 搭配在语料 bigram (c>=2) 里出现过。
    c==1 的 bigram 没打包——对'搭配是否存在'它们与零频等价。"""
    return f"{a} {b}" in _bigrams


# ── 变体生成（mech_flags 同源逻辑，同步移植）──

def ms_id_variants(word: str) -> list[str]:
    out = []
    for bad, good in _MS_ID_SUFFIX_MAP:
        if word.endswith(bad) and bad != good:
            cand = word[: -len(bad)] + good
            if cand != word:
                out.append(cand)
    return out


def strip_variants(word: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for pre, restore in _AFFIX_STRIP:
        if word.startswith(pre) and len(word) > len(pre) + 1:
            base = restore + word[len(pre):]
            out.append((base, "prefix"))
            for suf in _SUFFIX_STRIP:
                if base.endswith(suf) and len(base) > len(suf) + 1:
                    out.append((base[: -len(suf)], "pref+suf"))
    for suf in _SUFFIX_STRIP:
        if word.endswith(suf) and len(word) > len(suf) + 1:
            out.append((word[: -len(suf)], "suffix"))
    seen: dict[str, str] = {}
    for v, prov in out:
        if v != word and v not in seen:
            seen[v] = prov
    return list(seen.items())


def plausible_inflection(word: str, variant: str, provenance: str) -> bool:
    if word.startswith("di") and variant == word[2:]:
        return True
    for suf in ("kan", "an", "i", "nya"):
        if word.startswith("di") and variant + suf == word[2:]:
            return True
    removed = len(word) - len(variant)
    if provenance in ("prefix", "pref+suf"):
        return 0 < removed <= 6
    return 0 < removed <= 4 and len(variant) >= 0.80 * len(word)


# ── 扫描 ──

def scan(text: str) -> dict:
    """返回高亮用结果：
    - cold_words: 每个冷词 {word, reason, suggestion}
      reason: cold(语料无) / spelling(后缀规律错，附建议)
      合法屈折（词根在语料）不返回——不高亮
    - sentences: 每句 {idx, text, cold_seams, total_seams, density}
      冷缝 = 相邻实词对的 bigram 零频（语料从未见过该搭配）。
      密度 = 冷缝/总缝；句子级怪异度信号（puzzle_scan 同语义），
      不指向具体词——人看整句。
    """
    sent_of: dict[str, int] = {}
    order: list[str] = []
    sentences_out = []
    if not _words:  # 词表未加载（standalone import 误用）→ 拒绝，防误报洪流
        raise RuntimeError("words not loaded — call load_words() first")
    for si, sent in enumerate(re.split(r"(?<=[.!?])\s+", text or "")):
        # 句内实词序列（与词级检测同口径），用于冷缝计数
        stoks = []
        for m in WORD_RE.finditer((sent or "").lower()):
            w = m.group(0).strip("-'")
            if not w or w in FUNC or len(w) < MIN_WORD_LEN:
                continue
            if w not in sent_of:
                sent_of[w] = si
                order.append(w)
            stoks.append(w)
        cold = total = 0
        if _bigrams:
            for a, b in zip(stoks, stoks[1:]):
                total += 1
                if not bigram_exists(a, b):
                    cold += 1
        sentences_out.append({
            "idx": si, "text": (sent or "")[:200],
            "cold_seams": cold, "total_seams": total,
            "density": round(cold / total, 2) if total else 0.0})
    # 词级两遍：保证 suggestion 不丢：
    results = []
    for w in order:
        if word_exists(w):
            continue
        hit = None
        for cand in ms_id_variants(w):
            if word_exists(cand):
                hit = cand
                break
        if hit:
            results.append({"word": w, "reason": "spelling",
                            "suggestion": hit, "sentence_idx": sent_of.get(w, -1)})
            continue
        root_hit = any(word_exists(v) and plausible_inflection(w, v, prov)
                       for v, prov in strip_variants(w))
        if root_hit:
            continue  # 合法屈折，不高亮
        results.append({"word": w, "reason": "cold", "suggestion": "",
                        "sentence_idx": sent_of.get(w, -1)})
    # 规则触发（rule_book 外包层）：返回每个命中的规则 + span，
    # 按 conf 分档由前端决定高亮深度（high 深标 / medium·low 选中报告）
    rule_hits = []
    for rid, conf, note, rx in _RULES_COMPILED:
        for m in rx.finditer(text or ""):
            rule_hits.append({"rule": rid, "conf": conf, "note": note,
                              "span": m.group(0), "start": m.start(), "end": m.end()})
    # blacklist bigram 命中：已确认错误搭配，深标
    bl_hits = []
    if BLACKLIST:
        toks_all = []
        for m in WORD_RE.finditer((text or "").lower()):
            w = m.group(0).strip("-'")
            if w:
                toks_all.append((w, m.start(), m.end()))
        for (a, sa, _), (b, _, eb) in zip(toks_all, toks_all[1:]):
            seam = f"{a} {b}"
            if seam in BLACKLIST:
                bl_hits.append({"bigram": seam, "start": sa, "end": eb})
    return {"cold_words": results, "total_words": len(order),
            "sentences": sentences_out,
            "rule_hits": rule_hits, "blacklist_hits": bl_hits}


# ── 规则加载（rules.json 外置数据，2026-10-01 用户裁决：规则不是代码）──
# 加载顺序：exe 旁的 rules.json（可热改，重启 exe 生效）→ 打包内置副本。
# rules.json 携带：语法正则规则 + ms/id 后缀映射 + 词缀剥离表。
# conf 档位：high=结构性错误无合法场景/深标，medium=上下文相关/中标，
#            low=宽网提醒/浅标。

RULES_PATH = os.path.join(_ROOT, "rules.json")

# 内置兜底（rules.json 缺失/损坏时用，与 rules.json 内容同源）
_DEFAULT_RULES: list[dict] = [
    {"id": "KS-01", "conf": "medium", "note": "dari + 抽象名词应为 daripada",
     "re": r"\bdari (segi|sudut|aspek|perspektif)\b"},
    {"id": "KS-01", "conf": "medium", "note": "比较级 lebih ... dari 应为 daripada",
     "re": r"\blebih [^\n]{0,40}?\bdari\b"},
    {"id": "KS-01", "conf": "high", "note": "berasal dari 应为 berasal daripada",
     "re": r"\bberasal\s+dari\b"},
    {"id": "KS-02", "conf": "medium", "note": "antara 搭配 dengan（antara X dengan Y），此处用了 dan",
     "re": r"\bantara\b[^\n]{0,80}\bdan\b"},
    {"id": "KS-03", "conf": "low", "note": "berbanding 结构需与 dengan 搭配，请检查",
     "re": r"\bberbanding\b"},
    {"id": "KS-05", "conf": "medium", "note": "dalam + 时间词通常应为 pada（pada tahun/masa/waktu/hari）",
     "re": r"\bdalam (tahun|masa|waktu|hari)\b"},
    {"id": "KP-01", "conf": "low", "note": "ialah/adalah 出现——检查主语谓语类型是否匹配",
     "re": r"\b(ialah|adalah)\b"},
    {"id": "PI-01", "conf": "low", "note": "裸动词（meN- 脱落）：guna/pakai/cari 口语可，正式文体需 men-",
     "re": r"\b(guna|pakai|cari|tanya)\b"},
    {"id": "SA-01", "conf": "low", "note": "时间状语从句前置——检查主语是否悬垂",
     "re": r"\b(setelah|selepas|semasa|ketika)\b[^.\n]{0,60},?\s+\w+\s+(ialah|adalah|merupakan)\b"},
    {"id": "KL-01", "conf": "high", "note": "双 pemeri（ialah/adalah 重复）",
     "re": r"\b(ialah|adalah)\b[^\n.]{0,30}\b(ialah|adalah)\b"},
    {"id": "KL-01", "conf": "high", "note": "双连词（walau bagaimanapun/tetapi/namun 重复）",
     "re": r"\b(walau bagaimanapun|tetapi|namun)\b[^\n.]{0,10}\b(tetapi|namun|walau bagaimanapun)\b"},
    {"id": "KL-02", "conf": "medium", "note": "量词后接复数代词（kebanyakan...mereka 冗余复数）",
     "re": r"\b(kebanyakan|sebahagian|sesetengah|beberapa)\b[^\n]{0,30}\b(kita|mereka)\b"},
    {"id": "IS-02", "conf": "medium", "note": "komprehensif 为常误用的外来词（DBP 建议 menyeluruh）",
     "re": r"\bkomprehensif\b|\bkomprehensiv\b"},
    {"id": "IS-03", "conf": "high", "note": "yang mana 作连接词是英语 which 的直译，不规范",
     "re": r"\byang mana\b"},
    {"id": "IS-03", "conf": "medium", "note": "di mana 作非处所连接词是直译，不规范",
     "re": r"\bdi mana\b"},
    {"id": "IS-04", "conf": "medium", "note": "membuat penyelidikan 不地道（应为 menjalankan/mengkaji）",
     "re": r"\bmembuat penyelidikan\b|\bmelakukan penambahbaikan\b"},
    {"id": "EJ-01", "conf": "medium", "note": "外来词拼写疑似（检查 DBP 规范形式）",
     "re": r"\b(prarontal|prefrontal|sirkadian|intravena)\b"},
]
_DEFAULT_MS_ID = [("ken", "kan"), ("nye", "nya"), ("sj", "sahaja")]
_DEFAULT_AFFIX = [
    ("meng", "k"), ("meng", ""), ("meny", "s"), ("men", "t"),
    ("mem", "p"), ("mem", ""), ("memper", ""), ("menge", ""),
    ("ber", ""), ("ter", ""), ("di", ""), ("ke", ""), ("pe", ""),
    ("peng", "k"), ("peny", "s"), ("pen", "t"), ("pem", "p"),
]
_DEFAULT_SUFFIX = ["kan", "an", "i", "nya", "lah", "kah", "pun"]


def _load_rules() -> None:
    """从 rules.json 加载规则到模块全局；失败回退内置默认并记日志。"""
    global RULES, _RULES_COMPILED, _MS_ID_SUFFIX_MAP, _AFFIX_STRIP, _SUFFIX_STRIP
    rules = _DEFAULT_RULES
    ms_id = _DEFAULT_MS_ID
    affix = _DEFAULT_AFFIX
    suffix = _DEFAULT_SUFFIX
    # exe 旁边的 rules.json 优先（热改入口）；打包内置副本次之
    for cand in (RULES_PATH,
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "rules.json")):
        try:
            with open(cand, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("rules"):
                rules = data["rules"]
                ms_id = [tuple(x) for x in data.get("ms_id_suffix_map", ms_id)]
                affix = [tuple(x) for x in data.get("affix_strip", affix)]
                suffix = data.get("suffix_strip", suffix)
                _log(f"[rules] loaded {len(rules)} rules from {cand}")
                break
        except (OSError, ValueError) as e:
            _log(f"[rules] {cand} unusable ({e}); trying fallback")
    else:
        _log("[rules] no rules.json found — using built-in defaults")
    # 正则预编译；非法正则跳过并记日志（单条坏规则不拖垮整个检查器）
    compiled = []
    for r in rules:
        try:
            compiled.append((r["id"], r["conf"], r["note"],
                             re.compile(r["re"], re.IGNORECASE)))
        except (re.error, KeyError) as e:
            _log(f"[rules] skipped bad rule {r.get('id')}: {e}")
    RULES = rules
    _RULES_COMPILED = compiled
    _MS_ID_SUFFIX_MAP = ms_id
    _AFFIX_STRIP = affix
    _SUFFIX_STRIP = suffix


RULES: list[dict] = _DEFAULT_RULES
_RULES_COMPILED = [(r["id"], r["conf"], r["note"], re.compile(r["re"], re.IGNORECASE))
                   for r in _DEFAULT_RULES]

# blacklist：已确认的错误 bigram（从 puzzle/blacklist.json 导出；命中即深标）
BLACKLIST_PATH = os.path.join(_ROOT, "blacklist.json")


def load_blacklist() -> dict[str, int]:
    try:
        with open(BLACKLIST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


BLACKLIST: dict[str, int] = load_blacklist()

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
    return con


_cache_lock = threading.Lock()
_cache: sqlite3.Connection | None = None


def cache_get_full(word: str) -> dict | None:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = _cache_con()
        row = _cache.execute(
            "SELECT status, definition FROM cache WHERE word=?", (word,)).fetchone()
        return {"status": row[0], "definition": row[1] or ""} if row else None


def cache_put(word: str, status: str, definition: str = "") -> None:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = _cache_con()
        _cache.execute("INSERT OR REPLACE INTO cache "
                       "(word, status, definition, fetched_at_ms) "
                       "VALUES (?,?,?,?)",
                       (word, status, definition, int(time.time() * 1000)))
        _cache.commit()


class PrpmUnavailable(Exception):
    pass


def prpm_fetch(url: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "tepat/1.0 (word existence lookup)"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5)
    raise PrpmUnavailable(str(last))


def _prpm_definition(page: str) -> str:
    """从 PRPM Cari1 页面提取首条释义（Kamus Dewan/Pelajar 条目文本，
    截断 300 字）。仅在真正有条目时提取，不把'Carian kata tiada...'当作释义。"""
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
        return m_def.group(1).strip()[:300]
    return td_text[:300]


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
        
        # 存在性精准判据：
        # 1. Kamus 条目区判断
        m_td = re.search(r'class="tdclass"[^>]*>(.*?)</td>', page, re.DOTALL)
        td_text = re.sub(r"<[^>]+>", " ", m_td.group(1)) if m_td else ""
        td_text = re.sub(r"\s+", " ", td_text).strip()
        has_no_kamus = ("Carian kata tiada di dalam kamus terkini" in td_text) or \
                       ("Tiada maklumat" in td_text) or len(td_text) == 0

        # 2. Tesaurus 区判断
        m_tes = re.search(r'lblTesaurus[^>]*>(.*?)</span>', page, re.DOTALL)
        tes_text = re.sub(r"<[^>]+>", " ", m_tes.group(1)) if m_tes else ""
        has_no_tesaurus = ("Tiada maklumat tesaurus" in tes_text) or len(tes_text.strip()) == 0

        # 两边都没有 → 确实无此词条 (miss)
        has_entry = not (has_no_kamus and has_no_tesaurus)
        status = "hit" if has_entry else "miss"
        definition = _prpm_definition(page) if status == "hit" else ""

        cache_put(word, status, definition)
        time.sleep(1.0)  # 串行限流：请求间 ≥1s
        return {"word": word, "status": status, "url": url,
                "definition": definition}



# ── HTTP ──

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静模式
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
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
        elif self.path == "/api/health":
            self._json(200, {"ok": True, "words": len(_words),
                             "tray": tray_icon is not None})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        if self.path == "/api/scan":
            text = str(req.get("text") or "")
            self._json(200, scan(text))
        elif self.path == "/api/prpm":
            word = str(req.get("word") or "").strip().lower()
            if not word or re.search(r"\s", word):
                self._json(400, {"error": "single word required"})
                return
            self._json(200, prpm_lookup(word))
        elif self.path == "/api/check":  # 任意词手动查：词表 + PRPM 一步到位
            word = str(req.get("word") or "").strip().lower()
            if not word or re.search(r"\s", word):
                self._json(400, {"error": "single word required"})
                return
            in_corpus = word_exists(word)
            out = {"word": word, "in_corpus": in_corpus}
            if not in_corpus:  # 词表无 → 自动带上 PRPM 结果
                out["prpm"] = prpm_lookup(word)
            self._json(200, out)
        else:
            self._json(404, {"error": "not found"})


tray_icon = None  # main() 里赋值；/api/health 暴露给 UI 探测


def main() -> int:
    ap = argparse.ArgumentParser(description="tepat - pemeriksa kewujudan kata")
    ap.add_argument("--port", type=int, default=8377)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-tray", action="store_true",
                    help="skip the system tray icon (debug)")
    args = ap.parse_args()

    if not os.path.exists(WORDS_PATH):
        print(f"words.txt not found: {WORDS_PATH}", file=sys.stderr)
        return 2

    # 单实例锁（2026-10-02 重做）：mutex 的 GetLastError 检测被实测证明不可靠
    # （两个实例并存、都完整启动、Windows 默认还允许同端口双重 bind——
    # 14:44/14:45 双实例并存即是证据）。现在以"端口连通性"为最终裁决：
    # 起服务前先连 127.0.0.1:port，连得上 = 已有实例 → 开 UI 退出。
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
        # 2026-10-02 用户裁决：拦截分支不再弹浏览器。用户双击时应只看到
        # 已有实例的托盘；页面由用户右键托盘 → Buka UI 自己开。
        return 0

    load_words()
    _load_rules()  # 规则外置数据：exe 旁 rules.json 优先，缺失回退内置

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
            while True:
                time.sleep(30)
                if stop_event.is_set():
                    return
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
    """右下角托盘图标（Win32 原生 Shell_NotifyIcon，不依赖 pystray）。
    左键单击 = 打开 UI；右键 = 菜单（Buka UI / Keluar）。
    启动即弹 balloon 通知。失败时写日志文件。"""
    try:
        import ctypes
        import ctypes.wintypes as wt
        from PIL import Image

        icon_path = os.path.join(_ROOT, "assets", "icon32.png")
        if not os.path.exists(icon_path):  # 源码目录跑（非打包）
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "assets", "icon32.png")
        _log(f"[tray] loading icon from {icon_path} (exists={os.path.exists(icon_path)})")

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        shell32 = ctypes.windll.shell32

        WM_TRAYCB = 0x7FFF  # WM_APP-ish app-defined callback
        NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
        NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
        WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
        WM_DESTROY, WM_CLOSE = 0x0002, 0x0010

        class _BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                        ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD),
                        ("Data3", wt.WORD), ("Data4", wt.BYTE * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT),
                ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
                ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", _GUID), ("hBalloonIcon", wt.HICON),
            ]

        # ── PNG → HICON：手动放 RGBA 进 DIB ──
        img = Image.open(icon_path).convert("RGBA").resize((32, 32), Image.LANCZOS)
        w, h = img.size
        px = img.tobytes()
        # BGRA bottom-up
        rows = []
        for y in range(h - 1, -1, -1):
            row = bytearray()
            for x in range(w):
                r, g, b, a = px[(y * w + x) * 4:(y * w + x) * 4 + 4]
                row += bytes((b, g, r, a))
            rows.append(bytes(row))
        bits = b"".join(rows)
        bmi = (_BITMAPINFOHEADER * 1)()
        bmi[0].biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi[0].biWidth, bmi[0].biHeight = w, h
        bmi[0].biPlanes, bmi[0].biBitCount = 1, 32
        bmi[0].biCompression = 0  # BI_RGB
        bits_buf = (ctypes.c_ubyte * len(bits)).from_buffer_copy(bits)
        hdc = user32.GetDC(None)
        color = ctypes.windll.gdi32.CreateDIBitmap(hdc, ctypes.byref(bmi), 4,  # CBM_INIT
                                        bits_buf, ctypes.byref(bmi), 0)
        mask = ctypes.windll.gdi32.CreateBitmap(w, h, 1, 1, None)
        user32.ReleaseDC(None, hdc)
        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD),
                        ("yHotspot", wt.DWORD), ("hbmMask", wt.HBITMAP),
                        ("hbmColor", wt.HBITMAP)]
        ii = ICONINFO(True, 0, 0, mask, color)
        hicon = user32.CreateIconIndirect(ctypes.byref(ii))
        if not hicon:
            raise ctypes.WinError()

        # ── 隐藏窗口 + 消息循环线程 ──
        class _TrayState:
            hwnd = None
            nid = None
            stop = False

        state = _TrayState()

        WM_APP_STOP = 0x8000 + 1
        WM_APP_MENU = 0x8000 + 2   # 由后台线程触发：在鼠标处弹右键菜单
        WM_APP_OPEN = 0x8000 + 3   # 由后台线程触发：打开 UI
        MENU_OPEN, MENU_EXIT = 100, 101
        # Explorer 重启广播（RegisterWindowMessage 每进程取一次，值进程内恒定）。
        # 注意：这个 API 在 user32 不在 shell32。
        _taskbar_created_msg = [user32.RegisterWindowMessageW("TaskbarCreated")]

        def _open_ui():
            import webbrowser
            webbrowser.open(url)

        def _popup_menu_async():
            # CreatePopupMenu/SetForegroundWindow 必须在窗口线程里调，
            # 用 PostMessage 把控制权交回消息循环所在线程。
            if state.hwnd:
                user32.PostMessageW(state.hwnd, WM_APP_MENU, 0, 0)

        @ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, wt.UINT,
                            wt.WPARAM, wt.LPARAM)
        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == _taskbar_created_msg[0] and _taskbar_created_msg[0]:
                # Explorer/任务栏重启：托盘图标被系统清掉，必须重新 NIM_ADD
                _log("[tray] TaskbarCreated received — re-adding icon")
                shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(state.nid))
                return 0
            if msg == WM_TRAYCB:
                if lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
                    # 左键/右键都弹菜单（用户裁决 2026-10-02：左键打开 UI
                    # 的行为移除，统一走菜单，与右键一致）
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
                # 菜单显示在鼠标右上角：x 右移一点，y 上移整个菜单高度
                mi = wt.RECT()
                user32.GetMenuItemRect(hwnd, menu, 0, ctypes.byref(mi))
                mh = max(mi.bottom - mi.top, 1) * 3  # 3 项（含分隔线）
                cmd = user32.TrackPopupMenu(
                    menu, 0x100,  # TPM_RETURNCMD
                    pt.x + 6, pt.y - mh - 60, 0, hwnd, None)
                user32.DestroyMenu(menu)
                if cmd == MENU_OPEN:
                    threading.Thread(target=_open_ui, daemon=True).start()
                elif cmd == MENU_EXIT:
                    state.stop = True
                    user32.DestroyWindow(hwnd)
                    # Keluar = 整个进程退出（用户裁决 2026-10-02）。
                    # 只停托盘线程会让看门狗 30 秒后把图标拉回来，
                    # 且服务在后台无图标运行——用户视角等于"退不掉"。
                    os._exit(0)
                return 0
            if msg in (WM_CLOSE, WM_APP_STOP):
                state.stop = True
                user32.DestroyWindow(hwnd)
                # WM_APP_STOP 来自 _TrayHandle.stop()（KeyboardInterrupt/finally
                # 路径），同样应该结束整个进程——托盘退出即应用退出。
                os._exit(0)
                return 0
            if msg == WM_DESTROY:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(state.nid))
                user32.PostQuitMessage(0)
                return 0
            user32.DefWindowProcW.argtypes = [
                wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_size_t]
            user32.DefWindowProcW.restype = ctypes.c_size_t
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        class _WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT),
                        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                        ("hIcon", wt.HICON), ("hCursor", ctypes.c_void_p),
                        ("hbrBackground", ctypes.c_void_p),
                        ("lpszMenuName", wt.LPCWSTR),
                        ("lpszClassName", wt.LPCWSTR),
                        ("hIconSm", wt.HICON)]

        _wnd_proc_ref = WNDPROC(wnd_proc)  # 防 GC
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = _wnd_proc_ref
        wc.lpszClassName = "tepat-tray"
        wc.hInstance = kernel32.GetModuleHandleW(None)
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise ctypes.WinError()

        def _tray_thread():
            user32.CreateWindowExW.restype = wt.HWND
            user32.CreateWindowExW.argtypes = [
                wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wt.HWND, ctypes.c_void_p, wt.HINSTANCE, ctypes.c_void_p]
            state.hwnd = user32.CreateWindowExW(
                0, wc.lpszClassName, "Tepat", 0, 0, 0, 0, 0,
                None, None, wc.hInstance, None)
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = state.hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_INFO
            nid.uCallbackMessage = WM_TRAYCB
            nid.hIcon = hicon
            nid.szTip = f"Tepat — berjalan (127.0.0.1:{port})"
            nid.szInfo = "Tepat sedang berjalan di latar belakang"
            nid.szInfoTitle = "Tepat"
            nid.uVersion = 3  # NOTIFYICON_VERSION_3
            state.nid = nid
            ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            _log(f"[tray] Shell_NotifyIcon NIM_ADD ok={ok} hwnd={state.hwnd}")
            if not ok:
                _log(f"[tray] NIM_ADD failed: {ctypes.WinError()}")
            msg = wt.MSG()
            # 消息循环自愈（2026-10-02）：GetMessageW 返回 <=0 曾导致托盘
            # 线程静默死亡（服务照活，图标消失，无人察觉——14:25 实例的死法）。
            # 现在：异常退出时记录 rc 并重建窗口+图标，最多 5 次；正常退出
            # （state.stop，用户点了 Keluar）不重建。
            rebuilds = 0
            while True:
                rc = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if rc <= 0:
                    if state.stop:
                        _log(f"[tray] message loop exited (rc={rc}, stop requested)")
                        break
                    rebuilds += 1
                    _log(f"[tray] message loop died rc={rc}, rebuild {rebuilds}/5")
                    if rebuilds > 5:
                        _log("[tray] rebuild limit reached — tray disabled")
                        break
                    state.hwnd = user32.CreateWindowExW(
                        0, wc.lpszClassName, "Tepat", 0, 0, 0, 0, 0,
                        None, None, wc.hInstance, None)
                    if not state.hwnd:
                        _log("[tray] window recreate failed")
                        time.sleep(2)
                        continue
                    nid.hWnd = state.hwnd
                    ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
                    _log(f"[tray] rebuilt window+icon ok={ok} hwnd={state.hwnd}")
                    continue
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            _log("[tray] message loop exited")

        t = threading.Thread(target=_tray_thread, daemon=True, name="tray")
        t.start()
        _log("[tray] native win32 tray thread started")

        class _TrayHandle:
            def stop(self):
                try:
                    state.stop = True
                    if state.hwnd:
                        user32.PostMessageW(state.hwnd, WM_APP_STOP, 0, 0)
                except Exception as e:  # noqa: BLE001
                    _log(f"[tray] stop failed: {e!r}")

            def alive(self) -> bool:
                """托盘窗口是否还活着（服务端看门狗用）。"""
                try:
                    return bool(state.hwnd) and bool(user32.IsWindow(state.hwnd))
                except Exception:
                    return False
        return _TrayHandle()
    except Exception as e:  # noqa: BLE001
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
    sys.exit(main())
