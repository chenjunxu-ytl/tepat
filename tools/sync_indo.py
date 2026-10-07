"""Validate readable classifications, then generate the shipped word policy."""
import hashlib
import json
import re
from pathlib import Path


def sync(source_root, output):
    markdown=source_root/'indo_blacklist.md';source=source_root/'indo_blacklist.json'
    text=markdown.read_text(encoding='utf-8');data=json.loads(source.read_text(encoding='utf-8'))
    def words(begin,end):
        return set(re.findall(r'`([^`]+)`',text.split(begin,1)[1].split(end,1)[0]))
    if words('## 印尼语候选','## Register')!=set(data['indo_only_words']):raise ValueError('Markdown/JSON Indonesian candidates differ')
    if words('## Uncertain','## 变更日志')!=set(data['uncertain_words']):raise ValueError('Markdown/JSON uncertain candidates differ')
    for word,entry in data['register_words'].items():
        if word not in words('## Register','## Context') or entry['expansion'] not in words('## Register','## Context'):raise ValueError('Markdown/JSON register candidates differ')
    if words('## Context','## Suggested counterparts')!=set(data['context_words']):raise ValueError('Markdown/JSON context candidates differ')
    for word,target in data['suggestions'].items():
        if word not in words('## Suggested counterparts','## Uncertain') or target not in words('## Suggested counterparts','## Uncertain'):raise ValueError('Markdown/JSON counterparts differ')
    data['_meta']['generated_source_sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (markdown,source)}
    output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return data['_meta']['counts']


if __name__=='__main__':
    base=Path(__file__).resolve().parent          # tools/
    root=base.parent                               # prpm-checker/
    print(sync(root.parent/'puzzle',root/'indo_words.json'))
