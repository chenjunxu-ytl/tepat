import gzip
import json
import tempfile
import unittest
from pathlib import Path

from build_evidence import build, validate_record
from evidence import EvidenceStore
from text_units import sentences, token_runs, tokens


def fixture(root):
    def write(source, destination, rows):
        directory = root / source
        directory.mkdir(exist_ok=True)
        with gzip.open(directory / (destination + '.jsonl.gz'), 'wt', encoding='utf-8') as f:
            for r in rows:
                r.update(source=source, destination=destination, action='keep', location={})
                f.write(json.dumps(r) + '\n')
    for source, role in [('dbp','dictionary_example'),('td','positive_example'),('bench','prose'),('wiki','sentence')]:
        write(source,'ngram_candidate',[
            dict(id=source+'1',document_id='one',role=role,cleaned_text='Buku itu saya baca.'),
            dict(id=source+'2',document_id='two',role=role,cleaned_text='Buku itu saya baca.'),
            dict(id=source+'3',document_id='two',role=role,cleaned_text='Saya baca buku itu.')])
    write('dbp','lexicon_candidate',[dict(id='lex'+word,document_id=word,role='lexical_headword_candidate',cleaned_text=word)
                                    for word in ['baca','doktor','muzik','mengerahkan','kahwin','erti']])
    write('dbp','reference',[dict(id='def',document_id='baca',role='semantic_definition',field='definition',headword='baca',cleaned_text='memerhatikan tulisan dengan teliti')])
    write('td','reference',[dict(id='tdref',document_id='TD',role='explanation',field='book.prose',cleaned_text='Ayat pasif mempunyai susunan yang berbeza.')])


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        fixture(cls.root)
        build(cls.root, cls.root/'evidence.sqlite', verify=False)
        cls.store = EvidenceStore(cls.root/'evidence.sqlite')

    @classmethod
    def tearDownClass(cls):
        cls.store.connection().close()
        cls.temp.cleanup()

    def test_sources_counts_and_dedup(self):
        support = self.store.support('buku itu',2)
        self.assertEqual({s['source'] for s in support},{'wiki','bench','td','dbp'})
        self.assertTrue(all(s['c']==2 and s['doc_c']==2 for s in support))
        self.assertEqual(self.store.metadata['counts']['wiki']['duplicate_sentences'],1)

    def test_dictionary_versus_observed(self):
        self.assertEqual(self.store.lexical('baca')['state'],'dictionary_attested')
        self.assertEqual(self.store.lexical('buku')['state'],'corpus_observed')
        self.assertEqual(self.store.lexical('zzzz')['state'],'unknown')
        self.assertFalse(self.store.lexical('baca')['normative_checked'])

    def test_examples_and_reference_search(self):
        self.assertTrue(self.store.lookup('baca')['definitions'])
        self.assertIn('susunan',self.store.search('ayat pasif')[0]['text'])
        support = self.store.support('buku itu',2)[0]
        self.assertEqual(self.store.example(support['example_id'])['source'],support['source'])

    def test_reject_negative_holdout_and_hansard(self):
        for change in [dict(role='negative_example'),dict(heldout=True),dict(cleaned_text='*Buku itu saya baca.')]:
            with self.assertRaises(ValueError):
                validate_record(dict(source='td',destination='ngram_candidate',action='keep',role='positive_example',cleaned_text='Buku itu saya baca.',**{k:v for k,v in change.items() if k not in {'role','cleaned_text'}}) | change,'td','ngram_candidate')
        with self.assertRaises(ValueError):
            validate_record(dict(source='hansard',destination='reference',action='keep'),'wiki','ngram_candidate')

    def test_boundaries_titles_and_digits(self):
        self.assertEqual([s[2] for s in sentences('Dr. Ahmad membaca buku. Dia datang esok.')],['Dr. Ahmad membaca buku.','Dia datang esok.'])
        self.assertEqual([[t.word for t in run] for run in token_runs('buku, itu 2026 bagus')],[['buku'],['itu'],['bagus']])
        self.assertEqual(tokens('bener2')[0].word,'bener2')
        self.assertEqual([s[2] for s in sentences('Adakah betul? Ya!')],['Adakah betul?','Ya!'])


if __name__ == '__main__':
    unittest.main()
