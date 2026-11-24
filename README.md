# genai

A small command-line client for OpenAI-compatible APIs, standard library only, Python 3.10+.
**The model never executes anything on your system:** it proposes a command or a file operation, and
you decide. The one exception is opt-in: in coding mode, a profile can give it a sandbox, an offline
container working on a throwaway copy of the project, to build and test in
([Sandboxed runs](#sandboxed-runs)).
See [DECISIONS.md](DECISIONS.md) for the design choices and known limitations, and
[SECURITY.md](SECURITY.md) for the risks of a tool like this and how genai handles each.

## Quick start

```bash
cp endpoints/example.json ~/.config/genai/endpoints/work-llm.json   # edit name, base_url, model, key_env
genai --bless ~/.config/genai/endpoints/work-llm.json   # shows URL, model, key source; asks you to confirm
export GENAI_API_KEY=...                    # the endpoint names the variable, never the key
genai -e work-llm "what does 'set -euo pipefail' do?"
genai --bless profiles/coder.json           # a persona: shows its text, endpoint, skills, coding grant
genai -e work-llm -p coder                  # REPL: a line with only '..' sends; :help for commands
```

## Endpoints and profiles

An **endpoint** says where prompts go: URL, model, key source, context size. It is the
security-relevant file, because it decides where your prompts, files and key are sent.

A **profile** is a persona. It can name an endpoint and add standing instructions for the
model, default skills, sampling settings and coding mode:

```json
{
  "name": "coder",
  "description": "Careful senior engineer: small diffs, tests first",
  "endpoint": "work-llm",
  "persona": "You are a careful senior software engineer pairing with the user. ...",
  "skills": ["bash-scripts"],
  "coding": true
}
```

- `-p NAME` picks a profile and `-e NAME` picks an endpoint. `-e` overrides the profile's endpoint, and
  `genai -e NAME` works with no profile at all. A profile without an endpoint (like the bundled
  `coder`) uses `-e` or `default_endpoint`.
- The persona text is sent after genai's own rules and can't change them: the model still only proposes.
- `skills` replaces `default_skills` from `config.json`. Leave it out to keep the defaults; `[]` means
  none. `-k` always adds more.
- A profile's `params` override the endpoint's. Put the model maker's recommended sampling settings in
  the endpoint, and only change them in a profile for a reason.
- `"coding": true` turns coding mode on, as `--coding` does. A project directory under your home is
  fine. It is refused in `~` itself, `/`, genai's config directory and credential directories such as
  `~/.ssh`, where genai prints a notice and leaves it off. Only you can override that, with
  `--coding-anywhere` on the command line. The REPL banner and the JSON `hello` event say where coding
  mode came from.
- Endpoints, profiles and skills are each blessed with `genai --bless PATH`. A profile can only use an
  endpoint and skills that are blessed on their own. If any file changes, genai refuses it until you
  bless it again.

| Use | Command |
| --- | --- |
| Attach files | `genai -p NAME -f app.log -f app.conf "why is it crashing?"` |
| Skills | `genai -p NAME -k slurm` (skills must be blessed) |
| Plugins (trusted commands) | `genai -p NAME --plugin slurm` (plugins must be blessed; [Plugins and trusted commands](#plugins-and-trusted-commands)) |
| Sessions | `genai -p NAME -S nightly "summarize" > out.txt` |
| Coding mode | `genai -e NAME --coding` (reads stop asking, except files that look like secrets; writes still ask) |
| Coding mode where it is refused | `genai -e NAME --coding-anywhere` (prints what that exposes) |
| Builds and tests the model runs itself | a profile with `"coding": true` and a `"sandbox"` ([Sandboxed runs](#sandboxed-runs)) |
| Start over, keeping the session | `:clear` in the REPL, `/clear` in genai-tui (the model forgets the conversation; the saved session keeps it) |
| See exactly what was sent | `:raw [N\|all]` and `:system` in the REPL, `/raw` and `/system` in genai-tui (in `$PAGER`, default `less`; your API key is never shown) |
| Wrappers | `genai -p NAME --json` (JSON Lines over stdio) |
| Lists | `genai --list-endpoints`, `--list-profiles`, `--list-skills`, `--list-plugins`, `--list-sessions` (each with `--json`) |

Exit codes: 0 ok, 1 error, 2 usage, 3 endpoint or profile (none, unblessed, changed, key missing),
4 endpoint request failed, 5 context full, 130 interrupted.

## Files

```
genai                      the client (one file)
endpoints/example.json     endpoint template: copy, fill in, bless
profiles/coder.json        example persona with coding mode
skills/genai-endpoint/     helps you write an endpoint
skills/genai-profile/      helps you write a profile (persona)
skills/genai-skill/        helps you write a skill
skills/bash-scripts/       conventions for small, safe bash scripts
skills/hpc/                working on a Slurm cluster: login nodes, srun/sbatch, modules, scratch
plugins/slurm/             trusted read-only Slurm commands: sinfo, squeue, scontrol show, sacct
sandbox/run                podman sandbox for running genai and its tests, on this machine's OS
sandbox/hooks/modules.sh   sandbox hook: makes loaded environment modules work inside the sandbox
hooks/notify-desktop       example notify hook: a desktop notification when genai waits for you
tests/                     offline suite, live endpoint test, harness self-check
```

## Configuration

On first run genai creates `~/.config/genai` (or `$XDG_CONFIG_HOME/genai`), mode 0700, with
`endpoints/`, `profiles/`, `skills/`, `plugins/`, `sessions/` and a `config.json` that lists every setting at its
default. It says so once on stderr and never changes what is already there.

| Setting | Default | Meaning |
| --- | --- | --- |
| `default_profile` | `null` | Profile used when `-p` is not given. |
| `default_endpoint` | `null` | Endpoint used when neither `-e` nor the profile names one. |
| `default_skills` | `[]` | Skills used when the profile doesn't list its own. |
| `default_plugins` | `[]` | Plugins used when the profile doesn't list its own (`--plugin` adds more). |
| `endpoint_dirs` | `[]` | Extra directories searched for endpoints, after `~/.config/genai/endpoints/`. |
| `profile_dirs` | `[]` | Extra directories searched for profiles, after `~/.config/genai/profiles/`. |
| `skill_dirs` | `[]` | Extra directories searched for skills, after `~/.config/genai/skills/`. |
| `plugin_dirs` | `[]` | Extra directories searched for plugins, after `~/.config/genai/plugins/`. |

Keys starting with `_` are comments. Any other unknown key gets a warning, so a typo isn't silently
ignored. A blessed endpoint or profile is also found by name wherever it lives, so you can bless the
repo's files in place. For example, to use the coder persona by default and the bundled skills:

```json
{"default_profile": "coder", "default_endpoint": "work-llm", "skill_dirs": ["/path/to/genai/skills"]}
```

```bash
genai --bless /path/to/genai/profiles/coder.json
genai --bless /path/to/genai/skills/bash-scripts
```

## Plugins and trusted commands

A plugin is a directory with a `plugin.json` (and, for genai-tui, optionally a pane program). It defines
**trusted commands**: narrow, shell-free programs with named inputs, such as `squeue -h -u {user}`. You bless
the whole directory once, like a skill (`genai --bless plugins/slurm` shows every command, what each input
may be, which program it would run, and which ones ask every time), and genai refuses it again if any file in
it changes. Then `--plugin slurm`, `default_plugins`, a profile's `plugins`, or a `plugin` message from a
wrapper turns it on.

Each enabled command is a tool for the model, named `slurm__queue_user`. A command marked `"read_only": true`
runs without asking, like an automatic read in coding mode: you see what ran and its output, and the audit log
records it. Any other command asks every time, like a write. You (a genai-tui pane, through the `run` message)
can run the same commands with the same checks and limits. You decided once, at blessing time, exactly these
commands with exactly these inputs.

```json
{"name": "slurm", "description": "Slurm cluster status", "search": ["/usr/bin", "/opt/slurm/bin"], "env": ["SLURM_CONF"],
 "commands": {"queue_user": {"describe": "jobs of one user", "argv": ["squeue", "-h", "-u", "{user}"],
   "params": {"user": {"pattern": "^[a-z_][a-z0-9_-]{0,31}$"}}, "read_only": true, "min_interval": 5, "cache": 15}}}
```

- `name` is the directory's name (`a-z`, digits, `-`); commands are `a-z`, digits, `_`. Unknown fields anywhere
  refuse the plugin. `pane` (optional) names a file in the directory for genai-tui; genai only reports it.
- `argv`: a program name without `/` (found only in `search`, default `/usr/local/bin`, `/usr/bin`, `/bin`;
  your `PATH` is never used) or an absolute path, then arguments. `{param}` may be a whole argument or part of
  one (`--user={user}`); every placeholder needs a declared param and every param must be used. `{{` and `}}`
  are literal braces. A value never splits into several arguments and is never seen by a shell.
- `params` (all required): `pattern` (a regular expression the whole value must match; one that matches the
  empty string is refused), `enum` (a list), or `int` (`[min, max]`); optional `describe`. A value that doesn't
  fit is refused, never cleaned. At most 256 characters, one line, whatever the pattern says.
- Per command: `read_only` (default `false`), `timeout` (seconds, 15, at most 120), `max_output` (bytes,
  65536, at most 1 MiB; more is dropped and the program is killed), `min_interval` (seconds, 2: a call of the
  same command sooner than this after its last real run is refused with "rate limited"), `cache` (seconds, 0:
  the same command with the same values returns its last result, marked cached). genai also allows 30 real
  runs a minute across all plugins.
- `env`: names of environment variables passed on. The program otherwise gets only `PATH` (the `search`
  directories), `LANG=C.UTF-8`, `HOME`, `USER` and `LOGNAME`. `PATH` and `LD_*` can't be listed.
- The program must be a regular executable file, and neither it nor its directory may be writable by group or
  others or owned by another user than you or root. genai records which file it found at start and refuses to
  run it if it changed ("restart genai").

The model's output from a command is redacted for your endpoint key and common secret patterns, cut like
sandbox output, and escaped wherever it is shown. `plugins/slurm/` has `sinfo`, `squeue`, `scontrol show` and
`sacct` commands, all read-only. Over JSON, see the `plugins`, `plugin` and `run` messages below.

## Sandbox

The core has no sandbox by design; that is left to whatever wraps it. `sandbox/run` runs genai or its
tests in a rootless podman container made from this machine's own OS. There is no image to build or
pull: the container's root is an empty tree with your `/usr` and `/etc` mounted read-only, so it uses
the Python and editor you already have. Your home directory is not in it. The container is read-only
and has no capabilities. Only `/work` is writable. It uses its own genai config, never your real
`~/.config/genai`.

```bash
sandbox/run tests          # offline suite, --network=none
sandbox/run selfcheck      # the live test's harness against a fake endpoint, --network=none
sandbox/run live ~/.config/genai/endpoints/work-llm.json   # live test; needs the endpoint's key variable
sandbox/run genai -e NAME  # interactive, with ./sandbox-work mounted at /work
```

`sandbox/run` documents a small contract at its top: what is mounted where, which variables pass
in, and `GENAI_SANDBOX=<name>`. Your own sandbox can replace podman by following it. genai shows
where commands run: the REPL banner, the `:run` confirmation and result line, the `hello` event, and
the audit log all say **inside the <name> sandbox** or **OUTSIDE any sandbox**. That is an
indicator, not a guarantee. It comes from `GENAI_SANDBOX` or podman's or docker's marker file.

`sandbox/run genai` keeps its own config in `~/.local/share/genai-sandbox/config`; put and bless
endpoints there, and set `GENAI_KEY_ENV` to the key variable to pass in.

| Variable | Meaning |
| --- | --- |
| `GENAI_SANDBOX_WORK` | Host directory mounted at `/work` (default `./sandbox-work`). |
| `GENAI_SANDBOX_PYTHON` | The Python to run inside (default `python3`). It must be under `/usr` or a directory you add. |
| `GENAI_SANDBOX_RO` | More host directories to mount read-only at the same path, colon-separated, for a Python or editor outside `/usr`. Your home directory is refused. |
| `GENAI_SANDBOX_HOOKS` | Directory of sandbox hooks (default `~/.local/share/genai-sandbox/hooks`). See Hooks below. |

Anything your user can read under `/usr` and `/etc` can be read inside the sandbox, and
`sandbox/run genai` has network access, since genai has to reach the endpoint.

## Hooks

A hook is a small program of yours that runs at a fixed point. There are two kinds, and neither
can approve anything, add a tool, or make the sandbox less strict. [HOOKS.md](HOOKS.md) has the
full rules and the reasons.

**Sandbox hooks** run before `sandbox/run` starts the container. Each one prints requests: mount a
host directory read-only (`ro DIR`), pass a search path (`path NAME`), or pass a variable
(`env NAME`). `sandbox/run` checks every request and prints what each hook added. The bundled
`modules.sh` makes software from environment modules (Lmod, EasyBuild) work inside:

```bash
mkdir -p ~/.local/share/genai-sandbox/hooks
ln -s /path/to/genai/sandbox/hooks/modules.sh ~/.local/share/genai-sandbox/hooks/
module load gcc/13 openmpi
sandbox/run genai -e NAME
```

**The notify hook** tells you when genai is waiting for you: proposals to review, a proposed
command, or a finished reply. It is the file `~/.config/genai/hooks/notify`, and it runs only
after you bless it. It gets the event's name and a count, never file contents, commands or the
key.

```bash
mkdir -p ~/.config/genai/hooks
cp /path/to/genai/hooks/notify-desktop ~/.config/genai/hooks/notify
genai --bless ~/.config/genai/hooks/notify
```

Anything else, such as another front end or automatic decisions, belongs in a wrapper over
`--json`.

When a saved session's destination changes (the server's address or port, or the endpoint's
proxy, CA bundle or headers), or an older session has no recorded destination, genai and
genai-tui require `--allow-session-endpoint-change` before sending the saved conversation. Check
the URL shown in the error before approving the transfer. Another model on the same server needs
no consent.

**Models.** `"model": "auto"` in an endpoint uses the first model the server lists (`GET /models`;
vLLM and most local servers list the one they serve) and takes its context length from the list
when the server gives one (vLLM's `max_model_len`). To switch while you work: `:models` lists them
and `:model NAME` switches in the REPL; in genai-tui, `/model` (or a click on the model in the
info panel) opens a drop-down, and `/model NAME` switches directly. Only a model the server lists
can be chosen, the conversation carries on with it, and the audit log records the switch. It
lasts for this run: the endpoint file isn't changed, and a resumed session starts with the file's
model again.

## Another host from genai-tui

In genai-tui's shell pane, connect with `genai-ssh` instead of `ssh`:

```
genai-ssh -p 2222 me@login1.cluster      # ssh options as usual; no remote command
```

It is your own ssh (config, keys, agent) starting bash on that host with genai-tui's prompt hook,
so genai-tui follows each command there as it does here: `/run` sends a proposed command to that
host (the question says so, in red), `/share N` and the info panel show which host a command ran
on, and nothing is installed there. The session has a key of its own that never appears on a
command line or on screen; see SECURITY.md, section 7. Plain `ssh` still works, but genai-tui
can't follow it, and `/run` refuses while it runs.

### Remote mode

```
genai-tui --remote me@devbox --dir /srv/project                    # genai runs on devbox, in that directory
genai-tui --remote me@devbox --dir /srv/project --as svc           # ... as another user, through sudo -n
genai-tui --remote me@devbox -e work -p coder -- -J me@bastion     # a bastion; more ssh options after --
```

The screen, your endpoint's API key and your blessed configuration stay on your machine. genai runs on the
host, in PATH, as you or as `--as USER` (which needs `sudo -n -u USER` to work without a password there; the
launcher checks). The shell pane is a followed session on the same host, as the same user, in the same directory,
and says `disconnected from HOST` and offers to reconnect if it drops. Prefer ProxyJump (`-J`, or `ProxyJump` in
`~/.ssh/config`) to agent forwarding for a bastion: the connection never offers your agent to the host, and genai-tui
passes `-a` to ssh so a config file can't either.

What happens, before tmux starts (so a password or second factor is asked for in your own terminal):

1. `genai --remote-config` resolves `-e` and `-p` exactly as a normal start would (default endpoint and profile,
   blessing checks, a changed file is refused) and prints the endpoint and profile *without* `key_env`, `key_file`,
   `proxy`, `headers` or `ca_bundle`. `-S` and `-k` are not available with `--remote` yet.
2. An ssh master connection (`ssh -M -S STATE/cm -fN`, then the host's checks: Python 3.10 or newer as the target user,
   `sudo -n` for `--as`, the directory) whose control socket lives in genai-tui's private 0700 state directory.
3. `tui/keyproxy.py` starts here on a Unix socket, with a random token on its stdin.
4. `ssh -O forward -R 127.0.0.1:0:STATE/kp.sock` opens a loopback port on the host that leads to that proxy.
5. The conversation pane runs genai there over the same connection: a short Python program reads genai's own source
   from stdin (it is too big for a command line), runs it, and genai then reads a config line (the endpoint, whose
   `base_url` is that loopback port, the profile and the token) and the usual protocol. Nothing is installed or
   left on the host, and it is always the same genai version as yours.

The proxy accepts only `GET /v1/models` and `POST /v1/chat/completions` with the token, adds the key and the
endpoint's headers, and sends the request on through the endpoint's own TLS and proxy settings. On exit the proxy,
the forward and the ssh connection are closed; detaching tmux keeps the launcher running to hold them, so Ctrl-C there
ends the session.

You can see where it runs: the banner and the info panel say `genai runs ON host AS user in dir` (red as root), the
status line names the host, and the banner gives the audit log's path on the host (`hello`'s `audit`). The sessions,
audit log and `~/.config/genai` that genai uses are those of the user it runs as on the host; its endpoint is not
blessed there, because the blessing happened here. Test it against a SLES container with `tests/remote_sshd.sh test`.

Limits: the host's login shell must be POSIX-like, its sshd must allow TCP forwarding (`AllowTcpForwarding`), and
`python3`, `python3.13` ... `python3.10` is searched in that order for 3.10 or newer (SLES: `zypper install python311`).
Another user of that host can reach the forwarded port, which is why the token matters (SECURITY.md).

### File tree

`/files` shows or hides a file tree of the working directory on the left (`genai-tui --files` starts
with it shown). Move with the arrow keys; Right or Enter opens a directory and Left closes it; `.`
shows or hides hidden files and `r` refreshes. On a file, Enter views it in the pager, `a` attaches it
to your next message and `i` puts its path in the input box. A `⚠` marks a file genai thinks holds
secrets, `⚿` one the model would have to ask about first, and `↪` a symlink, which is not followed.

Browsing sends nothing to the model. Listings and files come through genai, which applies its usual
checks and masks secrets it recognises. `a` opens the file in your editor first, like shell context:
what you save goes with your next message, and nothing does if you save an empty file.

**Viewing.** A file, a whole proposal (`v`) and the raw view open in genai-tui's own pager, in a popup: Esc or `q`
closes it, the arrows, PgUp/PgDn and the wheel scroll, `/` searches and `n`/`N` move between matches. To use
another, set `GENAI_TUI_PAGER` (for example `GENAI_TUI_PAGER=less`).

### Features

genai-tui's `/model`, `/copy`, `/decline`, `/raw`, `/system` and `/files`, and the `genai-ssh` handshake, are
features, each in its own module under `tui/features/`. To switch some off, list them in
`~/.config/genai/tui.json` (`$XDG_CONFIG_HOME/genai/tui.json`):

```json
{"disable": ["copy", "model"]}
```

or set `GENAI_TUI_DISABLE=copy,model` for one run. Names are `model`, `copy`, `decline`, `raw`,
`ssh` and `files`. A disabled feature has no command, help line or menu item, and genai-tui's socket
refuses its requests. Without `ssh`, a genai-ssh session isn't followed. No file means
everything is on; a file that can't be read is ignored, with a note in the conversation.

## Sandboxed runs

In coding mode the model can only propose commands, so you run every build and test yourself. A
profile can instead give it a sandbox, where it runs them itself and fixes what fails:

```json
{
  "name": "coder-py",
  "endpoint": "work-llm",
  "coding": true,
  "sandbox": {"image": "localhost/myproject-dev:latest"}
}
```

The model then has a `run_in_sandbox` tool. Each command runs in a rootless podman container:

- **On a throwaway copy of the project.** The working directory is mounted at `/work` as an
  overlay: the command can write anything, and every change is discarded when it ends. The only
  way the real project changes is an edit or write you approve. So a test run can't plant a git
  hook, a `Makefile` or a `conftest.py` that later runs on your machine.
- **Without your secrets.** Files that look like secrets are hidden: `.env*`, `.netrc`, `.npmrc`,
  `.pypirc`, `.git-credentials`, private keys and keystores, directories such as `.aws` and `.ssh`,
  a `.git/config` holding a credential, and any file whose contents look like a secret (including
  the endpoint's key). Before the command runs, genai checks inside the container that each
  of them really is empty, and refuses to run it if one isn't. Virtual environments (directories
  with a `pyvenv.cfg`) and `node_modules` aren't scanned. Your home directory, `/tmp` and `/run`
  are not in the sandbox at all; its `HOME` is its own `/tmp`.
- **Offline.** `--network=none`, and no proxy variables. Nothing can be downloaded, so the image
  must already have the project's tools and dependencies, or the project a virtual environment.
- **Limited.** A time limit (`timeout`, default 300 s), a 1 GB `/tmp`, a read-only root, no
  capabilities and `no-new-privileges`. It also gets memory (`memory`, default `"4g"`) and process
  (`pids`, default 1024) limits where the host delegates cgroups to you. Where it doesn't, such as
  a login without a systemd user session, genai says so once and runs without them.

An offline run needs no approval: you see the command, then its exit code and output, and so does
the model. Ctrl-C stops it and ends the turn. Every run is in the audit log, by fingerprint.

**The network switch.** `:network on` (or the JSON `network` message) gives the sandbox the
network for this session; `:network off` takes it away, and a new session starts as the profile
says (`"network": true` starts it on, which blessing flags). While it is on, **every sandboxed
command asks first**, because code that can read the project and reach the network could send the
project anywhere.

**Not in your home directory.** The sandbox never runs where coding mode is refused (your home
directory, a directory above it, `~/.ssh` and other credential directories, genai's config
directory), even with `--coding-anywhere`: the copy would hold everything there, and offline runs
don't ask. `run_in_sandbox` is then not offered, and the banner says why. Start genai in the
project's own directory.

The image is used as it is (`--pull=never`); pull or build it yourself. Podman is the only engine
so far (`"engine": "podman"` or `"auto"`).

## JSON protocol

`genai --json` speaks JSON Lines over stdin and stdout: one object per line, each with a
`type`. A wrapper is the user interface. It shows proposals and sends back decisions, and genai
still enforces its own rules: blessing, coding-mode refusals, masking. Text from the model
arrives as it is, so the wrapper must escape it before it displays it.

genai's first line is `hello`. It has `protocol` (1), `version`, `features`, `endpoint`,
`profile`, `session`, `resumed`, `coding`, `coding_from`, `coding_override`, `cwd`, `sandbox`,
`runner` (the profile's sandbox for `run_in_sandbox`: `engine`, `image`, `network`, `timeout` and
`problem`, or `null`), the active `skills` and `plugins`, and the model's `tools`. Check `features` before you use a message added
later.

`hello` also says where genai runs: `host` (its host name), `user` (the effective user's name, or the uid
if it has none), `uid`, `remote` (true with `--config-stdin`) and `audit` (the path of its audit log). They are
always present. As root, the REPL
banner adds a `[!]` line saying every command it proposes would run as root.

`--remote-config [-e NAME] [-p NAME]` prints, as one JSON line and then exits, what genai-tui's remote mode sends:
`{"endpoint": {...}, "profile": {...} or null, "endpoint_name": "...", "base_path": "/v1"}`. The endpoint and profile
are resolved and checked as for a normal start (default endpoint and profile, blessing, files changed since
blessing are refused with the usual error and status 3), and the endpoint has no `key_env`, `key_file`, `proxy`,
`headers` or `ca_bundle`: no key, and nothing that could carry a secret. The key proxy on your machine uses the
endpoint's own proxy and headers.

`--config-stdin` (with `--json` only; not with `-e`, `-p`, `-S` or `-k`; plugins aren't sent
there yet) is for genai-tui's remote mode, where genai
runs on another host over ssh. genai first reads one line from stdin, before `hello`:

```
{"type": "config", "endpoint": {...}, "profile": {...} or null, "token": "<32 to 128 hex characters>"}
```

The endpoint and profile are the fields of an endpoint file and a profile file, checked the same way. Here they
come from the wrapper, which checked the user's blessed files on its side, so genai blesses nothing on this host
for them; the audit log records a `remote_config` event with their hashes. The endpoint may not name a key
(`key_env`, `key_file`) or a `proxy`, and its `base_url` must be `http://127.0.0.1:PORT`, with an optional path:
the key stays on the user's machine, and the port is a tunnel to a proxy there. genai sends the token in an
`X-Genai-Token` header and never shows it. The profile's `endpoint` is ignored, and its skills are not available
yet (a `notice` after `hello` says so). A bad line ends genai with exit status 3 and an `error` with code
`bad_config`, and nothing is sent to any endpoint.

**Messages a wrapper sends**

| `type` | Fields | What happens |
|---|---|---|
| `user` | `text` | Sends a message to the model, with any queued attachments. |
| `attach` | `text`, optional `label`, `ref` | Queues text for the next `user` message. Secrets in it are masked. Answered by `attached`. |
| `decision` | `id`, `decision` (`accept` or `decline`); for a plan, optional `approve` and `edits` | Decides a read, list, write, edit or plan proposal. For a plan, `approve` lists the step numbers approved (default: all), and `edits` maps a command step's number to the command the user wants instead. A malformed choice declines the plan, with the error `bad_message`. |
| `result` | `id`, `status` (`ok`, `declined`, `error`), `output` | Answers a proposal for a tool the wrapper registered. |
| `register_tools` | `tools`: a list of `{name, description, parameters}` | Adds tools the model can call. The wrapper performs them. Answered by `registered`. |
| `compact` | | Shrinks the conversation. Answered by `compacted`. |
| `models` | | Answered by `models`: `current`, and `models`, each `{"id", "context"}` (the server's list, `GET /models`, without names genai won't use). |
| `model` | `model` (a name from `models`) | Uses that model from the next message, on the same server. Answered by `model` (`endpoint`, `context`), or an `unknown_model` error. |
| `clear` | | Starts the conversation over: the model gets none of it from now on, and an approved plan is dropped. The saved session's history keeps it. Answered by `cleared` (`dropped`: how many messages, `context`). |
| `coding` | `on` (true or false) | Turns coding mode on or off. The same places are refused as for `--coding` (error `coding_refused`), and `--coding-anywhere` stays a command-line flag. Always answered by a `coding` event with the state now. |
| `skills` | | Answered by `skills`: every skill, its blessing `status`, and whether it is `active`. |
| `skill` | `name`, `on` (true or false) | Uses a skill from the next request on, or stops using it. Only a blessed skill can be turned on (errors `unknown_skill`, `not_blessed`). Always answered by a fresh `skills` list. |
| `plugins` | | Answered by `plugins`: every plugin found, each `{name, description, enabled, blessed, status, pane, dir, commands}`; a command has `name`, `describe`, `params` (each `{pattern or enum or int, describe}`), `read_only` and `argv` (with `{placeholders}`). genai also sends it once after `hello`. |
| `plugin` | `name`, `on` (true or false) | Turns a plugin's trusted commands on or off. Only a blessed plugin whose programs pass the checks can be turned on (errors `unknown_plugin`, `not_blessed`, `bad_plugin`; `remote` with `--config-stdin`). Always answered by a fresh `plugins` event. |
| `run` | `id` (a string or number), `plugin`, `command`, `params` | Runs one command of an enabled plugin for the user, with the same checks, limits, cache and rate limits as the model's calls (source `user` in the audit log). Answered by `ran`: `id`, then `exit` (`null` if killed), `output`, `note`, `cached`, `ms`, `redacted`, or `id` and `error`. Between turns only; while proposals wait it gets `busy`. |
| `files` | optional `path` (default `.`) | Lists one directory under the working directory, for a file tree. The response includes `truncated` when the scan reaches its limit. Each entry has `name`, `kind` (`file`, `dir`, `symlink`, `other`), `size` and `hidden`. A file also has `secret` (it looks like it holds secrets; `null` if genai would not read it at all, such as a binary or a file over the read limit) and `asks` (reading it would need your OK, even in coding mode). Contents are read only to classify them and never reach the model. Symlinks are listed, not followed; a bad path gets the error `bad_path`. |
| `read` | `path` | Shows one file to the user, for a file viewer. It is read through the same checks as the model's reads (no symlinks, regular files only, the read limit, no binary), answered by `file` with `path`, `text`, `size` (bytes), `secret` (it looks like it holds secrets) and `redacted` (counts, as for `attached`: the endpoint key and common secret patterns are masked in `text`). The text goes to the wrapper only, never to the model or the session, and each read is written to the audit log (no content). A file that cannot be read gets the error `bad_path` with the `path`. |
| `trace` | optional `from`, `to` (exchange numbers) | Answered by `trace`: the exchanges with the endpoint in that range (default: the latest), exactly as sent and received. Each has `n`, `time`, `method`, `url`, `headers` (the API key and the endpoint's own header values are not shown), `request` (the body, as sent), `http`, `stream`, `response` (the reply as it arrived: each streamed line, or the whole body), `status` (`ok` or `error`, with `error`), `message` (the reply as genai put it together), `usage` and `ms`. genai keeps the last 30, in memory only; `kept` gives the range it still has. |
| `system` | | Answered by `system`: the system prompt and the tool definitions the next request will carry, with `model` and `params`. |
| `plan` | optional `clear: true` | Answered by `plan`: the approved plan, or `null`. With `clear`, drops it first, so everything asks again. |
| `network` | optional `on` (true or false) | Turns the sandbox's network on or off for this session (error `no_sandbox` without one). Answered by `network`: `runner` as in `hello`, and `available` (whether `run_in_sandbox` is offered now). |

While proposals are waiting for decisions, only `decision`, `result`, `attach`, `trace` and `system` are accepted.
Anything else gets the error `busy`, so coding mode and skills never change halfway through a
decision.

**Events genai sends**

| `type` | Meaning |
|---|---|
| `text` | A piece of the model's reply. |
| `proposal` | `id`, `kind` (`read`, `list`, `write`, `edit`, `command`, `sandbox`, `trusted`, `external`, `invalid`), and the proposal's fields, such as `path`, `content`, `diff`, `command` and `reason`. `auto: true` means it needed no approval: coding mode, or a read the approved plan names. A `plan` proposal has `goal` and numbered `steps` (`n`, `kind`: `read`, `edit`, `write` or `command`, a `path` or the exact `command`, and `why`). While a plan is approved, a proposal it covers has `plan_step`; a command matching an approved step character for character also has `preapproved: true`, so the wrapper may run it without asking again; a command, edit or write the plan doesn't cover has `off_plan: true`. Edits and writes always ask. `secret: true` means the file looks like it holds secrets. A command is only shown: the wrapper runs it, if the user wants to, and can share the output with `attach`. A `sandbox` proposal (`command`, `reason`, `network`, `image`) is genai running the command in the sandbox: offline it has `auto: true` and runs at once; online it waits for a `decision`. A `trusted` proposal (`plugin`, `command`, `params`, `argv`, `read_only`) is a plugin's trusted command: `read_only` ones have `auto: true` and run at once; others wait for a `decision`. |
| `result` | `id`, `status`, `detail`: what happened to a proposal that was performed. For a `sandbox` or `trusted` run, also `exit` (`null` if it was killed at the time limit or never ran) and `output`, exactly what the model is sent. |
| `done` | The turn ended. `reason` is `reply`, `command`, `stopped` (Ctrl-C during a sandboxed run) or `step_limit`; `context` gives `used` and `limit`; `exchanges` gives the first and last exchange numbers of this turn, for `trace` (also on an `error` that ends a turn). |
| `context` | The conversation is getting near the context limit. |
| `notice` | Something the user should know. |
| `error` | `code` and `message`. |
| `plan` | The approved plan changed: approved, a step done, or dropped. `plan` has `id`, `goal` and `steps`, each with `approved`, `status` (`todo`, `done`, `left out`) and `edited` when the user changed its command; `null` when there is none. A plan is never saved with the session. |
| `attached`, `registered`, `compacted`, `cleared`, `models`, `model`, `coding`, `skills`, `files`, `file`, `trace`, `system`, `network`, `plugins`, `ran` | Answers to the messages above. |

When stdin closes, every waiting proposal is declined and genai exits.

## Live test

The live test takes any endpoint file that uses `key_env`. It checks the endpoint and model list,
has the model write a bash script to stdout,
redirected to a file with `>`, then checks it with `bash -n` without running it. It also runs the
JSON proposal/decision round trip, and checks that a persona profile picks its endpoint and that
its persona text reaches the model.

## License

MIT. See [LICENSE](LICENSE).
