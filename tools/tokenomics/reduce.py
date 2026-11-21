"""Make a reduced copy of a Claude Code session that is safe to commit.

A transcript can contain source code, file paths, and secrets. The reduced
copy keeps only the fields that `tokenomics session` reads:

    record types, time stamps, IDs, and the Claude Code version
    token counts, model IDs, and stop reasons
    content block types, tool names, and tool IDs
    the kind of a tool call ("test" for a command that runs tests) and the
    size of each tool result in bytes, with its error flag
    the permission decision of a tool call: decision, source, and reason
    type (for example "hook" when a PreToolUse hook approved the call)
    the origin kind of a prompt (human, peer, task-notification)
    the Claude Code estimate (the cost-state record)
    the agent type, the skill name, and the finished flag of each lane

It removes all prompt, response, thinking, and tool text, and the lane
descriptions. `tokenomics session` gives the same report for the copy and
for the original.

After the copy, `leftover_strings` lists every string value that is not an
ID, a time stamp, or a known field value. Read that list before you commit.
"""
from __future__ import annotations

import json
import pathlib
import re

from .toolresults import result_bytes, tool_kind

KEEP_TYPES = {"user", "assistant", "api-request", "system", "cost-state"}
RECORD_FIELDS = ("type", "timestamp", "uuid", "parentUuid", "sessionId", "version",
                 "isSidechain", "agentId", "id", "requestRef", "querySource",
                 "promptSource", "turnOrigin", "isMeta", "subtype", "toolDenialKind")
PERMISSION_FIELDS = ("decision", "source", "reasonType", "ruleSource", "reasonCode")
MESSAGE_FIELDS = ("id", "model", "role", "stop_reason", "usage")
COST_FIELDS = ("sessionId", "totalCostUSD", "totalAPIDuration", "totalAPIDurationWithoutRetries",
               "totalToolDuration", "totalLinesAdded", "totalLinesRemoved", "totalDuration",
               "startTime", "modelUsage", "hasUnknownModelCost")
META_FIELDS = ("agentType", "isBuiltIn", "spawnDepth", "requestShape", "pickedEffortModel",
               "finished")
SKILL_FIELDS = ("skillName",)
PROMPT_REMOVED = "[prompt removed]"
TOOL_REMOVED = "[removed]"

# String values that are IDs, time stamps, or versions.
_ID = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                 r"|\d{4}-\d\d-\d\dT[\d:.]+Z|(msg|toolu|req|srvtoolu)_\w+|a[0-9a-f]{16}"
                 r"|\d+\.\d+\.\d+)$")


def _block(block) -> dict:
    """Keep the type of a content block. Keep the name and ID of a tool call."""
    if not isinstance(block, dict):
        return {"type": "unknown"}
    out = {"type": block.get("type")}
    if block.get("type") == "tool_use":
        out["name"] = block.get("name")
        out["id"] = block.get("id")
        out["kind"] = tool_kind(block.get("name"), block.get("input")) or "other"
    elif block.get("type") == "tool_result":
        out["tool_use_id"] = block.get("tool_use_id")
        out["content"] = TOOL_REMOVED
        out["bytes"] = result_bytes(block)
        if "is_error" in block:
            out["is_error"] = bool(block["is_error"])
    return out


def reduce_record(record: dict) -> dict | None:
    """Return the reduced record, or None if `tokenomics session` does not read it."""
    rtype = record.get("type")
    if rtype not in KEEP_TYPES:
        return None
    if rtype == "cost-state":
        return {"type": rtype, **{k: record[k] for k in COST_FIELDS if k in record}}
    out = {k: record[k] for k in RECORD_FIELDS if k in record}
    if isinstance(record.get("origin"), dict):
        out["origin"] = {"kind": record["origin"].get("kind")}
    if isinstance(record.get("permissionDecision"), dict):
        out["permissionDecision"] = {k: record["permissionDecision"][k]
                                     for k in PERMISSION_FIELDS
                                     if k in record["permissionDecision"]}
    message = record.get("message")
    if isinstance(message, dict):
        reduced = {k: message[k] for k in MESSAGE_FIELDS if k in message}
        content = message.get("content")
        if isinstance(content, list):
            reduced["content"] = [_block(b) for b in content]
        elif content is not None:
            reduced["content"] = PROMPT_REMOVED
        out["message"] = reduced
    return out


def _reduce_jsonl(src: pathlib.Path, dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(src, encoding="utf-8") as fin, open(dest, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue         # a partly written last line
            reduced = reduce_record(record)
            if reduced is not None:
                fout.write(json.dumps(reduced, separators=(",", ":"), ensure_ascii=False) + "\n")


def _reduce_json(src: pathlib.Path, dest: pathlib.Path, fields: tuple) -> None:
    data = json.loads(src.read_text(encoding="utf-8"))
    dest.write_text(json.dumps({k: data[k] for k in fields if k in data}), encoding="utf-8")


def reduce_session(main: pathlib.Path, out_dir: pathlib.Path) -> list[pathlib.Path]:
    """Write a reduced copy of the session (the main lane and every other lane) to out_dir.

    The copy has the same layout as the original, so `tokenomics session` can
    read it. Return the files that it wrote. Do not overwrite an existing copy.
    """
    main, out_dir = pathlib.Path(main), pathlib.Path(out_dir)
    dest_main = out_dir / main.name
    dest_lanes = out_dir / main.stem
    if dest_main.exists() or dest_lanes.exists():
        raise FileExistsError(f"{dest_main} already exists. Remove it, or use another directory.")
    written = [dest_main]
    _reduce_jsonl(main, dest_main)
    lanes = main.with_suffix("") / "subagents"
    for src in sorted(lanes.glob("*.jsonl")):
        dest = dest_lanes / "subagents" / src.name
        _reduce_jsonl(src, dest)
        written.append(dest)
    for suffix, fields in ((".meta.json", META_FIELDS), (".forked-skill.json", SKILL_FIELDS)):
        for src in sorted(lanes.glob("*" + suffix)):
            dest = dest_lanes / "subagents" / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            _reduce_json(src, dest, fields)
            written.append(dest)
    return written


def _strings(value, found: set) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _strings(item, found)
    elif isinstance(value, list):
        for item in value:
            _strings(item, found)
    elif isinstance(value, str):
        found.add(value)


def leftover_strings(files: list[pathlib.Path]) -> list[str]:
    """Every string value in the files that is not an ID, a time stamp, or a version."""
    found: set = set()
    for path in files:
        text = pathlib.Path(path).read_text(encoding="utf-8")
        lines = text.splitlines() if str(path).endswith(".jsonl") else [text]
        for line in lines:
            if line.strip():
                _strings(json.loads(line), found)
    return sorted(s for s in found if not _ID.match(s))
