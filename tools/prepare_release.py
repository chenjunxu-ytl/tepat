"""Prepare checked Zip64 release assets from the existing portable runtime.

Single mode (2026-10-09): runtime + web + extension assets. Rule and word-list
data live only in %APPDATA%\\tepat; the first launch fetches rules.json /
indo_words.json / word-overrides.json from the GitHub repo immediately, so no
policy data ships in the zip and there is no duplicate copy beside the exe.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # 项目根（本文件在 tools/）
TAG = 'v0.3.0'
CHUNK = 4 * 1024 * 1024


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
    if extension.get('version') != TAG.removeprefix('v'):
        raise ValueError('Extension version differs from the release tag')
    exe = app_dir / 'tepat-v2.exe'
    if not exe.is_file() or not (app_dir / '_internal/python313.dll').is_file():
        raise ValueError('Windows portable runtime is missing')
    for relative in ('web/index.html', 'extension/results.js'):
        source = BASE / relative
        packaged = app_dir / '_internal' / relative
        if source.is_file() and packaged.is_file() and digest(packaged) != digest(source):
            raise ValueError(f'Portable resource differs from source: {relative}')
    # 政策数据不允许混进发布（单份原则：APPDATA + GitHub），旧构建残留也拒绝。
    for name in ('rules.json', 'indo_words.json'):
        if (app_dir / name).is_file() or (app_dir / '_internal' / name).is_file():
            raise ValueError(f'{name} beside/below the runtime is redundant '
                             f'(APPDATA + GitHub are the single source) — rebuild with build.bat')
    if (app_dir / '_internal/data').is_dir():
        raise ValueError('app_dir contains _internal/data (legacy evidence) — rebuild with build.bat')

    output.mkdir(parents=True, exist_ok=False)
    for name in ('INSTALL.md', 'RELEASE_NOTES.md'):
        (output / name).write_bytes((BASE / name).read_bytes())
    manifest = {
        'tag': TAG, 'prerelease': False,
        'checker_commit': git(BASE, 'rev-parse', 'HEAD'),
        'corpus_tools_commit': git(puzzle, 'rev-parse', 'HEAD'),
        'extension_version': extension['version'],
        'executable_sha256': digest(exe),
        'scope': 'Rule-based word/grammar checks + PRPM lookups; factuality not checked',
        'policy_data': 'not bundled — fetched from the GitHub repo on first launch',
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
    runtime += [(source, 'tepat-v2/' + name) for source, name in documents]
    archive(output / f'tepat-{TAG}-win64.zip', runtime)

    extension_files = git(BASE, 'ls-files', '--', 'extension').splitlines()
    archive(output / f'tepat-{TAG}-chrome.zip', [(BASE / name, name) for name in extension_files] + documents)

    # all-in-one（用户裁决 2026-10-09）：exe 运行时 + extension 一步到位——
    # 用户解压即得完整 Tepat（跑 exe + 加载扩展），不用分别下两个 zip。
    # 布局：tepat/extension/...（chrome://extensions 直接 Load unpacked 选它）、
    # tepat/tepat-v2/...（运行时）、tepat/source/...（无 exe 备胎：exe 被杀软/
    # SmartScreen 拦时进这里跑 start.bat）、文档在 tepat/ 根。
    bundle = [(BASE / name, 'tepat/extension/' + name.split('/', 1)[1])
              for name in extension_files]
    for source, name in runtime:
        if name.startswith('tepat-v2/_internal') or name == 'tepat-v2/tepat-v2.exe':
            bundle.append((source, name.replace('tepat-v2/', 'tepat/tepat-v2/', 1)))
        # else: 文档（tepat-v2/INSTALL.md 等）——留在根，下面 documents 补
    bundle += [(BASE / name, 'tepat/source/' + name)
               for name in ('server.py', 'checker.py', 'evidence.py',
                            'text_units.py', 'start.bat')]
    bundle += [(BASE / 'web' / 'index.html', 'tepat/source/web/index.html')]
    bundle += [(source, 'tepat/' + name) for source, name in documents]
    archive(output / f'tepat-{TAG}-all-in-one.zip', bundle)

    # source（用户裁决 2026-10-09）：无 exe 替代——目标机器装 Python 即跑
    # （server 纯 stdlib，零 pip 依赖），绕开无签名 exe 的 SmartScreen/杀软
    # 拦截。核心 py + web + extension + start.bat + 文档；政策数据靠首启
    # sync 拉（单份原则，不进包）。
    source_files = git(BASE, 'ls-files', '--',
                       'server.py', 'checker.py', 'evidence.py', 'text_units.py',
                       'start.bat', 'assets/', 'web/', 'extension/').splitlines()
    source = [(BASE / name, 'tepat-source/' + name) for name in source_files]
    source += [(path, 'tepat-source/' + name) for path, name in documents]
    archive(output / f'tepat-{TAG}-source.zip', source)

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
