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
        self.suffix_map = data.get('ms_id_suffix_map',[])
        self.indo = json.loads(self.indo_path.read_text(encoding='utf-8'))
        self.indo_words = set(self.indo['indo_only_words'])
        self.uncertain = set(self.indo['uncertain_words'])
        self.register = self.indo.get('register_words',{})
        self.context_words = self.indo.get('context_words',{})
        self.pairs = self.indo.get('suggestions',{})

    def roots(self, word):
        candidates = set()
        for pre,restore in self.affixes:
            if word.startswith(pre) and len(word)>len(pre)+2:
                base = restore+word[len(pre):]
                candidates.add(base)
                for suffix in self.suffixes:
                    if base.endswith(suffix) and len(base)>len(suffix)+2:
                        candidates.add(base[:-len(suffix)])
        for suffix in self.suffixes:
            if word.endswith(suffix) and len(word)>len(suffix)+2:
                candidates.add(word[:-len(suffix)])
        return sorted(w for w in candidates if w in self.store.lexicon and w!=word)

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
