#!/usr/bin/env python3
"""Run the live test's harness against a scripted fake endpoint, so a broken harness
is caught without a key or network. Run: sandbox/run selfcheck"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fakeapi import FakeAPI, call, reply  # noqa: E402

SCRIPT_TEXT = "#!/usr/bin/env bash\nset -euo pipefail\n# delete old logs\ndir=${1:-./logs}\necho \"$dir\"\n"
turns = [reply(SCRIPT_TEXT),
         call("write_file", path="hello.txt", content="hello from hoplon\n", reason="asked"), reply("Done."),
         call("propose_command", command="du -sh .", reason="disk usage"),
         reply("PINEAPPLE. 4")]

with FakeAPI(turns) as api:
    d = tempfile.mkdtemp(prefix="selfcheck-")
    prof = os.path.join(d, "fake.json")
    with open(prof, "w") as f:
        json.dump({"name": "fake", "base_url": api.url, "model": "fake-model", "context_limit": 100000}, f)
    r = subprocess.run([sys.executable, os.path.join(HERE, "live_endpoint.py"), "--endpoint", prof])
    sys.exit(r.returncode)
