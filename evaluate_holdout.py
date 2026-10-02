"""Independent alert-funnel audit, not a linguistic precision/accuracy score."""
import argparse
import json
import sqlite3
import statistics
from collections import Counter
from contextlib import closing
from pathlib import Path

from checker import Checker, utf16_offset
from evidence import EvidenceStore

BASE=Path(__file__).resolve().parent
APPROVED='annotated_132_20260925.json (platform v5 delivery export)'


def evaluate(output):
    store=EvidenceStore(BASE/'data/evidence.sqlite')
    checker=Checker(store,BASE/'rules.json',BASE/'indo_words.json')
    heldout=set(json.loads((BASE.parent/'puzzle/split.json').read_text(encoding='utf-8'))['holdout'])
    counts=Counter();examples=[];clean=[];latency=[]
    with closing(sqlite3.connect((BASE.parent/'Bench/bench.db').resolve().as_uri()+'?mode=ro',uri=True)) as con:
        for tid,sr,gr in con.execute("SELECT task_id,source_record,golden_responses FROM tasks WHERE task_name LIKE 'BM_Response_Quality%'"):
            if tid not in heldout:continue
            record=json.loads(sr or '{}');golden=json.loads(gr or '[]')
            for g in golden if isinstance(golden,list) else [golden]:
                if g.get('golden_source')!=APPROVED:continue
                counts['approved_holdout_tasks']+=1
                text=record.get('response') or ''
                if len(text)>30000:counts['oversized_originals_skipped']+=1;continue
                result=checker.scan(text);latency.append(result['elapsed_ms'])
                for pair in g.get('golden_kesalahan_bahasa') or []:
                    original=pair.get('original_text') or ''
                    if not original:continue
                    at=text.casefold().find(original.casefold())
                    if at<0:counts['unaligned_error_annotations']+=1;continue
                    start,end=utf16_offset(text,at),utf16_offset(text,at+len(original))
                    hits=[i for i in result['issues'] if i['start']<end and i['end']>start]
                    counts['aligned_error_spans']+=1;counts['spans_with_any_alert']+=bool(hits)
                    counts['spans_with_warning_or_error']+=any(i['level']!='info' for i in hits)
                    if len(examples)<20:examples.append({'task_id':tid,'original':original,'kind':pair.get('kind'),
                                                        'alert_origins':[i['origin'] for i in hits]})
                corrected=g.get('golden_corrected_response') or ''
                if corrected and len(corrected)<=30000:
                    res=checker.scan(corrected);latency.append(res['elapsed_ms'])
                    clean.append({'task_id':tid,'total_signals':len(res['issues']),
                                  'levels':dict(Counter(i['level'] for i in res['issues']))})
    store.close_thread()
    report={'scope':'Alert-funnel coverage on unseen original errors and signal burden on heldout human corrections; not final verdict precision or accuracy.',
            'counts':dict(counts),'corrected_holdout_signal_burden':clean,'examples':examples,
            'latency_ms':{'median':statistics.median(latency) if latency else None,'max':max(latency) if latency else None},
            'limitations':['Small holdout; exact substring-aligned errors only; broad sentence alerts count toward funnel coverage.',
                           'Signals on corrected responses need human adjudication before being counted as false positives.',
                           'No semantic model or factuality validation.']}
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'counts':report['counts'],'latency_ms':report['latency_ms']},ensure_ascii=False))
    return report


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,default=BASE/'validation-output.json')
    evaluate(ap.parse_args().output)
