import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer

import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parent.parent, _Path(__file__).resolve().parent.parent / "tools"):
    if str(_p) not in _sys.path: _sys.path.insert(0, str(_p))
from build_evidence import build
from checker import Checker
from evidence import EvidenceStore
from test_evidence import fixture
import server

BASE=Path(__file__).resolve().parent.parent  # 项目根（本文件在 tests/）


class CheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        fixture(cls.root);build(cls.root,cls.root/'evidence.sqlite',verify=False)
        cls.store=EvidenceStore(cls.root/'evidence.sqlite')
        cls.checker=Checker(cls.store,BASE/'rules.json',BASE/'indo_words.json')

    @classmethod
    def tearDownClass(cls):
        cls.store.connection().close();cls.temp.cleanup()

    def test_indonesian_list_does_not_depend_on_corpus_or_word_length(self):
        r=self.checker.scan('dokter datang esok. gw baca buku.')
        hits={h['word']:h for h in r['indo_hits']}
        self.assertEqual(hits['dokter']['suggestion'],'doktor')
        self.assertIn('gw',hits)
        self.assertEqual(hits['dokter']['level'],'warning')

    def test_shared_abbreviations_formal_only_and_titles(self):
        r=self.checker.scan('Dr. Ali datang dgn buku yg bagus.')
        self.assertEqual({h['word'] for h in r['indo_hits'] if h['kind']=='casual'},{'dgn','yg'})
        self.assertEqual({h['word'] for h in r['indo_hits'] if h['category']=='register'},{'dgn','yg'})
        self.assertFalse(any(h['word']=='dr' for h in r['indo_hits']))
        r=self.checker.scan('dgn buku yg bagus.',register='informal')
        self.assertFalse(r['indo_hits'])

    def test_digit_slang_and_uncertain_is_not_an_error(self):
        r=self.checker.scan('bener2 bagus. kualitas baik.')
        self.assertTrue(any(h['word']=='bener2' for h in r['indo_hits']))
        # kualitas 是 casual 且有 mapping（kualiti）→ formal 下 warning + 建议换写
        hit=next(h for h in r['indo_hits'] if h['word']=='kualitas')
        self.assertEqual(hit['kind'],'casual')
        self.assertEqual(hit['suggestion'],'kualiti')

    def test_rule_runs_on_attested_words_and_separate_clauses(self):
        # rules.json 只留 Rule Book 产物（RB-*）；regex 规则机制用注入验证
        config=self.root/'rules.json'
        config.write_text(json.dumps({'rules':[
            {'id':'KL-TEST','plugin':'regex_match','conf':'high','note':'pemeri kembar',
             'params':{'pattern':r'\b(ialah\s+ialah|adalah\s+adalah|ialah\s+adalah|adalah\s+ialah)\b'}}]}),encoding='utf-8')
        c=Checker(self.store,config,BASE/'indo_words.json')
        r=c.scan('Ali ialah adalah doktor.')
        self.assertTrue(any(h['conf']=='high' for h in r['rule_hits']))
        r=c.scan('Ali ialah doktor dan Abu adalah ketua.')
        self.assertFalse(any(h['conf']=='high' for h in r['rule_hits']))

    def test_sentence_derived_blacklist_cannot_create_grammar_alerts(self):
        # These survived unchanged in corrections, or changed only with context.
        # Their presence in an old sentence annotation is not a reusable rule.
        phrases=('lebih mudah','sebenarnya ialah','yang laju')
        for phrase in phrases:
            r=self.checker.scan(phrase+'.')
            self.assertFalse(r['blacklist_hits'])
            self.assertFalse(any(i['category']=='grammar' and i['level'] in {'error','warning'} for i in r['issues']))
        legacy=json.loads((BASE/'blacklist.json').read_text(encoding='utf-8'))
        self.assertTrue(set(phrases)<=legacy.keys())
        r=self.checker.scan('. '.join(legacy)+'.')
        self.assertFalse(any(i['origin'].startswith('legacy_bigram:') for i in r['issues']))

    def test_context_reminders_stay_visible_without_inline_error_marks(self):
        # low-conf 规则提醒不内联高亮——用注入的 RB-REMIND 验证（rules.json
        # 只留 Rule Book 产物，不再有内置 low 规则）
        config=self.root/'rules.json'
        config.write_text(json.dumps({'rules':[
            {'id':'RB-REMIND','plugin':'regex_match','conf':'low','note':'pemeri reminder',
             'params':{'pattern':r'\bialah\b'}}]}),encoding='utf-8')
        c=Checker(self.store,config,BASE/'indo_words.json')
        r=c.scan('Ali ialah doktor.')
        reminders=[i for i in r['rule_hits'] if i['conf']=='low']
        self.assertTrue(reminders)
        self.assertTrue(all(i['highlight'] is False for i in reminders))
        r=c.scan('qzxv qzxy qzxz qzxw.')
        context=[i for i in r['issues'] if i['origin'].startswith('context_sentence:')]
        self.assertTrue(context)
        self.assertTrue(all(i['highlight'] is False for i in context))

    def test_rule_matches_do_not_cross_sentence_boundary(self):
        config=self.root/'rules.json'
        config.write_text(json.dumps({'rules':[
            {'id':'RB-BOUND','plugin':'regex_match','conf':'high','note':'boundary',
             'params':{'pattern':r'\b(ialah\s+ialah|adalah\s+adalah)\b'}}]}),encoding='utf-8')
        c=Checker(self.store,config,BASE/'indo_words.json')
        r=c.scan('Ali ialah doktor. Abu adalah ketua.')
        self.assertFalse(any(h['conf']=='high' for h in r['rule_hits']))

    def test_function_words_and_single_count_evidence_retained(self):
        r=self.checker.scan('Buku itu saya baca.')
        grams=r['sentences'][0]['evidence']
        self.assertTrue(any(g['gram']=='itu saya' and g['sources'] for g in grams))
        self.assertEqual(r['sentences'][0]['total_seams'],3)
        self.assertTrue(any(g['n']==3 for g in grams))

    def test_exact_utf16_offsets(self):
        text='😀 dokter datang. doktor datang.'
        r=self.checker.scan(text)
        hits=[h for h in r['indo_hits'] if h['word']=='dokter']
        self.assertEqual(len(hits),1)
        self.assertEqual((hits[0]['start'],hits[0]['end']),(3,9))
        self.assertEqual(hits[0]['span'],'dokter')

    def test_unknown_and_root_are_not_automatic_errors(self):
        r=self.checker.scan('membacakan zzzzzz.')
        self.assertTrue(any(c['word']=='membacakan' and c['level']=='info' for c in r['cold_words']))
        self.assertFalse(any(c['level']=='error' for c in r['cold_words']))

    def test_coverage_and_limits(self):
        r=self.checker.scan('باچ buku.')
        self.assertTrue(r['coverage']['unsupported_script'])
        self.assertEqual(r['coverage']['factuality'],'not_checked')
        for text,register in [('a'*30001,'formal'),('buku','bad')]:
            with self.assertRaises(ValueError):self.checker.scan(text,register)

    def test_config_bad_regex_is_reported_and_exception_applied(self):
        config=self.root/'rules.json'
        config.write_text(json.dumps({'rules':[
            {'id':'bad','plugin':'regex_match','note':'bad','params':{'pattern':'['}},
            {'id':'custom','plugin':'regex_match','conf':'high','note':'test',
             'params':{'pattern':'buku','exceptions':['buku itu']}}]}),encoding='utf-8')
        c=Checker(self.store,config,BASE/'indo_words.json')
        r=c.scan('buku itu. buku ini.')
        self.assertTrue(r['config_warnings'])
        self.assertEqual(len(r['rule_hits']),1)

    def test_http_contracts(self):
        previous=server._checker;server._checker=self.checker
        service=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=service.serve_forever,daemon=True);thread.start()
        origin=f'http://127.0.0.1:{service.server_port}'
        def post(path,data):
            req=urllib.request.Request(origin+path,json.dumps(data).encode(),{'Content-Type':'application/json'})
            with urllib.request.urlopen(req) as response:return json.load(response)
        try:
            self.assertEqual(post('/api/scan',{'text':'dokter datang.'})['engine'],'evidence-v2')
            self.assertEqual(post('/api/scan',{'text':'\ud83d dokter datang.'})['engine'],'evidence-v2')
            self.assertTrue(post('/api/evidence',{'query':'baca'})['lexical']['definitions'])
            for data in [[],{'text':'a'*30001},{'text':'buku','register':'bad'}]:
                with self.assertRaises(urllib.error.HTTPError) as error:post('/api/scan',data)
                self.assertEqual(error.exception.code,400)
        finally:
            service.shutdown();service.server_close();thread.join();server._checker=previous


class PrpmTests(unittest.TestCase):
    def test_explicit_miss_versus_unverified_page(self):
        self.assertEqual(server.parse_prpm('<td class="tdclass">Carian kata tiada di dalam kamus terkini</td>')['status'],'miss')
        for text in ['<html>captcha</html>','<td class="tdclass">Please retry</td>','<td class="tdclass">Definisi : </td>']:
            self.assertEqual(server.parse_prpm(text)['status'],'unreachable')
        hit=server.parse_prpm("<td class='tdclass'><b>Definisi :</b> membaca &amp; melihat</td>")
        self.assertEqual(hit['status'],'hit');self.assertIn('&',hit['definition'])

    def test_legacy_cache_is_invalidated_and_new_cache_expires(self):
        with tempfile.TemporaryDirectory() as temp:
            path=str(Path(temp)/'cache.sqlite');prev_path=server.CACHE_PATH;prev_cache=server._cache
            con=sqlite3.connect(path);con.execute('CREATE TABLE cache(word TEXT PRIMARY KEY,status TEXT,definition TEXT,fetched_at_ms INTEGER)');con.execute("INSERT INTO cache VALUES('x','miss','',1)");con.commit();con.close()
            server.CACHE_PATH=path;server._cache=None
            try:
                self.assertIsNone(server.cache_get_full('x'))
                server.cache_put('x','hit','valid definition')
                self.assertEqual(server.cache_get_full('x')['status'],'hit')
                server._cache.execute("UPDATE cache SET fetched_at_ms=1");server._cache.commit()
                self.assertIsNone(server.cache_get_full('x'))
            finally:
                server._cache.close();server._cache=prev_cache;server.CACHE_PATH=prev_path


if __name__=='__main__':unittest.main()
