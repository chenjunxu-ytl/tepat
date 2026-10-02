"""Shared tokenization for building evidence and scanning; preserve raw offsets."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TOKEN = re.compile(r"[^\W\d_]+(?:[’'ʼ-][^\W\d_]+)*(?:\d+)?", re.UNICODE)
ABBREVIATIONS = {"dr", "prof", "en", "tn", "pn", "hj", "hjh", "ts", "ir", "sr", "no", "spt", "dsb", "dll"}


def normalize(word: str) -> str:
    return unicodedata.normalize("NFC", word).casefold().replace("’", "'").replace("ʼ", "'")


@dataclass(frozen=True)
class Token:
    word: str
    raw: str
    start: int
    end: int


def tokens(text: str, offset: int = 0) -> list[Token]:
    # Non-Latin scripts are reported as unsupported by the checker, never guessed.
    return [Token(normalize(m[0]), m[0], offset + m.start(), offset + m.end())
            for m in TOKEN.finditer(text)
            if all(ord(c) < 0x250 or not c.isalpha() for c in m[0])]


def sentences(text: str):
    """Yield bounded raw spans, protecting titles, initials, decimals and paragraphs."""
    start = 0
    for m in re.finditer(r"[.!?]+[\"'”’»\)\]]*(?=\s|$)|\n+", text):
        end = m.end()
        mark = m[0]
        if mark.startswith("."):
            before = text[start:m.start()]
            previous = re.search(r"([\w-]+)$", before)
            if previous and (normalize(previous[1]) in ABBREVIATIONS or len(previous[1]) == 1 and previous[1].isupper()):
                continue
            if before and before[-1].isdigit() and end < len(text) and text[end:end + 1].isdigit():
                continue
        raw = text[start:end]
        left = len(raw) - len(raw.lstrip())
        if raw.strip():
            yield start + left, start + len(raw.rstrip()), raw.strip()
        start = end
    raw = text[start:]
    if raw.strip():
        left = len(raw) - len(raw.lstrip())
        yield start + left, start + len(raw.rstrip()), raw.strip()


def token_runs(text: str, offset: int = 0):
    """Punctuation/numbers/unhandled scripts break n-grams rather than joining gaps."""
    run = []
    previous = offset
    for t in tokens(text, offset):
        gap = text[previous - offset:t.start - offset]
        if run and gap.strip():
            yield run
            run = []
        run.append(t)
        previous = t.end
    if run:
        yield run
