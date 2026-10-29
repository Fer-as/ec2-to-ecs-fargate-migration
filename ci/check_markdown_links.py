"""Offline check of relative links in the repository's Markdown files.

Checks [text](target), ![alt](target) and <img src="target"> links that point inside the
repository: the target file or directory must exist and, for a "#fragment" on a Markdown
target, a heading with that GitHub-style anchor must exist. External URLs (http, https,
mailto) are not fetched, so the check never depends on the network.

Usage: python ci/check_markdown_links.py [repo-root]   (exit 1 if a link is broken)
"""

import re
import subprocess
import sys
from pathlib import Path

LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
IMG_SRC = re.compile(r"<img\b[^>]*\bsrc=\"([^\"]+)\"", re.IGNORECASE)
FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]*`")
EXTERNAL = re.compile(r"^(https?:|mailto:)", re.IGNORECASE)


def markdown_files(root: Path) -> list[Path]:
    """Tracked and new (not ignored) Markdown files."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        capture_output=True, text=True, check=True,
    ).stdout
    return sorted({root / line for line in out.splitlines() if line and (root / line).is_file()})


def anchors(md_file: Path) -> set[str]:
    """GitHub-style heading anchors (lowercase, punctuation removed, spaces to hyphens)."""
    text = FENCE.sub("", md_file.read_text(encoding="utf-8"))
    found = set()
    for line in text.splitlines():
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if match:
            slug = re.sub(r"[^\w\- ]", "", match.group(1).strip().lower()).replace(" ", "-")
            found.add(slug)
    return found


def broken_links(root: Path) -> list[str]:
    problems = []
    for md_file in markdown_files(root):
        text = INLINE_CODE.sub("", FENCE.sub("", md_file.read_text(encoding="utf-8")))
        targets = LINK.findall(text) + IMG_SRC.findall(text)
        for target in targets:
            if EXTERNAL.match(target):
                continue
            path_part, _, fragment = target.partition("#")
            dest = (md_file.parent / path_part).resolve() if path_part else md_file
            rel = md_file.relative_to(root).as_posix()
            if not dest.exists():
                problems.append(f"{rel}: missing target {target}")
            elif fragment and dest.suffix == ".md" and fragment not in anchors(dest):
                problems.append(f"{rel}: missing anchor {target}")
    return problems


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    problems = broken_links(root)
    for problem in problems:
        print(problem)
    print(f"{len(markdown_files(root))} Markdown files checked; {len(problems)} broken links")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
