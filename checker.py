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
        for i,r in enumerate(data['rules']):
            if not isinstance(r,dict):
                self.config_warnings.append(f"Skipped rule {i}: object required")
                continue
            if r.get('enabled') is False:  # 用户关掉的规则（config UI toggle）
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
        self.indo_words = set(self.indo['indo_only_words'])
        self.uncertain = set(self.indo['uncertain_words'])
        self.register = self.indo.get('register_words',{})
        self.context_words = self.indo.get('context_words',{})
        self.pairs = self.indo.get('suggestions',{})

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
        def add(category, level, start, end, note, origin, *, suggestion='', evidence=None, confidence='medium', highlight=True):
            span=text[start:end]
            item={'id':hashlib.sha256(f'{origin}:{start}:{end}'.encode()).hexdigest()[:16],
                  'category':category,'level':level,'confidence':confidence,'span':span,
                  'start':utf16_offset(text,start),'end':utf16_offset(text,end),'note':note,
                  'origin':origin,'suggestion':suggestion,'evidence':evidence or [],'highlight':highlight}
            issues.append(item)
            return item
        # Rules always run, regardless of corpus/dictionary membership.
        for rule,rx,exceptions in self.rules:
            if rule.get('register','any') not in {'any',register}:
                continue
            for start,end,sentence in sentences(text):
                exclusions=[m.span() for ex in exceptions for m in ex.finditer(sentence)]
                for m in rx.finditer(sentence):
                    if any(a<=m.start() and m.end()<=b for a,b in exclusions):
                        continue
                    conf=rule.get('conf','medium')
                    item=add(rule.get('category','grammar'),{'high':'error','medium':'warning','low':'info'}[conf],
                             start+m.start(),start+m.end(),rule['note'],rule.get('entry_id',rule['id']),
                             evidence=[{'source':'manual_rule','rule':rule['id'],'basis':rule.get('source','Existing manually maintained rule')}],confidence=conf,highlight=conf!='low')
                    rule_hits.append({**item,'rule':rule['id'],'conf':conf})
        all_sentences=list(sentences(text))
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
                suggestion=self.pairs.get(t.word,'')
                if suggestion and normalize(suggestion) not in self.store.lexicon:
                    suggestion=''
                if t.word in self.register and not title and not acronym and not named:
                    if register=='formal':
                        entry=self.register[t.word]
                        item=add('register','warning',t.start,t.end,'Singkatan SMS: pertimbangkan bentuk penuh dalam penulisan formal.',
                                 'sms:'+t.word,suggestion=entry['expansion'],evidence=[entry])
                        indo_hits.append({**item,'word':t.word,'kind':'sms_abbreviation'})
                    continue
                if not title and not acronym and t.word in self.indo_words:
                    item=add('terminology','info' if named else 'warning',t.start,t.end,
                             'Bentuk calon bahasa Indonesia; semak makna, petikan dan laras sebelum menggantikannya.',
                             'indo:'+t.word,suggestion=suggestion,
                             evidence=[{'source':'indo_word_candidates','verification':'original_research_candidate','lexical':ev}],confidence='low' if named else 'medium')
                    indo_hits.append({**item,'word':t.word,'kind':'indonesian_candidate'})
                    continue
                if not title and not acronym and t.word in self.uncertain | self.context_words.keys():
                    if register=='formal' and not named:
                        item=add('terminology','info',t.start,t.end,'Penggunaan ini memerlukan konteks; asal bahasa atau kesesuaiannya belum dipastikan.',
                                 'context:'+t.word,evidence=[{'source':'indo_word_candidates','verification':'uncertain'}],confidence='low')
                        indo_hits.append({**item,'word':t.word,'kind':'context_required'})
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
