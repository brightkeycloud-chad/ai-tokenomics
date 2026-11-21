#!/usr/bin/env python3
"""Check the answer of a demo 5.2 run: did the agent find the cause of the 5xx errors?

The fault (scripts/inject-fault.sh) removes the DynamoDB policy from the ECS
task role. The answer finds the cause when it names all three parts:

    dynamodb     the failed call goes to DynamoDB
    permission   the call is refused: AccessDenied, "not authorized", or a
                 missing permission or policy
    task role    the IAM role of the application (the task role, or its name)

    check-answer.py ANSWER_FILE          prints one line for each part
    check-answer.py ANSWER_FILE --json

Exit status 0: the cause is found. 1: it is not found. Standard library only.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

PARTS = {
    "dynamodb": re.compile(r"dynamo\s*db", re.I),
    "permission": re.compile(r"access\s*denied|not\s+authori[sz]ed|permission|"
                             r"(iam|inline|role)\s+polic(y|ies)|policy\s+(is\s+)?"
                             r"(missing|removed|deleted|detached)", re.I),
    "task role": re.compile(r"(?<!execution )task[\s-]*role|status-board-task(?![\w-])", re.I),
}


def check(text: str) -> dict:
    found = {name: bool(pattern.search(text)) for name, pattern in PARTS.items()}
    return {"cause_found": all(found.values()), "parts": found}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    path = pathlib.Path(argv[0])
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    result = check(text)
    if "--json" in argv[1:]:
        print(json.dumps(result))
    else:
        for name, ok in result["parts"].items():
            print(f"  {'ok  ' if ok else 'MISS'} {name}")
        print(f"Cause found: {'yes' if result['cause_found'] else 'no'}")
    return 0 if result["cause_found"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
