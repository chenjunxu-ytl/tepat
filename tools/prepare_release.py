"""Prepare checked Zip64 release assets from the existing portable runtime.

Single mode (2026-10-09): rules + word lists + PRPM. The legacy 2.3GB
evidence corpus is not shipped — word/grammar checks run from APPDATA rule
files plus PRPM lookups, and rules.json / indo_words.json ride along as
first-start seeds. Expected zip size tens of MB.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # 项目根（本文件在 tools/）
TAG = 'v0.2.0-preview'
CHUNK = 4 * 1024 * 1024

SEED_FILES = ('rules.json', 'indo_words.json')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args]).decode().strip()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def archive(path, entries):
    staging = path.with_suffix('.zip.part')
    print(f'Packing {path.name}', flush=True)
    with zipfile.ZipFile(staging, 'x', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=3, allowZip64=True) as bundle:
        for source, name in entries:
            size = source.stat().st_size
            if size > 100_000_000:
                print(f'  {name}: {size:,} bytes', flush=True)
            with source.open('rb') as incoming, bundle.open(name, 'w', force_zip64=True) as outgoing:
                while block := incoming.read(CHUNK):
                    outgoing.write(block)
    with zipfile.ZipFile(staging) as bundle:
        assert set(bundle.namelist()) == {name for _, name in entries}
        # Fully consume every entry to check decompression and CRC.
        for entry in bundle.infolist():
            with bundle.open(entry) as stream:
                hashlib.file_digest(stream, 'sha256')
    staging.rename(path)
    print(f'Verified {path.name}: {path.stat().st_size:,} bytes', flush=True)


def prepare(app_dir, output):
    if git(BASE, 'status', '--porcelain'):
        raise ValueError('Commit checker changes before preparing a release')
    puzzle = BASE.parent / 'puzzle'
    if git(puzzle, 'status', '--porcelain', '--', 'cleaning', 'indo_blacklist.md', 'indo_blacklist.json', 'split.json'):
        raise ValueError('Commit the corpus tools and policy sources first')
    extension = json.loads((BASE / 'extension/manifest.json').read_text(encoding='utf-8'))
    if extension.get('version_name') != TAG.removeprefix('v'):
        raise ValueError('Extension preview version differs from the release tag')
    exe = app_dir / 'tepat-v2.exe'
    if not exe.is_file() or not (app_dir / '_internal/python313.dll').is_file():
        raise ValueError('Windows portable runtime is missing')
    # 语法数据（rules/indo_words）必须随运行时打包——它们现在是检查的本体。
    for name in SEED_FILES:
        if not (app_dir / '_internal' / name).is_file():
            raise ValueError(f'{name} missing from the portable runtime — rebuild with build.bat')
    for relative in ('web/index.html', 'extension/results.js', *SEED_FILES):
        source = BASE / relative
        packaged = app_dir / '_internal' / relative
        if source.is_file() and packaged.is_file() and digest(packaged) != digest(source):
            raise ValueError(f'Portable resource differs from source: {relative}')
    # 遗留证据库不允许混进发布（600MB 事故防线）。
    if (app_dir / '_internal/data').is_dir():
        raise ValueError('app_dir contains _internal/data (legacy evidence) — rebuild with build.bat')

    output.mkdir(parents=True, exist_ok=False)
    for name in ('INSTALL.md', 'RELEASE_NOTES.md'):
        (output / name).write_bytes((BASE / name).read_bytes())
    manifest = {
        'tag': TAG, 'prerelease': True,
        'checker_commit': git(BASE, 'rev-parse', 'HEAD'),
        'corpus_tools_commit': git(puzzle, 'rev-parse', 'HEAD'),
        'extension_version': extension['version'],
        'executable_sha256': digest(exe),
        'scope': 'Rule-based word/grammar checks + PRPM lookups; factuality not checked',
        'evidence_corpus': 'retired — not shipped (rules + PRPM only)',
    }
    (output / 'release.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    documents = [(output / name, name) for name in ('INSTALL.md', 'RELEASE_NOTES.md', 'release.json')]

    runtime = []
    for source in sorted((app_dir / '_internal').rglob('*')):
        relative = source.relative_to(app_dir).as_posix()
        if not source.is_file() or source.name == 'blacklist.json':
            continue
        runtime.append((source, 'tepat-v2/' + relative))
    runtime.append((exe, 'tepat-v2/tepat-v2.exe'))
    runtime += [(BASE / name, 'tepat-v2/' + name) for name in SEED_FILES]
    runtime += [(source, 'tepat-v2/' + name) for source, name in documents]
    archive(output / f'tepat-{TAG}-win64.zip', runtime)

    extension_files = git(BASE, 'ls-files', '--', 'extension').splitlines()
    archive(output / f'tepat-{TAG}-chrome.zip', [(BASE / name, name) for name in extension_files] + documents)
    corpus_files = git(puzzle, 'ls-files', '--', 'cleaning', 'indo_blacklist.md', 'indo_blacklist.json', 'split.json').splitlines()
    archive(output / f'tepat-{TAG}-corpus-tools.zip', [(puzzle / name, 'puzzle/' + name) for name in corpus_files] + documents)

    assets = sorted(path for path in output.iterdir() if path.is_file())
    (output / 'SHA256SUMS').write_text(''.join(f'{digest(path)}  {path.name}\n' for path in assets), encoding='utf-8')
    print(f'Release assets ready at {output}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir', type=Path, default=BASE / 'dist/tepat-v2')
    parser.add_argument('--output', type=Path, default=None,
                        help='defaults to releases/%s' % TAG)
    args = parser.parse_args()
    output = args.output or BASE / 'releases' / TAG
    prepare(args.app_dir.resolve(), output.resolve())
