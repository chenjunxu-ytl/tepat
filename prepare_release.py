"""Prepare checked Zip64 release assets from the existing portable runtime.

Modes (user ruling 2026-10-02: the lightweight PRPM runtime is a first-class
release asset, not an afterthought):

  --mode core   PRPM-only runtime. No evidence.sqlite / rules.json /
                indo_words.json in the zip; the app starts in the
                grammar_module_not_installed capability state and serves
                PRPM lookups. Expected zip size ~15-25 MB.
  --mode full   PRPM runtime + the grammar data pack (previous behaviour).

Both modes verify the same runtime integrity checks that apply to the files
they actually ship.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

from evidence import EvidenceStore

BASE = Path(__file__).resolve().parent
TAG = 'v0.2.0-preview'
CHUNK = 4 * 1024 * 1024
MODES = ('core', 'full')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args]).decode().strip()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def archive(path, entries, *, evidence_digest=None):
    staging = path.with_suffix('.zip.part')
    print(f'Packing {path.name}', flush=True)
    with zipfile.ZipFile(staging, 'x', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=3, allowZip64=True) as bundle:
        for source, name in entries:
            size = source.stat().st_size
            if size > 100_000_000:
                print(f'  {name}: {size:,} bytes', flush=True)
            with source.open('rb') as incoming, bundle.open(name, 'w', force_zip64=True) as outgoing:
                copied = 0
                next_report = size / 4
                while block := incoming.read(CHUNK):
                    outgoing.write(block)
                    copied += len(block)
                    if size > 100_000_000 and copied >= next_report:
                        print(f'  evidence {min(100, copied * 100 // size)}%', flush=True)
                        next_report += size / 4
    with zipfile.ZipFile(staging) as bundle:
        assert set(bundle.namelist()) == {name for _, name in entries}
        # Fully consume every entry to check decompression and CRC, including >2 GB.
        for entry in bundle.infolist():
            with bundle.open(entry) as stream:
                value = hashlib.file_digest(stream, 'sha256').hexdigest()
            if entry.filename.endswith('/data/evidence.sqlite'):
                if value != evidence_digest:
                    raise ValueError('Archived evidence hash differs from the completed database')
    if staging.stat().st_size >= 2 * 1024**3:
        raise ValueError('Asset exceeds GitHub release file limit; keep staging for inspection')
    staging.rename(path)
    print(f'Verified {path.name}: {path.stat().st_size:,} bytes', flush=True)


GRAMMAR_FILES = ('rules.json', 'indo_words.json')
GRAMMAR_APP_FILES = tuple(f'_internal/{name}' for name in GRAMMAR_FILES)
GRAMMAR_ARCHIVE = '_internal/data/evidence.sqlite'


def prepare(app_dir, output, mode: str):
    if git(BASE, 'status', '--porcelain'):
        raise ValueError('Commit checker changes before preparing a release')
    puzzle = BASE.parent / 'puzzle'
    if git(puzzle, 'status', '--porcelain', '--', 'cleaning', 'indo_blacklist.md', 'indo_blacklist.json', 'split.json'):
        raise ValueError('Commit the corpus tools and policy sources first')
    extension = json.loads((BASE / 'extension/manifest.json').read_text(encoding='utf-8'))
    if extension.get('version_name') != TAG.removeprefix('v'):
        raise ValueError('Extension preview version differs from the release tag')
    evidence = BASE / 'data/evidence.sqlite'
    metadata = None
    if mode == 'full':
        store = EvidenceStore(evidence)
        metadata = store.metadata
        if metadata.get('build_scope') != 'full' or metadata.get('tokenizer_sha256') != digest(BASE / 'text_units.py'):
            raise ValueError('Completed evidence and current tokenizer must match')
        store.close_thread()
    exe = app_dir / 'tepat-v2.exe'
    if not exe.is_file() or not (app_dir / '_internal/python313.dll').is_file():
        raise ValueError('Windows portable runtime is missing')
    # core 模式要求运行时确实是 core 构建（没带语法数据）——否则打出来的
    # "轻量版"会带数据库，正是 v0.2.0-preview 600MB 事故的根因。
    bundled_grammar = [rel for rel in (*GRAMMAR_APP_FILES, GRAMMAR_ARCHIVE)
                       if (app_dir / rel).is_file()]
    if mode == 'core' and bundled_grammar:
        raise ValueError(
            'app_dir contains grammar data but mode is core: '
            + ', '.join(bundled_grammar)
            + ' — rebuild with "build.bat core" first')
    for relative in ('web/index.html', 'extension/results.js', *GRAMMAR_FILES):
        source = BASE / relative
        if not source.is_file():
            continue  # core 构建可能没把语法文件拷到 app_dir
        if digest(app_dir / '_internal' / relative) != digest(source):
            raise ValueError(f'Portable resource differs from source: {relative}')

    output.mkdir(parents=True, exist_ok=False)
    for name in ('INSTALL.md', 'RELEASE_NOTES.md'):
        (output / name).write_bytes((BASE / name).read_bytes())
    manifest = {
        'tag': TAG, 'prerelease': True, 'mode': mode,
        'checker_commit': git(BASE, 'rev-parse', 'HEAD'),
        'corpus_tools_commit': git(puzzle, 'rev-parse', 'HEAD'),
        'extension_version': extension['version'],
        'executable_sha256': digest(exe),
        'scope': 'Partial spelling/terminology/grammar signals; factuality not checked',
    }
    if mode == 'full':
        manifest['evidence'] = {
            'review_run': metadata['review_run'], 'schema_version': metadata['schema_version'],
            'tokenizer_sha256': metadata['tokenizer_sha256'],
            'bytes': evidence.stat().st_size, 'sha256': digest(evidence),
            'lexicon_count': metadata['lexicon_count'], 'counts': metadata['counts'],
            'gram_counts': metadata['gram_counts'],
        }
    else:
        manifest['grammar_module'] = 'not bundled — PRPM-only runtime'
    (output / 'release.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    documents = [(output / name, name) for name in ('INSTALL.md', 'RELEASE_NOTES.md', 'release.json')]

    runtime = []
    for source in sorted((app_dir / '_internal').rglob('*')):
        relative = source.relative_to(app_dir).as_posix()
        if not source.is_file() or source.name == 'blacklist.json':
            continue
        # core: 语法数据不进 zip（runtime 目录里不该有，这里再防一道）
        if mode == 'core' and (relative in GRAMMAR_APP_FILES or relative == GRAMMAR_ARCHIVE
                               or relative.startswith('_internal/data/')):
            continue
        runtime.append((source, 'tepat-v2/' + relative))
    runtime.append((exe, 'tepat-v2/tepat-v2.exe'))
    if mode == 'full':
        runtime += [(evidence, 'tepat-v2/' + GRAMMAR_ARCHIVE)]
        runtime += [(BASE / name, 'tepat-v2/' + name) for name in GRAMMAR_FILES]
    runtime += [(source, 'tepat-v2/' + name) for source, name in documents]
    suffix = '' if mode == 'full' else '-prpm'
    archive(output / f'tepat-{TAG}{suffix}-win64.zip', runtime,
            evidence_digest=manifest.get('evidence', {}).get('sha256'))

    extension_files = git(BASE, 'ls-files', '--', 'extension').splitlines()
    archive(output / f'tepat-{TAG}{suffix}-chrome.zip', [(BASE / name, name) for name in extension_files] + documents)
    corpus_files = git(puzzle, 'ls-files', '--', 'cleaning', 'indo_blacklist.md', 'indo_blacklist.json', 'split.json').splitlines()
    archive(output / f'tepat-{TAG}{suffix}-corpus-tools.zip', [(puzzle / name, 'puzzle/' + name) for name in corpus_files] + documents)

    assets = sorted(path for path in output.iterdir() if path.is_file())
    (output / 'SHA256SUMS').write_text(''.join(f'{digest(path)}  {path.name}\n' for path in assets), encoding='utf-8')
    print(f'Release assets ready at {output} (mode={mode})', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=MODES, default='full',
                        help='core = PRPM-only lightweight asset; full = PRPM + grammar data pack')
    parser.add_argument('--app-dir', type=Path, default=BASE / 'dist/tepat-v2')
    parser.add_argument('--output', type=Path, default=None,
                        help='defaults to releases/%s[-core] per mode' % TAG)
    args = parser.parse_args()
    output = args.output or BASE / 'releases' / (TAG if args.mode == 'full' else f'{TAG}-core')
    prepare(args.app_dir.resolve(), output.resolve(), args.mode)
