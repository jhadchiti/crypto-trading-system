"""
Decision Journal — the log of every non-mechanical choice.
===========================================================

Mechanical decisions (entries, exits, sizing) are the system's. Everything
else — halting, resuming, registering unlock events, review verdicts,
capital changes, rule changes — gets an entry HERE, at decision time, with
your reasoning and expectation. Reviewed quarterly.

Why: hindsight rewrites memory. The journal is the only honest record of
what you knew and expected when you decided. Reviewing your own past
predictions is the fastest known way to find your bias patterns.

Usage:
    python decision_journal.py add "decision" "reasoning" "expected outcome"
    python decision_journal.py list             # newest first
    python decision_journal.py review           # entries older than 90d, for scoring

Example:
    python decision_journal.py add "Registered PROVE unlock short" ^
        "160% supply unlock Aug 5, funding not crowded" ^
        "expect +2-6% net; stop risk if squeeze"
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

JOURNAL = Path("decision_journal.jsonl")


def add(decision: str, reasoning: str, expectation: str) -> None:
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "reasoning": reasoning,
        "expectation": expectation,
        "outcome": None,          # fill during quarterly review
    }
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"logged: {decision}")


def entries() -> list[dict]:
    if not JOURNAL.exists():
        return []
    out = []
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def list_entries() -> None:
    es = entries()
    if not es:
        print("journal empty. Log decisions with:\n"
              '  python decision_journal.py add "decision" "reasoning" "expectation"')
        return
    for e in reversed(es[-20:]):
        print(f"[{e['time'][:10]}] {e['decision']}")
        print(f"    why: {e['reasoning']}")
        print(f"    expected: {e['expectation']}")
        if e.get("outcome"):
            print(f"    OUTCOME: {e['outcome']}")


def review() -> None:
    import pandas as pd
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=90)
    due = [e for e in entries()
           if pd.Timestamp(e["time"]) < cutoff and not e.get("outcome")]
    if not due:
        print("no entries due for review (90d+ old, unscored)")
        return
    print(f"{len(due)} decision(s) due for scoring — for each, compare what you "
          f"expected vs what happened, and note the gap:")
    for e in due:
        print(f"\n[{e['time'][:10]}] {e['decision']}")
        print(f"    expected: {e['expectation']}")
        print(f"    -> what actually happened? (edit {JOURNAL} 'outcome' field)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "add" and len(a) == 4:
        add(a[1], a[2], a[3])
    elif a and a[0] == "review":
        review()
    else:
        list_entries()
