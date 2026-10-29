"""Relative Markdown links and anchors in the repository must resolve (offline check)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_markdown_links  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_repository_markdown_links_resolve():
    assert check_markdown_links.broken_links(REPO_ROOT) == []


def test_checker_reports_missing_file_and_anchor(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.md").write_text("# Title\n\n[ok](#title) [bad](#nope) [gone](missing.md)\n"
                                   "[ext](https://example.com/x) `[code](skipped.md)`\n", encoding="utf-8")
    problems = check_markdown_links.broken_links(tmp_path)
    assert problems == ["a.md: missing anchor #nope", "a.md: missing target missing.md"]
