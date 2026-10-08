"""Validate readable classifications, then generate the shipped word policy.

新结构（用户裁决 2026-10-08）输出三分类：indo_only / casual，各为
{词: {"ms": 标准马来文}}。旧 puzzle 源（md + json）仍是人工维护的 readable
分类，这里读取它 + 本仓库的 ms mapping 合成 shipped 文件。

mapping 来源说明：uncertain/context/register 的 ms mapping 是 2026-10-08
研究产物（已直接并入本仓库 indo_words.json 的 casual）；puzzle 源不含
mapping 字段，所以本脚本以仓库现文件为 mapping 真源、puzzle 为词集真源。
"""
import hashlib
import json
import re
from pathlib import Path


def sync(source_root, output, repo_words=None):
    """repo_words：仓库现有 indo_words.json（mapping 真源）。None 时读 output。"""
    markdown = source_root / 'indo_blacklist.md'
    source = source_root / 'indo_blacklist.json'
    text = markdown.read_text(encoding='utf-8')
    data = json.loads(source.read_text(encoding='utf-8'))
    if repo_words is None:
        repo_words = json.loads(output.read_text(encoding='utf-8'))
    old_casual = repo_words.get('casual', {})
    old_indo_ms = {w: v.get('ms', '') for w, v in repo_words.get('indo_only', {}).items()}

    def words(begin, end):
        return set(re.findall(r'`([^`]+)`', text.split(begin, 1)[1].split(end, 1)[0]))

    # 词集从 puzzle 源校验
    if words('## 印尼语候选', '## Register') != set(data['indo_only_words']):
        raise ValueError('Markdown/JSON Indonesian candidates differ')

    # 旧五分类 → 新三分类。puzzle 源是"人工分类词集"的真源，但 2026-10-08
    # 研究新增的 casual 词（x/nak/gi 这类 66 个）只存在于仓库文件——
    # sync 不得丢掉它们：仓库 casual 里不在 puzzle 三组里的词全部保留。
    indo_only = {}
    for w in data['indo_only_words']:
        indo_only[w] = {'ms': old_indo_ms.get(w, data.get('suggestions', {}).get(w, ''))}
    casual = {}
    for w in data.get('uncertain_words', []):
        casual[w] = {'ms': old_casual.get(w, {}).get('ms', '')}
    for w, entry in data.get('register_words', {}).items():
        casual[w] = {'ms': old_casual.get(w, {}).get('ms', entry.get('expansion', ''))}
    for w in data.get('context_words', {}):
        casual[w] = {'ms': old_casual.get(w, {}).get('ms', '')}
    puzzle_casual = set(casual)
    for w, rec in old_casual.items():
        if w not in puzzle_casual and w not in indo_only:
            casual[w] = {'ms': rec.get('ms', '')}
    overlap = set(indo_only) & set(casual)
    if overlap:
        raise ValueError(f'words in both lists: {sorted(overlap)}')

    out = {
        '_meta': {
            **data.get('_meta', {}),
            'note': ('Three lists: indo_only = Indonesian-only words; casual = informal Malay '
                     'needing the standard form in formal writing. ms = DBP-standard equivalent, '
                     'empty = no single equivalent. ms mappings are maintained in the repo file '
                     '(research 2026-10-08); puzzle source remains the word-set truth.'),
            'generated_source_sha256': {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (markdown, source)},
        },
        'indo_only': {w: indo_only[w] for w in sorted(indo_only)},
        'casual': {w: casual[w] for w in sorted(casual)},
    }
    output.write_text(json.dumps(out, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    return {'indo_only': len(indo_only), 'casual': len(casual)}


if __name__ == '__main__':
    base = Path(__file__).resolve().parent          # tools/
    root = base.parent                               # prpm-checker/
    print(sync(root.parent / 'puzzle', root / 'indo_words.json'))
