#!/usr/bin/env python3
"""Live test of hoplon against a real endpoint. Meant to run inside the sandbox:

    sandbox/run live PATH/TO/ENDPOINT.json

It refuses to run outside a sandbox unless --allow-host is given. It uses its own
throwaway hoplon config (the profile and skills are blessed there by the harness), so
the user's real ~/.config/hoplon is never touched.

Checks:
  1. the endpoint answers and lists the profile's model
  2. CLI: hoplon writes a bash script to stdout, redirected to a file; the script is
     syntax-checked with `bash -n` and NOT run
  3. JSON: hello reports the sandbox; a write_file proposal is accepted and performed
     by hoplon; a command proposal ends the turn and nothing is run
  4. profile: a blessed persona profile picks the endpoint by itself (no -e) and its
     persona text reaches the model
"""

import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "hoplon")
if "--endpoint" not in sys.argv:
    sys.exit("usage: live_endpoint.py --endpoint PATH [--allow-host]")
ENDPOINT = sys.argv[sys.argv.index("--endpoint") + 1]
PERSONA = "live-persona"

_loader = importlib.machinery.SourceFileLoader("hoplon_mod", SCRIPT)
_spec = importlib.util.spec_from_loader("hoplon_mod", _loader)
G = importlib.util.module_from_spec(_spec)
_loader.exec_module(G)

results = []
NAME = None                          # the endpoint's name, read from the file


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)
    return ok


def setup_home():
    base = tempfile.mkdtemp(prefix="hoplon-live-")
    home = os.path.join(base, "hoplon")
    os.makedirs(home, mode=0o700)
    with open(os.path.join(home, "config.json"), "w") as f:
        json.dump({"skill_dirs": [os.path.join(REPO, "skills")]}, f)
    cfg = G.Config(home)
    ep = os.path.abspath(ENDPOINT)
    cfg.set_blessed(ep, {"kind": "endpoint", "name": NAME, "hash": G.hash_file(ep)})
    os.makedirs(os.path.join(home, "profiles"), mode=0o700)
    persona = os.path.join(home, "profiles", PERSONA + ".json")
    with open(persona, "w") as f:
        json.dump({"name": PERSONA, "endpoint": NAME, "skills": [], "params": {"temperature": 0},
                   "persona": "Begin every reply with the word PINEAPPLE in capital letters, then answer."}, f)
    cfg.set_blessed(persona, {"kind": "profile", "name": PERSONA, "hash": G.hash_file(persona)})
    skill = os.path.join(REPO, "skills", "bash")
    cfg.set_blessed(skill, {"kind": "skill", "name": "bash", "hash": G.hash_skill(skill)[0]})
    return base, home


def strip_fences(text):
    m = re.search(r"```[a-z]*\n(.*?)```", text, re.S)
    return (m.group(1), True) if m else (text, False)


def test_models(prof):
    print("1. endpoint", flush=True)
    t = time.time()
    data = G.Client(prof).models()
    ids = [m.get("id") for m in data.get("data", [])]
    check("GET /models answers", bool(ids), f"{len(ids)} model(s) in {time.time() - t:.1f}s: {', '.join(ids)[:200]}")
    if prof.model not in ids:
        print(f"  [note] profile model '{prof.model}' is not in the list; the server may ignore the name")


def test_cli_script(env, work):
    print("2. CLI: script to a file with stdout redirection (not run)", flush=True)
    out = os.path.join(work, "rotate-logs.sh")
    prompt = ("Write a bash script that deletes *.log files older than 7 days in a directory given as the "
              "first argument (default ./logs), with a --dry-run flag. Output only the script.")
    cmd = f"python3 {SCRIPT} -e {NAME} -k bash {json.dumps(prompt)} > {out}"
    before = set(os.listdir(work))
    t = time.time()
    r = subprocess.run(["sh", "-c", cmd], cwd=work, env=env, stdin=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True, timeout=600)
    check("hoplon exited 0", r.returncode == 0, f"{time.time() - t:.1f}s" + (f"; stderr: {r.stderr[-300:]}" if r.returncode else ""))
    raw = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
    script, fenced = strip_fences(raw)
    check("output file has a script", script.lstrip().startswith("#!"),
          f"{len(raw.splitlines())} lines" + ("; model wrapped it in a Markdown fence" if fenced else ""))
    syn = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    check("bash -n accepts it", syn.returncode == 0, syn.stderr.strip()[:200])
    check("script was not run or made executable", not os.access(out, os.X_OK) and not os.path.exists(
        os.path.join(work, "logs")))
    check("no other files created", set(os.listdir(work)) - before == {"rotate-logs.sh"},
          str(sorted(set(os.listdir(work)) - before)))
    print("  ---- rotate-logs.sh ----")
    print("  " + "\n  ".join(raw.splitlines()[:40]))
    print("  ------------------------", flush=True)


class Json:
    def __init__(self, env, work):
        self.p = subprocess.Popen([sys.executable, SCRIPT, "--json", "-e", NAME], cwd=work, env=env,
                                  text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.events = []

    def send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def next(self):
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("hoplon closed stdout: " + self.p.stderr.read()[-500:])
        ev = json.loads(line)
        self.events.append(ev)
        return ev

    def turn(self, text, decide):
        """Send a user message; answer proposals with decide(ev); return (proposals, done-or-error)."""
        self.send({"type": "user", "text": text})
        props = []
        while True:
            ev = self.next()
            if ev["type"] == "proposal":
                props.append(ev)
                if ev["kind"] != "command" and not ev.get("auto"):
                    self.send({"type": "decision", "id": ev["id"], "digest": ev["digest"], "decision": decide(ev)})
            elif ev["type"] in ("done", "error"):
                return props, ev

    def close(self):
        self.p.stdin.close()
        self.p.wait(30)


def test_json(env, work):
    print("3. JSON mode: proposals and decisions", flush=True)
    g = Json(env, work)
    hello = g.next()
    check("hello reports the sandbox", bool(hello.get("sandbox")), json.dumps(hello.get("sandbox")))
    props, end = g.turn("Create a file named hello.txt containing exactly the line: hello from hoplon. "
                        "Use the write_file tool.", lambda ev: "accept" if ev["kind"] == "write" else "decline")
    writes = [p for p in props if p["kind"] == "write"]
    check("model proposed write_file", bool(writes), f"{[p['kind'] for p in props]} then {end['type']}")
    path = os.path.join(work, "hello.txt")
    content = open(path).read() if os.path.exists(path) else ""
    check("hoplon wrote the accepted file", "hello from hoplon" in content, repr(content[:80]))
    props, end = g.turn("Propose a command that shows the disk usage of the current directory. "
                        "Use propose_command.", lambda ev: "decline")
    cmds = [p for p in props if p["kind"] == "command"]
    check("model proposed a command", bool(cmds), cmds[0]["command"] if cmds else str([p["kind"] for p in props]))
    check("command proposal ended the turn", end.get("reason") == "command", json.dumps(end)[:200])
    g.close()


def test_profile(env, work):
    print("4. profile: persona picks the endpoint and shapes the reply", flush=True)
    r = subprocess.run([sys.executable, SCRIPT, "-p", PERSONA, "What is 2 + 2? One line."], cwd=work, env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
    check("hoplon -p (no -e) exited 0", r.returncode == 0, r.stderr.strip()[-300:])
    check("persona reached the model", "PINEAPPLE" in r.stdout.upper()[:200], repr(r.stdout.strip()[:120]))


def main():
    if not G.sandbox_info() and "--allow-host" not in sys.argv:
        sys.exit("live_endpoint: not inside a sandbox (run it with sandbox/run live, or pass --allow-host)")
    global NAME
    prof = G.Endpoint.load(ENDPOINT)
    NAME = prof.name
    print(f"hoplon live test: {prof.base_url} model {prof.model}; commands would run "
          f"{G.where_runs(G.sandbox_info())}", flush=True)
    base, home = setup_home()
    work = tempfile.mkdtemp(prefix="work-", dir=os.environ.get("HOPLON_LIVE_WORK", "/work" if os.path.isdir("/work") else None))
    env = dict(os.environ, XDG_CONFIG_HOME=base)
    try:
        test_models(prof)
        test_cli_script(env, work)
        test_json(env, work)
        test_profile(env, work)
    except Exception as e:  # report, don't hide
        check("no unexpected error", False, f"{type(e).__name__}: {e}")
    finally:
        audit = os.path.join(home, "audit.log")
        n = sum(1 for _ in open(audit)) if os.path.exists(audit) else 0
        print(f"audit log: {n} records")
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
