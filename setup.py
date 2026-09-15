"""Include the canonical repository guides in wheels, with portable local links."""

import os
import re
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / 'src' / 'pg_perf_bench'


class BuildPy(build_py):
    def documentation(self):
        sources = [
            ROOT / 'README.md',
            ROOT / 'INITIALIZATION.md',
            ROOT / 'THIRD_PARTY_NOTICES.md',
            *sorted((ROOT / 'doc').rglob('*.md')),
            ROOT / 'tests' / 'benchmark' / 'README.md',
        ]
        docs = Path(self.build_lib) / 'pg_perf_bench' / 'docs'
        return {source: docs / source.relative_to(ROOT) for source in sources}

    def run(self):
        super().run()
        destinations = self.documentation()
        # Profile and JOIN READMEs also link to the root guides. Rewrite only
        # generated build files; the source checkout remains the single source.
        destinations.update(
            (source, Path(self.build_lib) / 'pg_perf_bench' / source.relative_to(PACKAGE))
            for source in PACKAGE.rglob('*.md')
        )
        for source, destination in destinations.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                self.rewrite_links(source, destination, destinations),
                encoding='utf-8',
            )

    @staticmethod
    def rewrite_links(source, destination, destinations):
        def rewrite(match):
            path, separator, anchor = match[1].partition('#')
            if not path or '://' in path:
                return match[0]
            installed = destinations.get((source.parent / path).resolve())
            if installed is None:
                return match[0]
            relative = Path(os.path.relpath(installed, destination.parent)).as_posix()
            return '](' + relative + separator + anchor + ')'

        return re.sub(r'\]\(([^)]+)\)', rewrite, source.read_text(encoding='utf-8'))

    def get_outputs(self, include_bytecode=True):
        return super().get_outputs(include_bytecode) + [
            str(path) for path in self.documentation().values()
        ]


if __name__ == '__main__':
    setup(cmdclass={'build_py': BuildPy})
