#!/usr/bin/env python3
"""Validate, mirror and package the text repository contract.

The release keeps the legacy shape that patchers download from
GakumasTranslationDataKor: `<repository>-<version>.zip` holding `local-files/`
and `version.txt` at the root, tagged with the contents of `version.txt`.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile

PAYLOAD = 'local-files'
VERSION_FILE = 'version.txt'
# Everything else a text repository may hold. RELEASE_NOTES.md is not here:
# the owner retired it from the text output (2026-09-26).
TOP_LEVEL = {'.git', '.github', PAYLOAD, VERSION_FILE, 'README.md', '.gitattributes'}
MAX_FILE_BYTES = 100 * 1000 * 1000  # GitHub refuses larger blobs
MAX_TOTAL_BYTES = 1536 * 1024 * 1024
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
# GakuToolkit writes the KST time as YYYYMMDD_HHMMSS without a final newline.
VERSION = re.compile(r'([0-9]{8}_[0-9]{6})\n?\Z')
SAFE_COMPONENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._ -]{0,253}[A-Za-z0-9._-]\Z|[A-Za-z0-9]\Z')
RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(n) for n in range(1, 10)), *('LPT' + str(n) for n in range(1, 10))}
REPOSITORY_NAME = re.compile(r'[A-Za-z0-9._-]{1,100}\Z')

def read_version(root):
    path = root / VERSION_FILE
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32:
        raise ValueError('Missing or unsafe version.txt')
    match = VERSION.fullmatch(path.read_bytes().decode('ascii', 'replace'))
    if not match:
        raise ValueError('version.txt must be YYYYMMDD_HHMMSS')
    datetime.strptime(match.group(1), '%Y%m%d_%H%M%S')
    return match.group(1)

def inventory(root):
    """Check the whole tree and return every payload file with its digest."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Text repository root must be a real directory')
    for item in root.iterdir():
        if item.name not in TOP_LEVEL:
            raise ValueError('Unexpected repository entry: ' + item.name)
        if item.is_symlink():
            raise ValueError('Symlinks are refused: ' + item.name)
    for item in (root / '.github').rglob('*') if (root / '.github').is_dir() else ():
        if item.is_symlink():
            raise ValueError('Symlinks are refused: ' + item.relative_to(root).as_posix())
    version = read_version(root)
    payload = root / PAYLOAD
    if payload.is_symlink() or not payload.is_dir():
        raise ValueError('local-files must be a real directory')
    files, names, total = {}, set(), 0
    for item in sorted(payload.rglob('*')):
        relative = item.relative_to(root)
        name = relative.as_posix()
        if item.is_symlink():
            raise ValueError('Symlinks are refused: ' + name)
        for part in relative.parts:
            if not SAFE_COMPONENT.fullmatch(part) or part.split('.')[0].upper() in RESERVED:
                raise ValueError('Unsafe or non-portable path: ' + name)
        if name.casefold() in names:
            raise ValueError('Case-insensitive path collision: ' + name)
        names.add(name.casefold())
        if item.is_dir():
            continue
        if not item.is_file():
            raise ValueError('Only regular files may be published: ' + name)
        size = item.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError('File is larger than GitHub accepts: ' + name)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError('Text distribution exceeds the 1.5 GiB limit')
        with item.open('rb') as stream:
            files[name] = {'bytes': size, 'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}
    if not files:
        raise ValueError('Empty text publication is refused')
    version_path = root / VERSION_FILE
    files[VERSION_FILE] = {'bytes': version_path.stat().st_size, 'sha256': hashlib.sha256(version_path.read_bytes()).hexdigest()}
    return {'version': version, 'files': files, 'count': len(files) - 1}

def archive_name(repository, version):
    if not REPOSITORY_NAME.fullmatch(repository):
        raise ValueError('Repository name is invalid')
    return f'{repository}-{version}.zip'

def mirror(source, target):
    """Make target's payload exactly source's; leave its workflows and README."""
    source, target = Path(source).resolve(), Path(target)
    if target.is_symlink() or not target.is_dir():
        raise ValueError('Target must be a real repository directory')
    target = target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('Source and target must be separate directories')
    before = inventory(source)
    for item in target.iterdir():
        if item.name not in TOP_LEVEL or item.is_symlink():
            raise ValueError('Unexpected target entry: ' + item.name)
    for name, kind in ((PAYLOAD, 'dir'), (VERSION_FILE, 'file')):
        path = target / name
        if path.is_symlink() or path.exists() and not (path.is_dir() if kind == 'dir' else path.is_file()):
            raise ValueError('Unsafe target path: ' + name)
    if (target / PAYLOAD).exists():
        shutil.rmtree(target / PAYLOAD)
    shutil.copytree(source / PAYLOAD, target / PAYLOAD)
    shutil.copyfile(source / VERSION_FILE, target / VERSION_FILE)
    after = inventory(target)
    if after['files'] != before['files'] or inventory(source) != before:
        raise ValueError('Text inventory changed while mirroring')
    return before

def package(root, output, repository):
    """Write the legacy-shaped ZIP and SHA256SUMS; byte-identical on replay."""
    root, output = Path(root), Path(output)
    before = inventory(root)
    name = archive_name(repository, before['version'])
    directories = sorted({parent for path in before['files'] for parent in _parents(path)})
    output.mkdir(parents=True, exist_ok=True)
    archive = output / name
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for directory in directories:
            entry = zipfile.ZipInfo(directory + '/', date_time=ZIP_TIME)
            entry.create_system = 3
            entry.external_attr = (0o40755 << 16) | 0x10
            zipped.writestr(entry, b'')
        for relative in sorted(before['files']):
            entry = zipfile.ZipInfo(relative, date_time=ZIP_TIME)
            entry.create_system = 3
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry._compresslevel = 6
            entry.external_attr = 0o100644 << 16
            with zipped.open(entry, 'w') as destination, (root / relative).open('rb') as source:
                shutil.copyfileobj(source, destination, 1024 * 1024)
    if inventory(root) != before:
        raise ValueError('Text inventory changed while packaging')
    verify(root, archive)
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    (output / 'SHA256SUMS').write_text(f'{digest}  {name}\n')
    return {'version': before['version'], 'files': before['count'], 'archive': name, 'archive_sha256': digest}

def _parents(path):
    parts = path.split('/')[:-1]
    return ['/'.join(parts[:index]) for index in range(1, len(parts) + 1)]

def verify(root, archive):
    """Check a (possibly already released) archive carries exactly this tree."""
    expected = inventory(root)
    with zipfile.ZipFile(archive) as zipped:
        entries = [entry for entry in zipped.infolist() if not entry.is_dir()]
        if len(entries) != len(expected['files']) or {entry.filename for entry in entries} != set(expected['files']):
            raise ValueError('Released version has a different file list; bump version.txt')
        for entry in entries:
            wanted = expected['files'][entry.filename]
            if entry.file_size != wanted['bytes']:
                raise ValueError('Released version has different content; bump version.txt')
            with zipped.open(entry) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != wanted['sha256']:
                    raise ValueError('Released version has different content; bump version.txt')
    return {'version': expected['version'], 'files': expected['count'], 'verified': True}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['inspect', 'mirror', 'package', 'verify'])
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path, nargs='?')
    parser.add_argument('--repository', help='repository name for the archive (package)')
    args = parser.parse_args()
    if args.operation != 'inspect' and args.destination is None:
        parser.error('destination is required')
    if args.operation == 'package' and not args.repository:
        parser.error('package needs --repository')
    if args.operation == 'inspect':
        result = inventory(args.source)
    elif args.operation == 'package':
        result = package(args.source, args.destination, args.repository)
    else:
        result = {'mirror': mirror, 'verify': verify}[args.operation](args.source, args.destination)
    print(json.dumps({key: value for key, value in result.items() if key != 'files' or not isinstance(value, dict)}))

if __name__ == '__main__':
    main()
