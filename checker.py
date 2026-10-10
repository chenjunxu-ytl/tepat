"""Independent rule, Indonesian/register, lexical and contextual evidence channels."""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path

from evidence import EvidenceStore
from text_units import normalize, sentences, token_runs, tokens

FUNCTION_WORDS = set('yang dan atau ke dari dalam pada dengan untuk bagi kepada di itu ini mereka kami kita saya awak dia beliau nya adalah ialah ada lah kah pun akan sudah telah sedang belum tidak bukan jangan boleh dapat mesti harus perlu juga lagi kerana jika kalau'.split())
ALPHABET = 'abcdefghijklmnopqrstuvwxyz'


def utf16_offset(text, offset):
    return len(text[:offset].encode('utf-16-le', errors='surrogatepass')) // 2


# ── Rule plugins（用户裁决 2026-10-08）─────────────────────────────────────
# regex 解决不了的结构性判断。每个 plugin 是 fn(text, sents, rule, add)，
# sents = [(start, end, sentence_text)]，add(category, level, start, end,
# note, origin, suggestion='', ...) 与 scan 内部同签名——plugin 产出与
# regex 命中同一格式，复用 flag/dedup/noflag 全套。
# 插件签名稳定、代码在 exe 里（发版节奏）；词表类参数（"params" 字段）
# 随 rules.json 走 GitHub 热更新——代码里不藏数据、不留兜底（用户裁决
# 2026-10-08）：params 缺字段就是规则条目写错了，直接抛错进
# config_warnings。未注册的 plugin 名同样只告警。


def _plugin_params(rule):
    """plugin 参数（rules.json 条目的 "params" 字段）。缺 params 或缺必需
    字段抛 ValueError——由 scan 的 plugin 调用层捕获转 config_warnings。"""
    params = rule.get('params')
    if not isinstance(params, dict):
        raise ValueError(f"plugin rule {rule.get('id', '?')} missing params object")
    return params


def _plugin_ks02_antara(text, sents, rule, add):
    """KS-02 antara X dengan/dan：数并列项。2 项 + dan → error（该用 dengan）；
    ≥3 项 + dengan → warn（通常用 dan）。数法：antara 与 dan/dengan 之间按
    逗号和 'dan' 切分并列项（最后一项前的 dan 是连接词不算项）。
    params: window（antara 后扫描窗口字符数）。"""
    params = _plugin_params(rule)
    window_n = int(params['window'])
    conf = {'high': 'error', 'medium': 'warning', 'low': 'info'}[rule.get('conf', 'medium')]
    for start, end, sent in sents:
        for m in re.finditer(r'\bantara\b', sent, re.IGNORECASE):
            window = sent[m.end():m.end() + window_n]
            stop = re.search(r'[.;:\n]|, (yang|tetapi|namun) ', window, re.IGNORECASE)
            span_txt = window[:stop.start()] if stop else window
            cj = re.search(r'\b(dan|dengan)\b', span_txt, re.IGNORECASE)
            if not cj:
                continue
            conj = cj.group(1).lower()
            head = span_txt[:cj.start()].strip()
            tail = span_txt[cj.end():].strip(' ,.')
            if not head or not tail:
                continue  # antara dan X / antara X dan（结构不完整交给 regex 通道）
            # 数 head 侧的项：逗号数 + 1；head 里的 'dan' 不应出现（出现了就是
            # 嵌套结构，保守放弃）
            if re.search(r'\bdan\b', head, re.IGNORECASE):
                continue
            n_items = head.count(',') + 2  # head + tail
            # span 覆盖从 antara 到连接词
            e_off = start + m.end() + cj.end()
            if conj == 'dan' and n_items == 2:
                add('grammar', conf, start + m.start(), e_off,
                    rule.get('note', 'antara dua item → dengan'),
                    rule.get('entry_id', rule['id']), suggestion='dengan',
                    evidence=[{'source': 'plugin', 'plugin': 'ks02_antara', 'items': n_items}],
                    confidence=rule.get('conf', 'medium'))
            elif conj == 'dengan' and n_items >= 3:
                add('grammar', 'info', start + m.start(), e_off,
                    rule.get('note_alt', 'antara ≥3 item biasanya dan'),
                    rule.get('entry_id', rule['id']),
                    evidence=[{'source': 'plugin', 'plugin': 'ks02_antara', 'items': n_items}],
                    confidence='low')


def _plugin_sa01_dangling(text, sents, rule, add):
    """SA-01 dangling modifier：句首时间从句（temporal_openers）的动作发出者
    必须是主句主语。可判的确定形态：时间从句无主语（动词/分词开头），
    主句却是 di- 被动——从句动作没有发出者可挂，错。
    params: temporal_openers（触发词表）、animate（有生命名词表，
    从句首词命中即视为有主语不判）。"""
    params = _plugin_params(rule)
    openers = tuple(params['temporal_openers'])
    animate = set(params['animate'])
    conf = {'high': 'error', 'medium': 'warning', 'low': 'info'}[rule.get('conf', 'medium')]
    for start, end, sent in sents:
        m = re.match(rf"\s*({'|'.join(openers)})\s+(\S+)", sent, re.IGNORECASE)
        if not m:
            continue
        # 大写开头 = 专名主语（Setelah Ali makan, ...）→ 不判；
        # 有生命名词表命中 = 从句有主语 → 不判
        first_word = m.group(2).lower().strip('.,;:()"\'')
        if re.match(r'[A-Z]', m.group(2)) or first_word in animate:
            continue
        comma = sent.find(',')
        if comma < 0 or comma > 90:
            continue
        main_clause = sent[comma + 1:comma + 90]
        # 主句谓语动词是 di- 被动 → dangling
        if re.search(r'\b(di\w+(?:kan|i|an)?)\b', main_clause) and re.search(
                r'\boleh\b|\bterus\b|\dibuat|\dipasang', main_clause, re.IGNORECASE):
            add('grammar', conf, start, start + comma + min(60, len(main_clause)),
                rule.get('note', 'setelah/selepas/semasa/ketika + klausa utama pasif = unsur penerang tergantung'),
                rule.get('entry_id', rule['id']),
                evidence=[{'source': 'plugin', 'plugin': 'sa01_dangling'}],
                confidence=rule.get('conf', 'medium'))


def _plugin_regex_match(text, sents, rule, add):
    """regex_match：通用正则规则执行器（用户裁决 2026-10-09：所有规则统一
    plugin 格式——regex 直判也是 plugin，pattern 随 rules.json 走 GitHub）。
    行为与旧 regex 通道逐字段一致（exceptions/noflag/register/entry_id
    origin），迁移零语义变化。
    params: pattern（必需）、exceptions（可选：例外区间正则列表）、
    suggestion（可选：替换建议）。"""
    params = _plugin_params(rule)
    rx = re.compile(params['pattern'], re.IGNORECASE)
    exceptions = [re.compile(p, re.IGNORECASE) for p in params.get('exceptions', [])]
    conf = rule.get('conf', 'medium')
    level = {'high': 'error', 'medium': 'warning', 'low': 'info'}[conf]
    for start, end, sent in sents:
        exclusions = [m.span() for ex in exceptions for m in ex.finditer(sent)]
        for m in rx.finditer(sent):
            if any(a <= m.start() and m.end() <= b for a, b in exclusions):
                continue
            add(rule.get('category', 'grammar'), level, start + m.start(), start + m.end(),
                rule.get('note', ''), rule.get('entry_id', rule['id']),
                suggestion=params.get('suggestion', ''),
                evidence=[{'source': 'manual_rule', 'rule': rule['id'],
                           'basis': rule.get('source', 'manually maintained rule')}],
                confidence=conf, highlight=conf != 'low', rule_id=rule['id'])


RULE_PLUGINS = {
    'ks02_antara': _plugin_ks02_antara,
    'sa01_dangling': _plugin_sa01_dangling,
    'regex_match': _plugin_regex_match,
}


# ── llm_judge（agent-assisted 规则，用户裁决 2026-10-10）───────────────────
# regex 表达不了的语言点（词序、句排列、跨句结构）走这里：规则条目带自然
# 语言判据（judge 描述 + trigger/clear 例句——人审过的行为规格），执行时
# 把句子交给 LLM 按判据分类。LLM 的实际调用由 server 注入（_llm_judge_fn，
# 避免checker 依赖 server；未注入=没配 key 的机器，这类规则静默跳过）。
# 判决按 (规则id+judge文本哈希, 句子) 进缓存——同句重扫零成本。LLM 说
# unsure 一律放过：宁漏勿误。
_llm_judge_fn = None        # server 注入：fn(rule_id, judge_key, sentence) -> 'trigger'|'clear'|'unsure'
_llm_judge_cache = {}       # (judge_key, sentence) -> verdict


def set_llm_judge(fn):
    """server 启动时注入 LLM 判决函数；None = 该机器没有 LLM 配置。"""
    global _llm_judge_fn
    _llm_judge_fn = fn


def _plugin_llm_judge(text, sents, rule, add):
    """llm_judge：LLM 按规则的判据分类句子。params:
      judge（必需）——自然语言判据，说明什么算错什么算对；
      例句用规则条目自己的 examples（trigger/clear），与 Rule Book 一致。
    产出：trigger 的整句标注（category/level 随规则 conf），note/suggestion
    用规则自己的字段。整句标注而非 span——LLM 定位不可信，句级锚定最稳。"""
    params = _plugin_params(rule)
    judge = str(params.get('judge') or '').strip()
    if not judge:
        raise ValueError(f"llm_judge rule {rule.get('id', '?')} missing params.judge")
    if _llm_judge_fn is None:
        return  # 机器没配 LLM——规则可用性由部署形态决定，跳过不报错
    ex = rule.get('examples') or {}
    judge_key = hashlib.sha256(
        json.dumps({'judge': judge, 'trigger': ex.get('trigger'),
                    'clear': ex.get('clear')}, ensure_ascii=False,
                   sort_keys=True).encode()).hexdigest()[:16]
    conf = rule.get('conf', 'low')
    level = {'high': 'error', 'medium': 'warning', 'low': 'info'}[conf]
    for start, end, sent in sents:
        key = (judge_key, sent)
        if key not in _llm_judge_cache:
            try:
                _llm_judge_cache[key] = _llm_judge_fn(rule.get('id', '?'), judge, sent)
            except Exception:  # noqa: BLE001 — LLM 调用失败：不确定=放过
                _llm_judge_cache[key] = 'unsure'
            # 缓存上限：判据+句子对无限增长，粗剪到最近 4k 条
            if len(_llm_judge_cache) > 4096:
                for k in list(_llm_judge_cache)[:len(_llm_judge_cache) - 4096]:
                    del _llm_judge_cache[k]
        if _llm_judge_cache[key] == 'trigger':
            add(rule.get('category', 'grammar'), level, start, end,
                rule.get('note', ''), rule.get('entry_id', rule['id']),
                suggestion=rule.get('suggestion', ''),
                evidence=[{'source': 'llm_judge', 'rule': rule['id'],
                           'basis': judge[:200]}],
                confidence=conf, highlight=conf != 'low', rule_id=rule['id'])


RULE_PLUGINS['llm_judge'] = _plugin_llm_judge


class Checker:
    def __init__(self, store: EvidenceStore, rules_path, indo_path):
        self.store = store
        self.rules_path = Path(rules_path)
        self.indo_path = Path(indo_path)
        self.reload()

    def reload(self):
        data = json.loads(self.rules_path.read_text(encoding='utf-8'))
        self.rules = []
        self.config_warnings = []
        # plugin 规则（用户裁决 2026-10-08）：regex 解决不了的结构性判断
        # （数 items、从句解析、词类一致）。rules.json 条目带 "plugin" 字段
        # 引用注册表里的函数；regex 规则照旧。老规则文件没有 plugin 字段
        # 完全不受影响；未知 plugin 名只告警不崩（老 exe 拉到新文件安全）。
        self.plugin_rules = []
        for i,r in enumerate(data['rules']):
            if not isinstance(r,dict):
                self.config_warnings.append(f"Skipped rule {i}: object required")
                continue
            if r.get('enabled') is False:  # 用户关掉的规则（config UI toggle）
                continue
            plugin_name = r.get('plugin')
            if plugin_name:
                fn = RULE_PLUGINS.get(plugin_name)
                if fn is None:
                    self.config_warnings.append(
                        f"Rule {r.get('id','?')}: unknown plugin {plugin_name!r} (skipped)")
                    continue
                self.plugin_rules.append((r, fn))
                continue
            try:
                if r.get('conf','medium') not in {'high','medium','low'} or not all(k in r for k in ('id','note','re')):
                    raise ValueError('Invalid rule shape/confidence')
                rx = re.compile(r['re'],re.IGNORECASE)
                exceptions = [re.compile(p,re.IGNORECASE) for p in r.get('exceptions',[])]
                self.rules.append((r,rx,exceptions))
            except (KeyError,re.error,ValueError,TypeError) as e:
                self.config_warnings.append(f"Skipped rule {i}: {e}")
        self.affixes = data.get('affix_strip',[])
        self.suffixes = data.get('suffix_strip',[])
        self.particles = data.get('particle_strip',[])
        self.infix_words = data.get('infix_words',{})
        self.suffix_map = data.get('ms_id_suffix_map',[])
        self.indo = json.loads(self.indo_path.read_text(encoding='utf-8'))
        # 词表三分类（用户裁决 2026-10-08）：indo_only / casual，各为
        # {词: {"ms": 标准马来文}}。旧结构（五 key 平铺）向后兼容读取——
        # 老 exe 会从 GitHub 拉到新文件，不能让它们崩。
        if 'indo_only' in self.indo and 'casual' in self.indo:
            self.indo_map = {w: v.get('ms', '') if isinstance(v, dict) else str(v)
                             for w, v in self.indo['indo_only'].items()}
            self.casual_map = {w: v.get('ms', '') if isinstance(v, dict) else str(v)
                               for w, v in self.indo['casual'].items()}
        else:
            legacy_sug = self.indo.get('suggestions', {})
            self.indo_map = {w: legacy_sug.get(w, '')
                             for w in self.indo.get('indo_only_words', [])}
            self.casual_map = {}
            for w in self.indo.get('uncertain_words', []):
                self.casual_map[w] = ''
            for w, v in self.indo.get('register_words', {}).items():
                self.casual_map[w] = v.get('expansion', '') if isinstance(v, dict) else ''
            for w in self.indo.get('context_words', {}):
                self.casual_map.setdefault(w, '')
        # 兼容别名：scan 旧分支用这些名字
        self.indo_words = set(self.indo_map)
        self.register = {w: {'expansion': ms} for w, ms in self.casual_map.items()}
        self.pairs = self.indo_map

    # TD 词素音位还原表（研究验证 2026-10-07）：(表面前缀, rest 首字母) → 词根还原段。
    # 规则属于具体前缀：men+t→tulis（还原 t）、meng+V→ambil/karang（k 可选还原）、
    # meny+s→sapu / meny+ny→nyanyi。None = 组合不合法。
    _NASAL_TABLE = {
        # meng-/peng-：g/h 原样；k 融化但借词保留 → 双解；元音 → karang 双解
        'meng': {'g': ('',), 'h': ('',), 'k': ('', 'k'),
                 'a': ('', 'k'), 'e': ('', 'k'), 'i': ('', 'k'), 'o': ('', 'k'), 'u': ('', 'k')},
        'peng': {'g': ('',), 'h': ('',), 'k': ('', 'k'),
                 'a': ('', 'k'), 'e': ('', 'k'), 'i': ('', 'k'), 'o': ('', 'k'), 'u': ('', 'k')},
        # meny-/peny-：s 融化（rest 是剥掉 s 后的形态，还原 s）；元音不再生成（meny 不接元音词根）
        'meny': {'s': ('s',), 'a': ('s',), 'e': ('s',), 'i': ('s',), 'o': ('s',), 'u': ('s',)},
        'peny': {'s': ('s',), 'a': ('s',), 'e': ('s',), 'i': ('s',), 'o': ('s',), 'u': ('s',)},
        # mem-/pem-：b/f/v 原样；p 融化但借词/极端 f 双解；元音 → tulis 型还原 p
        'mem': {'b': ('',), 'f': ('',), 'v': ('',), 'p': ('', 'p'),
                'a': ('p',), 'e': ('p',), 'i': ('p',), 'o': ('p',), 'u': ('p',)},
        'pem': {'b': ('',), 'f': ('',), 'v': ('',), 'p': ('', 'p'),
                'a': ('p',), 'e': ('p',), 'i': ('p',), 'o': ('p',), 'u': ('p',)},
        # men-/pen-：c/d/j/z 原样；t 融化但阿拉伯借词双解；元音 → tulis 型还原 t
        'men': {'c': ('',), 'd': ('',), 'j': ('',), 'z': ('',), 't': ('', 't'),
                'a': ('t',), 'e': ('t',), 'i': ('t',), 'o': ('t',), 'u': ('t',)},
        'pen': {'c': ('',), 'd': ('',), 'j': ('',), 'z': ('',), 't': ('', 't'),
                'a': ('t',), 'e': ('t',), 'i': ('t',), 'o': ('t',), 'u': ('t',)},
        # me-/pe-：l/m/n/r/w/y/ny 原样
        'me': {'l': ('',), 'm': ('',), 'n': ('',), 'r': ('',), 'w': ('',), 'y': ('',), 'ny': ('',)},
        'pe': {'l': ('',), 'm': ('',), 'n': ('',), 'r': ('',), 'w': ('',), 'y': ('',), 'ny': ('',)},
    }

    def _strip_prefix(self, base):
        """对一个 stem 生成全部 (前缀, 词根候选)。TD 词素音位规则，非法组合不出候选。"""
        out = []
        # 最长优先：memper/mempel/diper/dipel > menge/penge > meng/peng/meny/peny > mem/pem/men/pen > me/pe
        for pre in ('memper', 'mempel', 'diper', 'dipel', 'menge', 'penge'):
            if base.startswith(pre) and len(base) > len(pre) + 1:
                out.append((pre, base[len(pre):]))
        # meny/peny 特例：menyanyi 的 ny- 词根 = me + nyanyi（只剥 me）
        if base.startswith('meny') and len(base) > 6 and base[4:6] == 'ny':
            out.append(('meny', base[3:]))          # me|nyanyi → nyanyi
        if base.startswith('peny') and len(base) > 6 and base[4:6] == 'ny':
            out.append(('peny', base[3:]))          # pe|nyanyi → nyanyi
        for pre, table in self._NASAL_TABLE.items():
            if not base.startswith(pre) or len(base) <= len(pre) + 2:
                continue
            rest = base[len(pre):]
            first = 'ny' if rest.startswith('ny') else rest[:1]
            restores = table.get(first)
            if restores is None:
                continue
            for r in restores:
                out.append((pre, r + rest))
        # beR- 家族：ber/be（bel- 白名单词已提前返回）
        if base.startswith('ber') and len(base) > 5:
            out.append(('ber', base[3:]))
        if base.startswith('be') and len(base) > 4:
            rest = base[2:]
            if rest[:1] in 'aeiou':  # berenang → renang（r 还原）
                out.append(('be', 'r' + rest))
            else:                     # bekerja → kerja
                out.append(('be', rest))
        # per-（动词性/peR-）
        if base.startswith('per') and len(base) > 5:
            out.append(('per', base[3:]))
        # ter/te（terasa→rasa 由 te+r 覆盖；terbesar→besar 由 ter 覆盖）
        if base.startswith('ter') and len(base) > 5:
            out.append(('ter', base[3:]))
        if base.startswith('te') and len(base) > 4:
            out.append(('te', base[2:]))
        if base.startswith('di') and len(base) > 4:
            out.append(('di', base[2:]))
        if base.startswith('ke') and len(base) > 4:
            out.append(('ke', base[2:]))
        if base.startswith('se') and len(base) > 4:
            out.append(('se', base[2:]))
        return out

    def roots(self, word):
        """Kata dasar 候选生成（TD 完整体系，研究验证 2026-10-07）。

        剥离只产生候选，DBP 词表才有决定权：
          partikel → akhiran → awalan（+awalan 后再剥 akhiran 覆盖 apitan），
          词素音位还原按 TD 规则（men+tulis→tulis，mentadbir→tadbir 双解），
          非法组合（men+ulis 无还原）根本不出候选。
        拆烂的碎片（makan→mak）长度守卫 + 词表 gate 双重过滤，不会出现。
        """
        def known(w):  # 三态 gate：词典或语料任一确认
            return self.store.lexical(w)["state"] != "unknown"

        # 白名单：sisipan 与 bel-/pel- 词（~18 个）不可通用逆转，直接查表
        if word in self.infix_words and known(self.infix_words[word]):
            return [self.infix_words[word]]

        # 整词已知时的裸词根保护（makan 不拆 mak、terima 不拆 rima）：
        # 只作用于"前缀剥离"。后缀剥离照常（makanan→makan 是合法的 apitan 逆转，
        # 词根+后缀的碎片是自由形式）。危险前缀 ter/di/ke/se/per 中，te/be/per
        # 的脱落形（terasa→te+rasa、bekerja→be+kerja）例外——**整词是词典词**且
        # 还原出词典词时放行（terima 是 corpus-only，不给 te+rima）。
        word_known = word == normalize(word) and self.store.lexical(word)["state"] != "unknown"
        word_dict = word == normalize(word) and word in self.store.lexicon
        prefix_block = False
        prefix_allow = None
        corpus_whole = False
        if word_known and not word.startswith(('memper', 'mempel', 'diper', 'dipel',
                                               'menge', 'penge', 'meng', 'peng',
                                               'meny', 'peny', 'mem', 'pem', 'men', 'pen',
                                               'me', 'pe', 'ber', 'bel')):
            if word.startswith(('ter', 'di', 'ke', 'se', 'per', 'be', 'te')):
                prefix_block = True
                if word_dict:
                    allow = set()
                    for pre, root in self._strip_prefix(word):
                        if pre in ('te', 'be', 'per'):
                            # bekerja→kerja（kerja corpus 高频）：词典或高频都放行
                            if root in self.store.lexicon or sum(
                                    r['c'] for r in self.store.support(root)) >= 200:
                                allow.add(root)
                    if allow:
                        prefix_allow = allow
                        prefix_block = False
            else:
                # makan/terima 这类非危险前缀的已知词：前缀不剥；
                # corpus-only 整词（makan）连后缀碎片也只收词典词（mak 是 corpus 碎片 → 拦）
                prefix_block = 'nonrisky'
                corpus_whole = not word_dict
        if prefix_allow:
            return sorted(prefix_allow)

        candidates = set()
        # ① 剥 partikel（-nya/-lah/-kah/-tah/-pun/-ku/-mu）；剥后的 stem 直接算候选
        #    （makanlah→makan、adakah→ada——partikel 不改变词根，绕过 corpus_whole
        #    收窄：它剥的是附着成分，不是词根碎片）
        stems = {word}
        particle_stems = set()
        for p in self.particles:
            if word.endswith(p) and len(word) > len(p) + 2:
                stem = word[:-len(p)]
                stems.add(stem)
                if stem != word:
                    particle_stems.add(stem)
                    candidates.add(stem)
        # ② 剥 akhiran（-kan/-an/-i）；剥后 stem 也是候选（makanan→makan）
        bases = set()
        for base in stems:
            bases.add(base)
            for s in self.suffixes:
                if base.endswith(s) and len(base) > len(s) + 2:
                    stripped = base[:-len(s)]
                    bases.add(stripped)
                    if stripped != word:
                        candidates.add(stripped)  # 纯后缀词根（makanan→makan）
        # ③ 剥 awalan（词素音位规则）+ awalan 后再剥 akhiran（menggunakan→guna）
        # 已知词的裸词根保护：prefix_block 时跳过前缀剥离（makan 不剥 ma/mek）
        if prefix_block:
            for base in bases:
                if base == word or base in stems:
                    continue  # 整词/词干的前缀不剥；② 已产出后缀候选
                for _pre, root in self._strip_prefix(base):
                    if root != word:
                        candidates.add(root)
        else:
            for base in bases:
                for pre, root in self._strip_prefix(base):
                    if root != word:
                        candidates.add(root)
                    for s in self.suffixes:
                        if root.endswith(s) and len(root) > len(s) + 2:
                            deeper = root[:-len(s)]
                            if deeper != word:
                                candidates.add(deeper)
        candidates.discard(word)
        # gate：词典词直接收；corpus 词根要高频（makan 4678 过，mukul 9 拦）。
        # corpus-only 整词（makan）：后缀碎片只收词典词（mak 是 corpus 碎片 → 拦）；
        # partikel stem（makanlah→makan）不受此限——剥的是附着成分，词根完整。
        passed = []
        for w in sorted(candidates):
            if w in self.store.lexicon:
                passed.append(w)
            elif w in particle_stems or not corpus_whole:
                support = self.store.support(w)
                if sum(r['c'] for r in support) >= 200:
                    passed.append(w)
        # 深根优先：候选本身还能剥出另一个已过关候选的（gunakan=guna+kan、
        # penulis=pen+tulis、diberi=di+beri 都是中间形态）让位给更深词根。
        keep = []
        for w in passed:
            deeper = False
            for s in self.suffixes:
                if w.endswith(s) and w[:-len(s)] in passed:
                    deeper = True
            for pre, root in self._strip_prefix(w):
                if root in passed or root != w and any(
                        root.endswith(s) and root[:-len(s)] in passed
                        for s in self.suffixes):
                    deeper = True
            if not deeper:
                keep.append(w)
        return keep

    def suggestions(self, word):
        if len(word)>28:
            return []
        candidates = set()
        for i in range(len(word)+1):
            left,right=word[:i],word[i:]
            if right:
                candidates.add(left+right[1:])
                candidates.update(left+c+right[1:] for c in ALPHABET)
            if len(right)>1:
                candidates.add(left+right[1]+right[0]+right[2:])
            candidates.update(left+c+right for c in ALPHABET)
        matches = candidates & self.store.lexicon
        matches.discard(word)
        ranked = sorted(matches,key=lambda w:(-sum(s['doc_c'] for s in self.store.support(w) if s['source']!='wiki'),w))
        return ranked[:3]

    def scan(self, text: str, register: str = 'formal') -> dict:
        if register not in {'formal','informal'}:
            raise ValueError('register must be formal or informal')
        if len(text)>30000:
            raise ValueError('Maximum 30,000 characters per scan')
        started=time.monotonic()
        issues=[]; lexical=[]; sent_results=[]; rule_hits=[]; indo_hits=[]; cold=[]
        def add(category, level, start, end, note, origin, *, suggestion='', evidence=None,
                confidence='medium', highlight=True, rule_id=None):
            span=text[start:end]
            item={'id':hashlib.sha256(f'{origin}:{start}:{end}'.encode()).hexdigest()[:16],
                  'category':category,'level':level,'confidence':confidence,'span':span,
                  'start':utf16_offset(text,start),'end':utf16_offset(text,end),'note':note,
                  'origin':origin,'suggestion':suggestion,'evidence':evidence or [],'highlight':highlight}
            issues.append(item)
            if rule_id:  # plugin 命中记入 rule_hits——同一对象引用：抑制/dedup
                # 循环用 `is` 对照 issues 条目，浅拷贝会让删除永远失配
                item['rule'] = rule_id
                item['conf'] = confidence
                rule_hits.append(item)
            return item
        # Rules always run, regardless of corpus/dictionary membership.
        # 全部规则走 plugin 通道（regex 也由 regex_match 执行器跑，用户裁决
        # 2026-10-09）。noflag 例外条目先跑：命中区间（按 entry 区分）压制
        # "其他条目"的重叠 finding；例外条目自身不产出 finding。
        all_sentences=list(sentences(text))
        noflag_spans=[]
        for rule,fn in self.plugin_rules:
            if rule.get('register','any') not in {'any',register} or not rule.get('noflag'):
                continue
            before=len(issues)
            try:
                fn(text, all_sentences, rule, add)
            except Exception as e:  # noqa: BLE001 — plugin 崩不能带垮整个 scan
                self.config_warnings.append(f"plugin {rule.get('plugin')}: {e!r}")
            # 命中只留作例外区间；issues/rule_hits 里的痕迹全部撤掉
            for item in issues[before:]:
                noflag_spans.append((id(rule),item['start'],item['end']))
            del issues[before:]
            del rule_hits[before:]
        for rule,fn in self.plugin_rules:
            if rule.get('register','any') not in {'any',register} or rule.get('noflag'):
                continue
            before=len(rule_hits)
            try:
                fn(text, all_sentences, rule, add)
            except Exception as e:  # noqa: BLE001 — plugin 崩不能带垮整个 scan
                self.config_warnings.append(f"plugin {rule.get('plugin')}: {e!r}")
                continue
            # noflag 例外区间抑制本条目的重叠命中（区分 entry：自己的例外自己不压）
            for h in list(rule_hits[before:]):
                if any(rid is not id(rule) and h['start']<x_e and x_s<h['end']
                       for rid,x_s,x_e in noflag_spans):
                    issues[:] = [i for i in issues if i is not h]
                    rule_hits.remove(h)
        # 同 ID 去重（Rule Book 并入后）：粗规则（origin 形如 KS-01-02）与精化版
        # （origin 就是规则 ID）命中重叠区间时，只留精化版。add() 已把粗规则条目
        # 塞进 issues——这里同步从 issues 里摘掉被精化版覆盖的粗条目。
        coarse_pat=re.compile(r'^[A-Z]{2}-\d+-\d+$')
        refined=[h for h in rule_hits if not coarse_pat.match(str(h.get('origin','')))]
        dropped=[c for c in rule_hits if coarse_pat.match(str(c.get('origin','')))
                 and any(r['rule']==c['rule'] and r['start']<c['end'] and c['start']<r['end']
                         for r in refined)]
        rule_hits=[h for h in rule_hits if h not in dropped]
        dropped_keys={(d['origin'],d['start'],d['end']) for d in dropped}
        issues[:]=[i for i in issues
                   if (i['origin'],i['start'],i['end']) not in dropped_keys]
        for si,(start,end,sentence) in enumerate(all_sentences):
            stokens=tokens(sentence,start)
            for t in stokens:
                ev=self.store.lexical(t.word)
                ev.update(start=utf16_offset(text,t.start),end=utf16_offset(text,t.end),sentence_idx=si)
                lexical.append(ev)
                # Titles/initials/acronyms must retain original case and punctuation.
                title=t.raw=='Dr' and text[t.end:t.end+1]=='.'
                acronym=t.raw.isupper() and len(t.raw)>1
                named=t.raw[:1].isupper() and t.start!=start
                if not title and not acronym and t.word in self.indo_map:
                    # 印尼语独有词：terminology 通道报（mapping 有则作为建议）
                    suggestion=self.indo_map.get(t.word,'')
                    if suggestion and normalize(suggestion) not in self.store.lexicon:
                        suggestion=''
                    item=add('terminology','info' if named else 'warning',t.start,t.end,
                             'Bentuk calon bahasa Indonesia; semak makna, petikan dan laras sebelum menggantikannya.',
                             'indo:'+t.word,suggestion=suggestion,
                             evidence=[{'source':'indo_word_candidates','verification':'original_research_candidate','lexical':ev}],confidence='low' if named else 'medium')
                    indo_hits.append({**item,'word':t.word,'kind':'indonesian_candidate'})
                    continue
                if not title and not acronym and not named and t.word in self.casual_map:
                    # casual（用户裁决 2026-10-08：register/uncertain/context 三分支
                    # 合一）：口语/非正式词，formal 体裁下提醒换成标准形（ms mapping）。
                    if register=='formal':
                        ms=self.casual_map.get(t.word,'')
                        note=('Bentuk tidak formal; pertimbangkan bentuk standard dalam penulisan formal.'
                              if ms else
                              'Penggunaan ini memerlukan konteks; asal bahasa atau kesesuaiannya belum dipastikan.')
                        item=add('register','warning' if ms else 'info',t.start,t.end,note,
                                 'casual:'+t.word,suggestion=ms,
                                 evidence=[{'source':'casual_words','ms':ms}],confidence='low')
                        indo_hits.append({**item,'word':t.word,'kind':'casual'})
                    continue
                if ev['state']!='unknown' or t.word in FUNCTION_WORDS or len(t.word)<3 or title or acronym:
                    continue
                roots=self.roots(t.word)
                suggestions=[]
                for bad,good in self.suffix_map:
                    if t.word.endswith(bad):
                        cand=t.word[:-len(bad)]+good
                        if cand in self.store.lexicon or any(s['source']!='wiki' for s in self.store.support(cand)):
                            suggestions.append(cand)
                if not suggestions and not named and not roots:
                    suggestions=self.suggestions(t.word)
                level='info' if named or roots else 'warning'
                note=('Bentuk berimbuhan belum disahkan; akar kata ditemui dalam DBP.' if roots else
                      'Nama atau bentuk ini belum ditemui; semak PRPM atau sumber asal.' if named else
                      'Bentuk ini belum ditemui dalam bukti tempatan; ketiadaan bukan bukti salah ejaan.')
                item=add('spelling',level,t.start,t.end,note,'lexical:'+t.word,suggestion=suggestions[0] if suggestions else '',
                         evidence=[ev,{'root_candidates':roots,'suggestions':suggestions}],confidence='low')
                cold.append({**item,'word':t.word,'reason':'spelling' if suggestions else 'cold','sentence_idx':si})
            details=[];total=cold_count=core_count=wiki_only_count=0
            for run in token_runs(sentence,start):
                for n in (2,3):
                    for i in range(len(run)-n+1):
                        gram=' '.join(t.word for t in run[i:i+n])
                        support=list(self.store.support(gram,n))
                        if n==2:
                            total+=1; cold_count+=not bool(support)
                            core = any(s['source'] != 'wiki' for s in support)
                            core_count += core
                            wiki_only_count += bool(support) and not core
                        if len(details)<32:
                            state = 'core_observed' if any(s['source'] != 'wiki' for s in support) else 'wiki_observed' if support else 'unseen'
                            details.append({'n':n,'gram':gram,'sources':support,'state':state})
            density=round(cold_count/total,3) if total else None
            needs_context=total>=3 and density is not None and density>=0.6
            sent_results.append({'idx':si,'text':sentence,'start':utf16_offset(text,start),'end':utf16_offset(text,end),
                                 'cold_seams':cold_count,'total_seams':total,'density':density,
                                 'core_supported_seams':core_count,'wiki_only_seams':wiki_only_count,
                                 'needs_context':needs_context,'grammar_status':'partial_rules_and_usage_only','evidence':details})
            if needs_context:
                add('grammar','info',start,end,'Banyak pasangan belum ditemui; semak struktur atau istilah. Ini bukan keputusan tatabahasa.',
                    'context_sentence:'+str(si),evidence=details[:8],confidence='low',highlight=False)
        unsupported=bool(re.search(r'[\u0600-\u06ff\u3400-\u9fff]',text))
        coverage={'spelling':'dictionary_and_usage_partial','terminology':'candidate_list_and_dictionary_partial',
                  'grammar':'manual_rules_and_local_context_partial','factuality':'not_checked',
                  'unsupported_script':unsupported,'register':register}
        return {'engine':'evidence-v2','issues':sorted(issues,key=lambda x:(x['start'],x['end'],x['origin'])),
                'lexical_evidence':lexical,'cold_words':cold,'rule_hits':rule_hits,'indo_hits':indo_hits,
                # Compatibility field: legacy sentence-derived bigrams are no longer alerts.
                'blacklist_hits':[],'sentences':sent_results,'total_words':len(lexical),
                'coverage':coverage,'config_warnings':self.config_warnings,
                'elapsed_ms':round((time.monotonic()-started)*1000,1)}
