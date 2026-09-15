"""Check the shipped guides and their local links in actual wheel/sdist artifacts.

Run from a source checkout after ``python -m build``:
    python tests/packaging/check_docs.py dist/*.whl dist/*.tar.gz
"""

import argparse
import posixpath
import re
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r'\]\(([^)]+)\)')


def source_guides():
    guides = [
        ROOT / 'README.md',
        ROOT / 'INITIALIZATION.md',
        ROOT / 'THIRD_PARTY_NOTICES.md',
        ROOT / 'tests/benchmark/README.md',
        *sorted((ROOT / 'doc').rglob('*.md')),
        *sorted((ROOT / 'src/pg_perf_bench').rglob('*.md')),
    ]
    return {path.relative_to(ROOT).as_posix(): path.read_text(encoding='utf-8') for path in guides}


def read_archive(path):
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            return {
                name: archive.read(name).decode('utf-8')
                for name in archive.namelist()
                if name.endswith('.md')
            }
    with tarfile.open(path, 'r:gz') as archive:
        return {
            member.name.partition('/')[2]: archive.extractfile(member).read().decode('utf-8')
            for member in archive.getmembers()
            if member.isfile() and member.name.endswith('.md')
        }


def check_archive(path, sources):
    documents = read_archive(path)
    errors = []
    for source, content in sources.items():
        target = source
        if path.suffix == '.whl':
            target = (
                source.removeprefix('src/')
                if source.startswith('src/')
                else 'pg_perf_bench/docs/' + source
            )
        if target not in documents:
            errors.append(f'missing guide: {target}')
        elif LINK.sub('](LINK)', content) != LINK.sub('](LINK)', documents[target]):
            errors.append(f'stale or incomplete guide: {target}')

    for name, content in documents.items():
        for match in LINK.finditer(content):
            link = urlsplit(match[1])
            if link.scheme or link.netloc:
                continue
            target = (
                posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(link.path)))
                if link.path
                else name
            )
            if target not in documents:
                errors.append(f'broken local link: {name} -> {match[1]}')
            elif link.fragment:
                # GitHub-style anchors for the plain Markdown headings used by these guides.
                headings = re.findall(r'^#{1,6}\s+(.+)$', documents[target], flags=re.MULTILINE)
                anchors = {
                    re.sub(r'[^\w\- ]', '', heading.lower()).replace(' ', '-')
                    for heading in headings
                }
                if unquote(link.fragment) not in anchors:
                    errors.append(f'broken heading link: {name} -> {match[1]}')
    for error in errors:
        print(f'{path.name}: {error}')
    if not errors:
        print(f'{path.name}: {len(sources)} current guides, all local links OK')
    return not errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifacts', nargs='+', type=Path)
    args = parser.parse_args()
    sources = source_guides()
    results = [check_archive(path, sources) for path in args.artifacts]
    return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
