# Copyright 2026 Moritz E. Beber
"""Run the Python code blocks documented in the project's Markdown files."""

from pathlib import Path

import pytest
from mktestdocs import check_md_file

_MARKDOWN_FILES = [*Path("docs").glob("**/*.md"), Path("README.md")]


@pytest.mark.parametrize("path", _MARKDOWN_FILES, ids=str)
def test_markdown_code_blocks_run(path: Path) -> None:
    """Every Python code block in a documented Markdown file executes cleanly."""
    check_md_file(fpath=path, memory=True)
