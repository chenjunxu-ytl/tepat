"""Read-only, source-separated evidence. Occurrence never certifies correctness."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from text_units import normalize, tokens

SCHEMA_VERSION = 1


class EvidenceStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.local = threading.local()
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as con:
            self.metadata = {k: json.loads(v) for k, v in con.execute("SELECT k,v FROM metadata")}
            if self.metadata.get("schema_version") != SCHEMA_VERSION or not self.metadata.get("complete"):
                raise ValueError("Evidence database incomplete or unsupported")
            self.lexicon = {w for w, in con.execute("SELECT word FROM lexicon")}

    def connection(self):
        if not getattr(self.local, "con", None):
            self.local.con = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
            self.local.con.row_factory = sqlite3.Row
        return self.local.con

    def close_thread(self):
        con = getattr(self.local, "con", None)
        if con is not None:
            con.close()
            self.local.con = None

    @lru_cache(maxsize=24000)
    def support(self, phrase: str, n: int = 1) -> tuple:
        return tuple(dict(r) for r in self.connection().execute(
            "SELECT source,c,doc_c,example_id FROM grams WHERE n=? AND g=?", (n, phrase)))

    def lexical(self, word: str) -> dict:
        word = normalize(word)
        support = self.support(word)
        state = "dictionary_attested" if word in self.lexicon else "corpus_observed" if support else "unknown"
        return {"word": word, "state": state, "normative_checked": False,
                "sources": list(support)}

    def example(self, example_id: int) -> dict | None:
        row = self.connection().execute("SELECT * FROM examples WHERE id=?", (example_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["location"] = json.loads(out.pop("location_json"))
        return out

    def lookup(self, word: str) -> dict:
        word = normalize(word)
        row = self.connection().execute("SELECT record FROM lexicon WHERE word=?", (word,)).fetchone()
        definitions = [json.loads(r[0]) for r in self.connection().execute(
            "SELECT record FROM definitions WHERE word=? ORDER BY id LIMIT 8", (word,))]
        lexical = self.lexical(word)
        return {**lexical, "dictionary": json.loads(row[0]) if row else None,
                "definitions": definitions,
                "examples": [e for s in lexical["sources"] if (e := self.example(s["example_id"]))]}

    def search(self, query: str, limit: int = 8) -> list[dict]:
        terms = [t.word for t in tokens(query)][:8]
        if not terms:
            return []
        expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        rows = self.connection().execute(
            "SELECT r.* FROM reference_fts f JOIN reference r ON r.id=f.rowid "
            "WHERE reference_fts MATCH ? ORDER BY bm25(reference_fts) LIMIT ?", (expression, limit))
        return [{**dict(r), "location": json.loads(r["location_json"])} for r in rows]
