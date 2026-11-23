#!/usr/bin/env python3
"""Send one choice from a tmux menu to the conversation pane, over genai-tui's private socket.

Usage: request.py share mark|all|last
       request.py mark|unmark
       request.py menu run|edit|prompt|copy ID
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ipc import Client  # noqa: E402


def main(argv):
    if len(argv) == 2 and argv[0] == "share" and argv[1] in ("mark", "all", "last"):
        msg = {"type": "share", "what": argv[1]}
    elif len(argv) == 1 and argv[0] in ("mark", "unmark"):
        msg = {"type": "mark", "on": argv[0] == "mark"}
    elif (len(argv) == 3 and argv[0] == "menu" and argv[1] in ("run", "edit", "prompt", "copy")
          and re.fullmatch(r"p[0-9]{1,9}", argv[2])):
        msg = {"type": "menu", "action": argv[1], "id": argv[2]}
    else:
        print(__doc__, file=sys.stderr)
        return 2
    state = os.environ.get("GENAI_TUI_STATE")
    if not state:
        return 1
    try:
        c = Client(state, "menu", wait=1.0)
    except OSError:
        return 1
    ok = c.send(msg)
    c.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
