"""The reference: what report.py should print (for check.sh; the model never sees this file)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "repo"))
import report  # noqa: E402


def as_json(users):
    def entry(runs, failed, minutes):
        return {"runs": runs, "failed": failed, "failure_rate": round(failed / runs, 2), "hours": round(minutes / 60, 1)}
    rows = []
    for name in sorted(users, key=lambda n: (-users[n]["runs"], n)):
        u = users[name]
        rows.append({"name": name, **entry(u["runs"], u["failed"], u["minutes"]), "last_run": u["last"][:10]})
    t = entry(sum(u["runs"] for u in users.values()), sum(u["failed"] for u in users.values()),
              sum(u["minutes"] for u in users.values()))
    return json.dumps({"version": 2, "users": rows,
                       "totals": {"runs": t["runs"], "failed": t["failed"], "hours": t["hours"]}})


users = report.summarize(report.load(sys.argv[2]))
print(report.text(users) if sys.argv[1] == "text" else as_json(users))
