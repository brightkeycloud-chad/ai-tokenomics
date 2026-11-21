"""Variant overlays for the run harness.

A variant changes one thing in the start state of a run: it puts files over
the copy of the workload before the harness makes the start commit. The
agent cannot see the change in the git history: the start commit already
has the variant files.

A variant is a directory:

    <variant>/
        variant.json      {"name": "bloated-agents-md",
                           "title": "What the variant changes",
                           "workload": "status-board-app"}
        files/            the files to put over the copy, at their paths in
                          the workload (for example files/AGENTS.md)

A variant can extend another variant: "extends": "../other" in
variant.json (the path is relative to the variant directory). The harness
puts the files of the other variant over the copy first, then the files of
this variant. A file of this variant replaces a file with the same path. The
two variants must change the same workload. For example, a variant
`verbose-tests` adds a large test module and a pytest.ini. A variant
`test-output-hook` extends it with a hook. The two differ only in the hook.

Give it to the harness with `--variant DIR`, or with "variant": "DIR" in a
setup of a sweep file (the path is relative to the sweep file). The results
log records the name in the `variant` column. A run with no variant has an
empty `variant` column: that is the baseline start state.

The record of the run keeps a hash of the variant files, so that a changed
variant shows in the record.

variant.json can have two optional fields, for extensions that are
not files in the copy:

    "claude_args": ["--plugin-dir", "{copy}/plugins/x"]
                    more arguments for `claude -p`. {copy} is the copy
                    directory. Use it for --mcp-config and --plugin-dir.
    "paste": "paste/extract.txt"
                    a file (relative to the variant directory) that the
                    harness pastes into the prompt, after the task prompt:
                    the direct-file way to give the agent data. The file is
                    not put in the copy.

A variant with "paste" or "claude_args" can have no files/ directory. The
hash of the variant covers both fields and the pasted file.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
from dataclasses import dataclass

NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class Variant:
    name: str
    title: str
    workload: str
    dir: pathlib.Path
    files: tuple[str, ...]   # paths relative to files/, in sorted order
    sha: str                 # hash of the paths and the contents of the files
    base: str = ""           # the name of the variant that this variant extends
    sources: tuple[pathlib.Path, ...] = ()   # the source file of each path in files
    claude_args: tuple[str, ...] = ()        # more `claude -p` arguments ({copy} = the copy)
    paste: pathlib.Path | None = None        # a file to paste into the prompt

    def source(self, rel: str) -> pathlib.Path:
        """The file to copy for a path: from this variant, or from the variant
        that it extends."""
        if self.sources:
            return self.sources[self.files.index(rel)]
        return self.dir / "files" / rel


def file_size(data: bytes) -> tuple[int, int]:
    """(lines, bytes) of a file. A last line with no newline counts as a line."""
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return lines, len(data)


def fmt_size(lines: int, size: int) -> str:
    """'1,502 lines, 31.4 KB' (1 KB = 1,024 bytes, the unit of the Codex cap)."""
    return f"{lines:,} line{'' if lines == 1 else 's'}, {size / 1024:.1f} KB"


def load_variant(path: str | pathlib.Path, _seen: tuple = ()) -> Variant:
    root = pathlib.Path(path).expanduser()
    if root.resolve() in _seen:
        raise ValueError(f"{root}: the variants extend each other in a loop")
    meta_path = root / "variant.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"{root} is not a variant: it has no variant.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    name = meta.get("name") or root.name
    if not NAME.match(name):
        raise ValueError(f"{meta_path}: name {name!r} must be lower case letters, digits, "
                         "and hyphens")
    if not meta.get("workload"):
        raise ValueError(f"{meta_path}: give the workload that the variant changes")
    files_dir = root / "files"
    own = {p.relative_to(files_dir).as_posix(): p
           for p in files_dir.rglob("*")
           if p.is_file() and "__pycache__" not in p.parts} if files_dir.is_dir() else {}
    claude_args = meta.get("claude_args") or []
    if not isinstance(claude_args, list) or not all(isinstance(a, str) for a in claude_args):
        raise ValueError(f"{meta_path}: claude_args must be a list of strings")
    paste = None
    if meta.get("paste"):
        paste = root / meta["paste"]
        if not paste.is_file():
            raise FileNotFoundError(f"{meta_path}: no file {meta['paste']} to paste")
    if not own and not meta.get("extends") and not claude_args and paste is None:
        raise ValueError(f"{root}: files/ has no files")
    sources, base_name = dict(own), ""
    if meta.get("extends"):
        base = load_variant(root / meta["extends"], _seen + (root.resolve(),))
        if base.workload != meta["workload"]:
            raise ValueError(f"{meta_path}: it changes {meta['workload']}, but the variant "
                             f"{base.name} that it extends changes {base.workload}")
        sources = {rel: base.source(rel) for rel in base.files} | own
        base_name = base.name
    files = sorted(sources)
    h = hashlib.sha256()
    for rel in files:
        h.update(rel.encode() + b"\0" + sources[rel].read_bytes() + b"\0")
    if claude_args:
        h.update(b"claude_args\0" + json.dumps(claude_args).encode() + b"\0")
    if paste is not None:
        h.update(b"paste\0" + paste.read_bytes() + b"\0")
    return Variant(name, meta.get("title", name), meta["workload"], root, tuple(files),
                   h.hexdigest()[:12], base_name, tuple(sources[rel] for rel in files),
                   tuple(claude_args), paste)


def compare_files(variant: Variant, read_old) -> list[dict]:
    """For each variant file: its size, and the size of the workload file that it
    replaces. read_old(path) gives the bytes of the workload file, or None when
    the workload has no such file."""
    out = []
    for rel in variant.files:
        new = variant.source(rel).read_bytes()
        old = read_old(rel)
        out.append({"path": rel, "lines": file_size(new)[0], "bytes": len(new),
                    "old_lines": None if old is None else file_size(old)[0],
                    "old_bytes": None if old is None else len(old)})
    return out


def describe_files(compared: list[dict]) -> list[str]:
    lines = []
    for f in compared:
        text = f"{f['path']}: {fmt_size(f['lines'], f['bytes'])}"
        if f["old_bytes"] is None:
            text += " (a new file)"
        else:
            text += (f" (replaces {fmt_size(f['old_lines'], f['old_bytes'])}: "
                     f"{f['bytes'] / f['old_bytes']:.1f}× the bytes)")
        lines.append(text)
    return lines


def apply_variant(variant: Variant, copy_dir: pathlib.Path) -> list[str]:
    """Put the variant files over the copy. Return the paths."""
    for rel in variant.files:
        target = copy_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(variant.source(rel), target)
    return list(variant.files)


def expand_args(variant: Variant | None, copy_dir: pathlib.Path) -> list[str]:
    """The `claude -p` arguments of a variant, with {copy} changed to the copy."""
    if variant is None:
        return []
    return [a.replace("{copy}", str(copy_dir)) for a in variant.claude_args]


def pasted_prompt(prompt: str, variant: Variant | None) -> str:
    """The task prompt, and after it the pasted file of the variant, if any."""
    if variant is None or variant.paste is None:
        return prompt
    text = variant.paste.read_text(encoding="utf-8")
    return (prompt.rstrip("\n") + f"\n\nPasted file {variant.paste.name}:\n\n```text\n"
            + text.rstrip("\n") + "\n```\n")
