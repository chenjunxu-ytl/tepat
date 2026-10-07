"""Build an atomic evidence database from accepted cleaning exports, never raw corpus.

Usage: python build_evidence.py --review-root ../puzzle/corpus/clean-review/20261002-v3
Writes a new database; never changes puzzle/ngrams2.db or legacy checker exports.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from evidence import SCHEMA_VERSION
from text_units import sentences, token_runs

SOURCES = ("dbp", "td", "bench", "wiki")
ROLES = {"dbp": {"dictionary_example"}, "td": {"positive_example"},
         "bench": {"prose"}, "wiki": {"sentence"}}


def records(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def validate_record(r, source, destination):
    if r.get("source") != source or r.get("destination") != destination or r.get("action") not in {"keep", "modify"}:
        raise ValueError(f"Invalid candidate: {r.get('id')}")
    if destination == "ngram_candidate":
        if r.get("role") not in ROLES[source] or r.get("heldout") or r.get("cleaned_text", "").lstrip().startswith("*"):
            raise ValueError(f"Nonpositive/heldout candidate: {r.get('id')}")


def verify_review(root):
    validation = json.loads((root / "validation.json").read_text(encoding="utf-8"))
    acceptance = json.loads((root / "review-acceptance.json").read_text(encoding="utf-8"))
    if not validation.get("all_sources_full") or not acceptance.get("accepted_current_cleaning_results"):
        raise ValueError("Full accepted cleaning run required")
    for source in (*SOURCES, "hansard"):
        summary = json.loads((root / source / "summary.json").read_text(encoding="utf-8"))
        if (root / source / "RUNNING.json").exists() or summary["metadata"].get("partial") or summary["metadata"].get("partial_run"):
            raise ValueError(f"Incomplete source: {source}")
        # The accepted extraction code must still match this snapshot.
        for name, digest in summary["metadata"].get("code_sha256", {}).items():
            code = Path(__file__).resolve().parent.parent.parent / "puzzle/cleaning" / name
            if not code.exists() or hashlib.sha256(code.read_bytes()).hexdigest() != digest:
                raise ValueError(f"Changed cleaning policy: {name}")
    return validation


def build(root: Path, output: Path, *, limit=None, verify=True):
    validation = verify_review(root) if verify else {}
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_suffix(".building.sqlite")
    if staging.exists():
        raise FileExistsError(f"Unfinished build at {staging}; use a different output or inspect it first")
    started = time.monotonic()
    con = sqlite3.connect(staging)
    counts = {}
    con.executescript("""
        PRAGMA journal_mode=DELETE; PRAGMA synchronous=OFF; PRAGMA cache_size=-131072;
        CREATE TABLE metadata(k TEXT PRIMARY KEY,v TEXT NOT NULL);
        CREATE TABLE lexicon(word TEXT PRIMARY KEY,record TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE definitions(id INTEGER PRIMARY KEY,word TEXT,record TEXT NOT NULL);
        CREATE INDEX definitions_word ON definitions(word);
        CREATE TABLE examples(id INTEGER PRIMARY KEY,record_id TEXT,source TEXT,document_id TEXT,location_json TEXT,text TEXT);
        CREATE TABLE grams(n INTEGER,g TEXT,source TEXT,c INTEGER,doc_c INTEGER,example_id INTEGER,
                           PRIMARY KEY(n,g,source)) WITHOUT ROWID;
        CREATE TABLE reference(id INTEGER PRIMARY KEY,source TEXT,document_id TEXT,location_json TEXT,text TEXT,kind TEXT);
        CREATE VIRTUAL TABLE reference_fts USING fts5(text,content='reference',content_rowid='id');
    """)
    metadata = {"schema_version": SCHEMA_VERSION, "complete": False, "review_run": root.name,
                "build_scope": "full" if limit is None else "partial", "max_n": 3,
                "tokenizer_sha256": hashlib.sha256(Path(__file__).resolve().parent.parent.joinpath("text_units.py").read_bytes()).hexdigest(),
                "source_policy": "DBP/TD/Bench normative-context candidates; Wiki unverified supplemental usage; Hansard excluded",
                "authorization": "User: 同意，你可以在观察完现在代码的状态后开始落地新的检查器",
                "cleaning_validation": validation, "counts": counts}
    def save_metadata():
        con.executemany("INSERT OR REPLACE INTO metadata VALUES(?,?)",
                        [(k, json.dumps(v, ensure_ascii=False)) for k,v in metadata.items()])
        con.commit()
    save_metadata()
    try:
        for r in records(root / "dbp/lexicon_candidate.jsonl.gz"):
            validate_record(r, "dbp", "lexicon_candidate")
            con.execute("INSERT INTO lexicon VALUES(?,?)", (r["cleaned_text"], json.dumps(r, ensure_ascii=False)))
        for source in ("dbp", "td"):
            for r in records(root / source / "reference.jsonl.gz"):
                text = r["cleaned_text"].strip()
                if source == "dbp" and r["field"] == "definition":
                    con.execute("INSERT INTO definitions(word,record) VALUES(?,?)", (r["headword"], json.dumps(r, ensure_ascii=False)))
                elif source != "td" or r.get("role") in {"metadata", "unknown", "negative_example"} or not text:
                    continue
                if text:
                    con.execute("INSERT INTO reference(source,document_id,location_json,text,kind) VALUES(?,?,?,?,?)",
                                (source, r["document_id"], json.dumps(r["location"], ensure_ascii=False), text, r["role"]))
        con.execute("INSERT INTO reference_fts(reference_fts) VALUES('rebuild')")
        con.commit()
        for source in SOURCES:
            seen = set()
            doc = None
            pending = Counter()
            pending_docs = Counter()
            sample_ids = {}
            doc_grams = Counter()
            doc_samples = {}
            seen_count = accepted = duplicates = 0
            def finish_doc():
                for gram, count in doc_grams.items():
                    pending[gram] += count
                    pending_docs[gram] += 1
                    sample_ids.setdefault(gram, doc_samples[gram])
                doc_grams.clear(); doc_samples.clear()
            def flush():
                con.executemany("INSERT INTO grams VALUES(?,?,?,?,?,?) ON CONFLICT(n,g,source) DO UPDATE SET c=c+excluded.c,doc_c=doc_c+excluded.doc_c",
                                [(n,g,source,c,pending_docs[(n,g)],sample_ids[(n,g)])
                                 for (n,g),c in pending.items()])
                pending.clear(); pending_docs.clear(); sample_ids.clear()
                con.commit()
            # Accumulate occurrence + independent document support. Exact repeated
            # sentences are deduplicated within each source before counting.
            for r in records(root / source / "ngram_candidate.jsonl.gz"):
                if limit is not None and seen_count >= limit:
                    break
                validate_record(r, source, "ngram_candidate")
                seen_count += 1
                if r["document_id"] != doc:
                    finish_doc()
                    doc = r["document_id"]
                    if len(sample_ids) >= 160000:
                        flush()
                for _, _, sentence in sentences(r["cleaned_text"]):
                    digest = hashlib.sha256(sentence.encode("utf-8")).digest()
                    if digest in seen:
                        duplicates += 1
                        continue
                    seen.add(digest)
                    cur = con.execute("INSERT INTO examples(record_id,source,document_id,location_json,text) VALUES(?,?,?,?,?)",
                                      (r["id"], source, doc, json.dumps(r["location"], ensure_ascii=False), sentence))
                    accepted += 1
                    for run in token_runs(sentence):
                        words = [t.word for t in run]
                        for n in (1,2,3):
                            for i in range(len(words)-n+1):
                                gram = (n," ".join(words[i:i+n]))
                                doc_grams[gram] += 1
                                doc_samples.setdefault(gram, cur.lastrowid)
                if seen_count % 50000 == 0:
                    print(f"[{source}] {seen_count:,} records, {accepted:,} sentences, elapsed {time.monotonic()-started:.0f}s", flush=True)
            finish_doc(); flush()
            expected = validation.get("source_checks", {}).get(source, {}).get("ngram_candidate")
            if limit is None and expected is not None and seen_count != expected:
                raise ValueError(f"Source count changed: {source} {seen_count} != {expected}")
            counts[source] = {"candidate_records": seen_count, "unique_sentences": accepted, "duplicate_sentences": duplicates}
            print(f"[{source}] complete: {counts[source]}", flush=True)
        metadata["lexicon_count"] = con.execute("SELECT count(*) FROM lexicon").fetchone()[0]
        metadata["gram_counts"] = [list(r) for r in con.execute("SELECT source,n,count(*) FROM grams GROUP BY source,n")]
        metadata["elapsed_seconds"] = round(time.monotonic()-started, 2)
        metadata["complete"] = limit is None
        save_metadata()
        result = con.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise ValueError(result)
        con.close()
        if limit is None:
            staging.replace(output)
            output.with_suffix(".manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            print(f"Partial build left at {staging}; cannot be loaded by checker", flush=True)
        return metadata
    except BaseException:
        con.close()
        raise


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--review-root", type=Path, required=True)
    ap.add_argument("--output", type=Path, default=Path("data/evidence.sqlite"))
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    build(args.review_root.resolve(), args.output.resolve(), limit=args.limit)
