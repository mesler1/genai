"""Screenshots of hoplon-tui for the docs: a throwaway tmux server, a fake model server, a temporary config, so
nothing of yours shows. It reads the terminal hoplon-tui draws into pyte, writes each scene as HTML, and turns
that into a PNG with headless Chromium and ImageMagick.

    python3 -m venv /tmp/shots && /tmp/shots/bin/pip install pyte
    /tmp/shots/bin/python docs/images/screenshots.py          # writes docs/images/tui-*.png
"""

import fcntl
import html
import socket
import json
import os
import pty
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

REPO = os.path.expanduser("~/code/genai")
sys.path.insert(0, os.path.join(REPO, "tests"))
import pyte  # noqa: E402
from fakeapi import FakeAPI, call, reply  # noqa: E402
from test_hoplon import G  # noqa: E402

OUT = os.path.dirname(os.path.abspath(__file__))
WORK = tempfile.mkdtemp(prefix="hoplon-shot-pages-")       # the HTML pages, before they become PNGs
COLS, ROWS = 150, 42
SOCK = f"hoplon-shots-{os.getpid()}"

SOURCE = open(os.path.join(REPO, "bench/tasks/parser-fix/repo/durations.py")).read()
OLD = SOURCE[SOURCE.index("    total = 0"):SOURCE.index("    return total") + len("    return total")]
NEW = '''    if not re.fullmatch(r" *(?:\\d+[smhd] *)+", text):
        raise ValueError(f"not a duration: {text!r}")
    return sum(int(n) * UNITS[u] for n, u in re.findall(r"(\\d+)([smhd])", text))'''

TURNS = [
    call("read_file", path="durations.py", reason="to see how parse() reads a duration"),
    call("edit_file", path="durations.py", old_text=OLD, new_text=NEW,
         reason="parse() skips anything it doesn't recognise, so '1h 2' reads as one hour; match the whole text "
                "first and refuse what isn't a duration"),
    reply("Fixed. `parse()` used `re.findall`, which skips whatever it can't match, so `\"1h 2\"` came back as "
          "3600 and `\"m5\"` as 0.\n\nIt now checks the whole text first and raises `ValueError` for anything that "
          "isn't a run of number-and-unit pairs:\n\n- `parse(\"1h 30m\")` is still 5400\n- `parse(\"1h 2\")`, "
          "`parse(\"m5\")` and `parse(\"1hh\")` raise\n\nRun the tests with `python3 -m unittest discover -s tests`."),
]
MODELS = [{"id": "small-coder-7b", "max_model_len": 32000}, {"id": "coder-32b-instruct", "max_model_len": 128000},
          {"id": "coder-235b-a22b-thinking", "max_model_len": 256000}]
ALIASES = {"low": "small-coder-7b", "medium": "coder-32b-instruct", "high": "coder-235b-a22b-thinking"}


def tmux(*args):
    return subprocess.run(["tmux", "-L", SOCK, *args], capture_output=True, text=True).stdout


def panes():
    out = tmux("list-panes", "-a", "-F", "#{pane_id} #{@hoplon_title}")
    return {t.split(" ")[0].split("·")[0].strip(): p for p, _, t in (ln.partition(" ") for ln in out.splitlines())}


def wait_for(screen, text, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if text in "\n".join(screen.display):
            time.sleep(0.6)
            return
        time.sleep(0.1)
    raise SystemExit(f"{text!r} never appeared:\n" + "\n".join(screen.display))


PALETTE = {"black": "#0e1113", "red": "#e06c75", "green": "#98c379", "brown": "#d4a05a", "yellow": "#e5c07b",
           "blue": "#61afef", "magenta": "#c678dd", "cyan": "#56b6c2", "white": "#c8ccd4",
           "brightblack": "#5c6370", "brightred": "#e06c75", "brightgreen": "#98c379", "brightyellow": "#e5c07b",
           "brightblue": "#61afef", "brightmagenta": "#c678dd", "brightcyan": "#56b6c2", "brightwhite": "#ffffff"}
FG, BG = "#d7dae0", "#16191c"


def color(c, default):
    if c == "default":
        return default
    if c in PALETTE:
        return PALETTE[c]
    if len(c) == 6 and all(x in "0123456789abcdefABCDEF" for x in c):
        return "#" + c
    return default


HOST = socket.gethostname()


def is_wide(c):
    import unicodedata
    return unicodedata.east_asian_width(c) in ("W", "F")


def cells(text):
    """ASCII as it is; anything else in a box exactly as wide as its cells, whatever the font draws."""
    text = text.replace(HOST, ("workstation" if len(HOST) >= 11 else "work").ljust(len(HOST)))   # not this machine's name
    out = []
    for c in text:
        if ord(c) < 128:
            out.append(html.escape(c))
        else:
            w = 2 if is_wide(c) else 1
            out.append(f'<i style="display:inline-block;width:{w}ch;font-style:inherit;text-align:center;font-family:DejaVu Sans Mono;'
                       f'vertical-align:top">{html.escape(c)}</i>')
    return "".join(out)


def save(screen, name, title):
    rows = []
    for y in range(screen.lines):
        line = screen.buffer[y]
        spans, cur, buf = [], None, []
        for x in range(screen.columns):
            ch = line[x]
            if x > 0 and line[x - 1].data and is_wide(line[x - 1].data) and not ch.data:
                continue                        # the second cell of a wide character
            fg, bg = color(ch.fg, FG), color(ch.bg, BG)
            if ch.reverse:
                fg, bg = bg, fg
            style = (fg, bg, ch.bold, ch.italics, ch.underscore)
            if style != cur and buf:
                spans.append((cur, "".join(buf)))
                buf = []
            cur = style
            buf.append(ch.data or " ")
        spans.append((cur, "".join(buf)))
        rows.append("".join(
            f'<span style="color:{s[0]};background:{s[1]};{"font-weight:700;" if s[2] else ""}'
            f'{"font-style:italic;" if s[3] else ""}{"text-decoration:underline;" if s[4] else ""}">'
            f"{cells(t)}</span>" for s, t in spans))
    page = f"""<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
html,body{{margin:0;background:#0b0d0f}}
.win{{margin:28px;display:inline-block;border-radius:10px;overflow:hidden;box-shadow:0 10px 40px #0008;
  border:1px solid #2a2f35}}
.bar{{background:#1e2226;height:30px;display:flex;align-items:center;padding:0 12px;gap:8px}}
.dot{{width:12px;height:12px;border-radius:50%}}
.ttl{{color:#8b939c;font:13px system-ui,sans-serif;margin-left:12px}}
pre{{margin:0;padding:10px 12px;background:{BG};font:15px/1.3 'JetBrainsMono Nerd Font','JetBrains Mono',monospace;
  font-variant-ligatures:none}}
pre span{{white-space:pre}}
</style>
<div class="win"><div class="bar"><span class="dot" style="background:#ff5f57"></span>
<span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span>
<span class="ttl">{html.escape(title)}</span></div><pre>{chr(10).join(rows)}</pre></div>"""
    page_path = os.path.join(WORK, name + ".html")
    with open(page_path, "w") as f:
        f.write(page)
    raw, png = os.path.join(WORK, name + ".png"), os.path.join(OUT, f"tui-{name}.png")
    subprocess.run(["chromium", "--headless=new", "--disable-gpu", f"--user-data-dir={WORK}/chrome",
                    "--hide-scrollbars", "--force-device-scale-factor=2", "--window-size=1500,1050",
                    f"--screenshot={raw}", "file://" + page_path], capture_output=True, check=True)
    subprocess.run(["magick", raw, "-fuzz", "2%", "-trim", "+repage", "-bordercolor", "#0b0d0f", "-border", "40",
                    png], check=True)
    print("wrote", png)


def main():
    tmp = tempfile.mkdtemp(prefix="hoplon-shots-")
    runtime = tempfile.mkdtemp(prefix="hs-", dir="/tmp")
    os.chmod(runtime, 0o700)
    try:
        with FakeAPI(TURNS, models=MODELS) as api:
            home = os.path.join(tmp, "cfg", "hoplon")
            os.makedirs(os.path.join(home, "endpoints"))
            os.makedirs(os.path.join(home, "profiles"))
            ep = os.path.join(home, "endpoints", "work-llm.json")
            with open(ep, "w") as f:
                json.dump({"name": "work-llm", "base_url": api.url, "model": "medium", "aliases": ALIASES,
                           "context_limit": 128000}, f)
            prof = os.path.join(home, "profiles", "coder.json")
            with open(prof, "w") as f:
                json.dump({"name": "coder", "endpoint": "work-llm", "coding": True}, f)
            cfg = G.Config(home)
            cfg.ensure()
            cfg.set_blessed(ep, {"kind": "endpoint", "name": "work-llm", "hash": G.hash_file(ep)})
            cfg.set_blessed(prof, {"kind": "profile", "name": "coder", "hash": G.hash_file(prof)})
            wd = os.path.join(tmp, "durations")
            shutil.copytree(os.path.join(REPO, "bench/tasks/parser-fix/repo"), wd)
            env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home), HOME=tmp, LANG="C.UTF-8",
                       TERM="xterm-256color", HOPLON_TUI_SOCKET=SOCK, EDITOR="true", VISUAL="true",
                       XDG_RUNTIME_DIR=runtime, PS1="durations$ ")
            env.pop("TMUX", None)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=wd, env=env)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
            proc = subprocess.Popen([os.path.join(REPO, "tui/hoplon-tui"), "-p", "coder"], cwd=wd, env=env,
                                    stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            class Screen(pyte.Screen):
                def report_device_status(self, *a, **k):
                    pass

                def report_device_attributes(self, *a, **k):
                    pass

            screen = Screen(COLS, ROWS)
            stream = pyte.ByteStream(screen)
            lock = threading.Lock()

            def pump():
                while True:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        return
                    if not data:
                        return
                    with lock:
                        stream.feed(data)
                    if b"\x1b[c" in data or b"\x1b[>c" in data:     # answer device attribute queries
                        os.write(master, b"\x1b[?62;22c")
            threading.Thread(target=pump, daemon=True).start()
            try:
                wait_for(screen, "Type a message")
                p = panes()
                you = p["you"]
                tmux("send-keys", "-t", you, "-l",
                     "parse() accepts garbage like \"1h 2\" and \"m5\". Can you make it refuse them?")
                tmux("send-keys", "-t", you, "Enter")
                wait_for(screen, "edit")
                time.sleep(1.5)
                with lock:
                    save(screen, "approval", "hoplon-tui: an edit waiting for approval")
                time.sleep(0.6)
                tmux("send-keys", "-t", you, "y")
                wait_for(screen, "Run the tests with")
                time.sleep(1)
                with lock:
                    save(screen, "done", "hoplon-tui: the edit made, the model's answer")
                tmux("send-keys", "-t", you, "-l", "/model")
                tmux("send-keys", "-t", you, "Enter")
                wait_for(screen, "Cancel", 10)
                time.sleep(1)
                with lock:
                    save(screen, "models", "hoplon-tui: model aliases in the model menu")
                tmux("send-keys", "-t", you, "Escape")
            finally:
                tmux("kill-server")
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
