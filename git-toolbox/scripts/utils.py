import re
import os
from pathlib import Path
from typing import List, Dict, Any


def validate_path(relative: str, base: Path) -> Path:
    if ".." in relative or relative.startswith("/"):
        raise ValueError(f"Invalid path: {relative}")

    base = base.resolve()
    full_path = (base / relative)

    try:
        canonical_full = full_path.resolve()
    except OSError:
        parent = full_path.parent
        try:
            canonical_parent = parent.resolve()
        except OSError:
            raise ValueError(f"Failed to canonicalize parent path: {parent}")

        if not str(canonical_parent).startswith(str(base)):
            raise ValueError(f"Path traversal detected in parent: {parent}")
        return full_path

    if not str(canonical_full).startswith(str(base)):
        raise ValueError(f"Path traversal detected: {canonical_full}")

    return canonical_full


def glob_to_regex(glob: str) -> str:
    regex_str = "^"
    special_chars = set(".+()|^$[]{}()\\")
    for c in glob:
        if c == "*":
            regex_str += ".*"
        elif c == "?":
            regex_str += "."
        elif c in special_chars:
            regex_str += "\\" + c
        else:
            regex_str += c
    regex_str += "$"
    return regex_str


_GREP_LINE_RE = re.compile(r"^([a-zA-Z0-9_./-]+)(:|-)([0-9]+)(:|-)(.*)$")


def get_priority_score(path: str, active_files: List[str]) -> int:
    if not active_files:
        return 4

    if path in active_files:
        return 1

    path_parent = str(Path(path).parent)
    if path_parent and path_parent != ".":
        for active_file in active_files:
            active_parent = str(Path(active_file).parent)
            if active_parent and active_parent != "." and path_parent == active_parent:
                return 2

    if path.startswith("include/"):
        return 3

    return 4


def format_git_grep_output(stdout: str, revision: str, active_files: List[str]) -> str:
    prefix = f"{revision}:"

    grouped: Dict[str, List[str]] = {}
    current_file = None

    for line in stdout.split("\n"):
        if line == "--":
            if current_file and current_file in grouped:
                grouped[current_file].append("  --")
            continue

        stripped = line
        if line.startswith(prefix):
            stripped = line[len(prefix):]

        m = _GREP_LINE_RE.match(stripped)
        if m:
            path = m.group(1)
            sep1 = m.group(2)
            line_num = m.group(3)
            sep2 = m.group(4)
            content = m.group(5)

            if sep1 == sep2:
                formatted_line = f"  {line_num}{sep1}{content}"
                current_file = path
                if path not in grouped:
                    grouped[path] = []
                grouped[path].append(formatted_line)
            elif current_file:
                if current_file not in grouped:
                    grouped[current_file] = []
                grouped[current_file].append(stripped)
        elif current_file:
            if current_file not in grouped:
                grouped[current_file] = []
            grouped[current_file].append(stripped)

    blocks = sorted(grouped.items(), key=lambda x: (get_priority_score(x[0], active_files), x[0]))

    total_files = len(blocks)
    total_matches = sum(
        1
        for _, lines in blocks
        for l in lines
        if l.strip() != "--"
    )

    MAX_SUMMARY_FILES = 10
    file_summaries = []
    for path, lines in blocks[:MAX_SUMMARY_FILES]:
        count = sum(1 for l in lines if l.strip() != "--")
        label = "match" if count == 1 else "matches"
        file_summaries.append(f"{path} ({count} {label})")

    summary = ", ".join(file_summaries)
    if total_files > MAX_SUMMARY_FILES:
        summary += f", ... and {total_files - MAX_SUMMARY_FILES} more files"

    result_parts = []
    if total_files > 0:
        file_label = "file" if total_files == 1 else "files"
        match_label = "match" if total_matches == 1 else "matches"
        result_parts.append(
            f"Matches found across {total_files} {file_label} ({total_matches} total {match_label}): {summary}\n"
        )

    for path, lines in blocks:
        result_parts.append(f"[file: {path}]")
        for l in lines:
            result_parts.append(l)
        result_parts.append("")

    return "\n".join(result_parts).rstrip()