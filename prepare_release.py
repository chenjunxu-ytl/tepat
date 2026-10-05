"""Prepare checked release assets from an existing core or full portable runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

from evidence import EvidenceStore

BASE = Path(__file__).resolve().parent
TAG = 'v2.0.0-preview.1'
CHUNK = 4 * 1024 * 1024


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args]).decode().strip()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def archive(path, entries, *, evidence_digest=None):
    staging = path.with_suffix('.zip.part')
    if staging.exists():
        staging.unlink()
    if path.exists():
        raise ValueError(f'Release asset already exists: {path}')
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
        for entry in bundle.infolist():
            with bundle.open(entry) as stream:
                value = hashlib.file_digest(stream, 'sha256').hexdigest()
            if evidence_digest and entry.filename.endswith('/data/evidence.sqlite'):
                if value != evidence_digest:
                    raise ValueError('Archived evidence hash differs from the completed database')
    if staging.stat().st_size >= 2 * 1024**3:
        raise ValueError('Asset exceeds GitHub release file limit; keep staging for inspection')
    staging.rename(path)
    print(f'Verified {path.name}: {path.stat().st_size:,} bytes', flush=True)


def runtime_entries(app_dir):
    runtime = []
    for source in sorted((app_dir / '_internal').rglob('*')):
        if not source.is_file() or source.name == 'blacklist.json':
            continue
        runtime.append((source, 'tepat-v2/' + source.relative_to(app_dir).as_posix()))
    runtime.append((app_dir / 'tepat-v2.exe', 'tepat-v2/tepat-v2.exe'))
    return runtime


def prepare(app_dir, output, mode):
    if git(BASE, 'status', '--porcelain'):
        raise ValueError('Commit Tepat changes before preparing a release')

    extension = json.loads((BASE / 'extension/manifest.json').read_text(encoding='utf-8'))
    if extension.get('version_name') != TAG.removeprefix('v'):
        raise ValueError('Extension preview version differs from the release tag')

    exe = app_dir / 'tepat-v2.exe'
    if not exe.is_file() or not (app_dir / '_internal/python313.dll').is_file():
        raise ValueError('Windows portable runtime is missing')
    for relative in ('web/index.html', 'extension/results.js'):
        if digest(app_dir / '_internal' / relative) != digest(BASE / relative):
            raise ValueError(f'Portable resource differs from source: {relative}')

    output.mkdir(parents=True, exist_ok=True)
    for name in ('INSTALL.md', 'RELEASE_NOTES.md'):
        (output / name).write_bytes((BASE / name).read_bytes())

    manifest = {
        'tag': TAG,
        'prerelease': True,
        'mode': mode,
        'checker_commit': git(BASE, 'rev-parse', 'HEAD'),
        'extension_version': extension['version'],
        'executable_sha256': digest(exe),
        'capabilities': {
            'prpm': True,
            'grammar': mode == 'full',
        },
    }

    runtime = runtime_entries(app_dir)
    evidence_digest = None

    if mode == 'core':
        forbidden = {
            'tepat-v2/_internal/data/evidence.sqlite',
            'tepat-v2/_internal/rules.json',
            'tepat-v2/_internal/indo_words.json',
        }
        packaged = {name for _, name in runtime}
        leaked = sorted(forbidden & packaged)
        if leaked:
            raise ValueError(f'Core runtime unexpectedly contains grammar data: {leaked}')
        manifest['scope'] = 'Lightweight PRPM checker; grammar module not bundled'
        manifest_name = 'release-core.json'
        asset_name = f'tepat-{TAG}-prpm-win64.zip'
    else:
        puzzle = BASE.parent / 'puzzle'
        if git(puzzle, 'status', '--porcelain', '--', 'cleaning', 'indo_blacklist.md', 'indo_blacklist.json', 'split.json'):
            raise ValueError('Commit the corpus tools and policy sources first')
        evidence = BASE / 'data/evidence.sqlite'
        store = EvidenceStore(evidence)
        metadata = store.metadata
        if metadata.get('build_scope') != 'full' or metadata.get('tokenizer_sha256') != digest(BASE / 'text_units.py'):
            raise ValueError('Completed evidence and current tokenizer must match')
        store.close_thread()
        for relative in ('rules.json', 'indo_words.json'):
            packaged = app_dir / '_internal' / relative
            if not packaged.is_file() or digest(packaged) != digest(BASE / relative):
                raise ValueError(f'Portable resource differs from source: {relative}')
        evidence_digest = digest(evidence)
        manifest['corpus_tools_commit'] = git(puzzle, 'rev-parse', 'HEAD')
        manifest['evidence'] = {
            'review_run': metadata['review_run'],
            'schema_version': metadata['schema_version'],
            'tokenizer_sha256': metadata['tokenizer_sha256'],
            'bytes': evidence.stat().st_size,
            'sha256': evidence_digest,
            'lexicon_count': metadata['lexicon_count'],
            'counts': metadata['counts'],
            'gram_counts': metadata['gram_counts'],
        }
        manifest['scope'] = 'PRPM plus partial spelling/terminology/grammar signals; factuality not checked'
        manifest_name = 'release.json'
        asset_name = f'tepat-{TAG}-win64.zip'

    manifest_path = output / manifest_name
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    documents = [(output / name, name) for name in ('INSTALL.md', 'RELEASE_NOTES.md')]
    documents.append((manifest_path, manifest_name))
    archive(output / asset_name, runtime + [(source, 'tepat-v2/' + name) for source, name in documents],
            evidence_digest=evidence_digest)

    if mode == 'full':
        puzzle = BASE.parent / 'puzzle'
        extension_files = git(BASE, 'ls-files', '--', 'extension').splitlines()
        archive(output / f'tepat-{TAG}-chrome.zip',
                [(BASE / name, name) for name in extension_files] + documents)
        corpus_files = git(puzzle, 'ls-files', '--', 'cleaning', 'indo_blacklist.md',
                           'indo_blacklist.json', 'split.json').splitlines()
        archive(output / f'tepat-{TAG}-corpus-tools.zip',
                [(puzzle / name, 'puzzle/' + name) for name in corpus_files] + documents)

    assets = sorted(path for path in output.iterdir()
                    if path.is_file() and path.name != 'SHA256SUMS')
    (output / 'SHA256SUMS').write_text(
        ''.join(f'{digest(path)}  {path.name}\n' for path in assets),
        encoding='utf-8',
    )
    print(f'{mode.capitalize()} release assets ready at {output}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('core', 'full'), default='full')
    parser.add_argument('--app-dir', type=Path, default=BASE / 'dist/tepat-v2')
    parser.add_argument('--output', type=Path, default=BASE / 'releases' / TAG)
    args = parser.parse_args()
    prepare(args.app_dir.resolve(), args.output.resolve(), args.mode)
