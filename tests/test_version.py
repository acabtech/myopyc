"""Version single-source check.

The package version must stay identical in three places:

- ``src/myopyc/__init__.py`` (``__version__``)
- ``pyproject.toml`` (``[project] version``)
- ``CHANGELOG.md`` (the latest ``## [x.y.z]`` heading)

This test fails loudly if a release bump touches one but not the others.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import myopyc

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_VERSION_HEADING = re.compile(r"^## \[(\d[^\]]*)\]", re.MULTILINE)


def _pyproject_version() -> str:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    version: str = data["project"]["version"]
    return version


def _changelog_version() -> str:
    text = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = _VERSION_HEADING.search(text)
    assert match is not None, "CHANGELOG.md has no '## [x.y.z]' version heading"
    return match.group(1)


def test_package_version_matches_pyproject() -> None:
    assert myopyc.__version__ == _pyproject_version()


def test_package_version_matches_changelog() -> None:
    assert myopyc.__version__ == _changelog_version()
