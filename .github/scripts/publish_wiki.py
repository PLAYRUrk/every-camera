#!/usr/bin/env python3
"""Build GitHub Wiki pages from docs/.

Usage: publish_wiki.py <docs_dir> <wiki_dir>

Everything in <wiki_dir> except .git is replaced. File names in docs/ stay
Latin because stations may run under a non-UTF-8 locale; the Wiki takes its
page names from each page's H1.
"""
import re
import shutil
import sys
from pathlib import Path

KEEP_NAME = {"Home", "_Sidebar", "_Footer"}
FORBIDDEN_IN_TITLE = re.compile(r'[./\\:*?"<>|#%]')
LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


class WikiError(Exception):
    pass


def split_fences(text):
    """Yield (line, in_code) so headings and links inside code blocks are skipped."""
    in_code = False
    for line in text.splitlines():
        if FENCE.match(line):
            yield line, True
            in_code = not in_code
        else:
            yield line, in_code


def slugify(heading):
    """Match the anchor GitHub generates for a heading."""
    text = LINK.sub(r"\1", heading).replace("`", "").strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors_of(text):
    seen = {}
    anchors = set()
    for line, in_code in split_fences(text):
        match = None if in_code else HEADING.match(line)
        if not match:
            continue
        slug = slugify(match.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(f"{slug}-{count}" if count else slug)
    return anchors


def page_title(stem, text):
    first = text.lstrip().split("\n", 1)[0]
    match = HEADING.match(first)
    if not match or len(match.group(1)) != 1:
        raise WikiError(f"{stem}.md: the first line must be an H1")
    title = match.group(2)
    if FORBIDDEN_IN_TITLE.search(title):
        raise WikiError(f"{stem}.md: the H1 {title!r} cannot name a Wiki page")
    return title


def wiki_names(pages):
    names = {}
    for stem, text in pages.items():
        names[stem] = (stem if stem in KEEP_NAME
                       else page_title(stem, text).replace(" ", "-"))
    duplicates = {n for n in names.values() if list(names.values()).count(n) > 1}
    if duplicates:
        raise WikiError(f"duplicate Wiki page names: {sorted(duplicates)}")
    return names


def convert_links(stem, text, pages, names, anchors):
    def replace(match):
        label, target = match.group(1), match.group(2)
        if re.match(r"[a-z][a-z0-9+.-]*:", target):
            return match.group(0)
        path, _, anchor = target.partition("#")
        if path and not path.endswith(".md"):
            raise WikiError(f"{stem}.md: unsupported link {target!r}")
        other = path[:-3] if path else stem
        if other not in pages:
            raise WikiError(f"{stem}.md: broken link {target!r}")
        if anchor and anchor not in anchors[other]:
            raise WikiError(f"{stem}.md: broken anchor {target!r}")
        if not path:
            return match.group(0)
        suffix = f"#{anchor}" if anchor else ""
        return f"[{label}]({names[other]}{suffix})"

    lines = []
    for line, in_code in split_fences(text):
        lines.append(line if in_code else LINK.sub(replace, line))
    return "\n".join(lines) + "\n"


def strip_h1(text):
    return text.lstrip().split("\n", 1)[1].lstrip("\n")


def check_sidebar(pages):
    if "_Sidebar" not in pages:
        raise WikiError("docs/_Sidebar.md is missing")
    listed = set(re.findall(r"\]\(([^)#]+)\.md", pages["_Sidebar"]))
    missing = set(pages) - listed - {"_Sidebar", "_Footer"}
    if missing:
        raise WikiError(f"pages missing from _Sidebar.md: {sorted(missing)}")


def build(docs_dir, wiki_dir):
    docs_dir, wiki_dir = Path(docs_dir), Path(wiki_dir)
    pages = {p.stem: p.read_text(encoding="utf-8")
             for p in sorted(docs_dir.glob("*.md"))}
    if "Home" not in pages:
        raise WikiError("docs/Home.md is missing")
    check_sidebar(pages)
    names = wiki_names(pages)
    anchors = {stem: anchors_of(text) for stem, text in pages.items()}

    output = {}
    for stem, text in pages.items():
        converted = convert_links(stem, text, pages, names, anchors)
        if stem not in KEEP_NAME:
            converted = strip_h1(converted)
        output[f"{names[stem]}.md"] = converted

    wiki_dir.mkdir(parents=True, exist_ok=True)
    for entry in wiki_dir.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for name, text in output.items():
        (wiki_dir / name).write_text(text, encoding="utf-8")
    return sorted(output)


def main(argv):
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        written = build(argv[1], argv[2])
    except WikiError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"{len(written)} pages written to {argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
