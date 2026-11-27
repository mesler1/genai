#!/usr/bin/env python3
"""A report of batch runs from a CSV file: one line per user, then the totals.

    python3 report.py data/runs.csv
"""

import csv
import sys


def load(path):
    with open(path, newline="") as f:
        return [{"user": r["user"], "job": r["job"], "state": r["state"], "minutes": int(r["minutes"]),
                 "ended": r["ended"]} for r in csv.DictReader(f)]


def summarize(runs):
    users = {}
    for r in runs:
        u = users.setdefault(r["user"], {"runs": 0, "failed": 0, "minutes": 0, "last": ""})
        u["runs"] += 1
        u["failed"] += r["state"] != "COMPLETED"
        u["minutes"] += r["minutes"]
        u["last"] = max(u["last"], r["ended"])
    return users


def text(users):
    lines = [f"{'user':<10}{'runs':>6}{'failed':>8}{'hours':>8}  last run"]
    for name in sorted(users):
        u = users[name]
        lines.append(f"{name:<10}{u['runs']:>6}{u['failed']:>8}{u['minutes'] / 60:>8.1f}  {u['last']}")
    total = sum(u["minutes"] for u in users.values())
    lines.append(f"{'total':<10}{sum(u['runs'] for u in users.values()):>6}"
                 f"{sum(u['failed'] for u in users.values()):>8}{total / 60:>8.1f}")
    return "\n".join(lines)


def main(argv):
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    print(text(summarize(load(argv[0]))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
