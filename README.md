# genai

A small command-line client for OpenAI-compatible APIs, standard library only, Python 3.10+.
**The model never executes anything on your system:** it proposes a command or a file operation, and
you decide. The one exception is opt-in: in coding mode, a profile can give it a sandbox, an offline
container working on a throwaway copy of the project, to build and test in
([Sandboxed runs](#sandboxed-runs)).
See [DECISIONS.md](DECISIONS.md) for the design choices and known limitations, and
[SECURITY.md](SECURITY.md) for the risks of a tool like this and how genai handles each.

## Quick start

From a clone of this repository:

```bash
mkdir -p ~/.local/bin                       # any directory on your PATH will do
ln -s "$PWD/genai" "$PWD/tui/genai-tui" ~/.local/bin/
genai --list-endpoints                      # the first run creates ~/.config/genai (none yet)
$EDITOR ~/.config/genai/endpoints/work-llm.json         # write your endpoint: see below
genai --bless ~/.config/genai/endpoints/work-llm.json   # shows URL, model, key source; asks you to confirm
export GENAI_API_KEY=...                    # the endpoint names the variable, never the key
genai -e work-llm "what does 'set -euo pipefail' do?"
```

The endpoint file says where your prompts go. Fill in your server's OpenAI-compatible API address
(usually ending in `/v1`), its model name, the environment variable that will hold your API key (never
the key itself) and the model's context size in tokens:

```json
{
  "name": "work-llm",
  "base_url": "YOUR-SERVER-ADDRESS/v1",
  "model": "YOUR-MODEL-NAME",
  "key_env": "GENAI_API_KEY",
  "context_limit": 128000
}
```

To use the example persona, which grants coding mode and uses a bundled skill, add
`"skill_dirs": ["/path/to/genai/skills"]` to `~/.config/genai/config.json` ([Configuration](#configuration)),
then bless both and start it in a project directory (coding mode is refused in your home directory):

```bash
genai --bless profiles/coder.json           # a persona: shows its text, endpoint, skills, coding grant
genai --bless skills/bash-scripts           # the skill it uses
cd ~/src/some-project
genai -e work-llm -p coder                  # REPL: a line with only '..' sends; :help for commands
genai-tui -e work-llm -p coder              # the same in tmux panes, beside your shell (needs tmux)
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
- `"edits": "approved-files"` (default `"ask"`): once you approve an edit or write to a file, further
  edits and writes to that same file apply without asking, for the rest of the session. They are
  still shown with their diff, logged, and bound to the file as it was. New files, hidden files,
  anything that looks like a secret, and a whole replacement of a file that can't be diffed always
  ask; declining an edit to a file takes it out of the set. `:edits` (or `/edits` in genai-tui)
  shows the files, `:edits reset` forgets them, and `:edits ask|approved-files` switches for this
  session. The set is never saved, so a resumed session starts empty. Blessing flags the field.
- Endpoints, profiles and skills are each blessed with `genai --bless PATH`. A profile can only use an
  endpoint and skills that are blessed on their own. If any file changes, genai refuses it until you
  bless it again.

| Use | Command |
| --- | --- |
| Attach files | `genai -p NAME -f app.log -f app.conf "why is it crashing?"` |
| Skills | `genai -p NAME -k slurm` (skills must be blessed) |
| Plugins (trusted commands) | `genai -p NAME --plugin slurm` (plugins must be blessed; [Plugins and trusted commands](#plugins-and-trusted-commands)) |
| Sessions | `genai -p NAME -S nightly "summarize" > out.txt` |
| Coding mode | `genai -e NAME --coding` (reads stop asking, except files that look like secrets; writes still ask; the model can also search the files) |
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

### Plugins in genai-tui

`/plugins` lists the plugins genai found; `/plugins NAME on|off` switches one, and `/help` and the info panel
list the enabled ones' commands. A plugin's `pane` is a program in its directory (blessed with the rest of it)
that genai-tui starts with `/plugins NAME pane`, in a tmux pane of its own; the same command hides and shows it
again, like `/files`. A `.py` pane runs with genai-tui's Python, with `tui/` on `PYTHONPATH` so it can use
`ipc.Client`; any other is run as a program.

A pane never runs inside the conversation pane, and it never reaches genai. It connects to genai-tui's private
socket in the role `plugin:NAME`, and may only send `run` (one of its own plugin's **read-only** commands, by
`command` and `params`, with its own `id`; genai runs it with the usual checks, cache and rate limits, and the
`ran` answer comes back to that pane alone), `insert` (text for the input box) and `attach` (`text` and a
`label`, which open in your editor first, like shell context). A plugin that is off gets `{"type": "refused"}`.
Panes don't run in remote mode yet.

**The Slurm console** (`plugins/slurm/slurm.py`, `/plugins slurm pane`) shows nodes by state (down and drained
ones in red, with their reasons; the healthy ones only as counts), partitions, how many jobs are in each state,
and your own jobs. ↑↓ choose a node or job, Enter or a click shows its details (`scontrol show`), `i` puts its
name in the input box, `a` attaches what is shown (after you review it), `/` filters (and shows the healthy nodes
that match), `r` refreshes (genai's cache and `min_interval` still apply) and Esc goes back. Adding the pane
changed the plugin, so bless `plugins/slurm` again.

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
sandbox/run shell [--net]  # a bash in the same sandbox: no network unless --net, no key variable
```

`sandbox/run` documents a small contract at its top: what is mounted where, which variables pass
in, and `GENAI_SANDBOX=<name>`. Your own sandbox can replace podman by following it. genai shows
where commands run: the REPL banner, the `:run` confirmation and result line, the `hello` event, and
the audit log all say **inside the <name> sandbox** or **OUTSIDE any sandbox**. That is an
indicator, not a guarantee. It comes from `GENAI_SANDBOX` or podman's or docker's marker file.

Inside `sandbox/run genai` the REPL marks itself so you can tell: the prompt is `(sandbox) > `, the
banner's second line is in bold (`IN THE PODMAN SANDBOX: /work = <host path>, no host home`), and
`:sandbox` says where genai and your commands run, the working directory, the config directory and
whether the network looks available. Outside a sandbox the prompt is the plain `> ` and the banner says
**OUTSIDE any sandbox**. `sandbox/run shell` opens a bash in the same sandbox, with the prompt
`[sandbox:podman /work] DIR$` in reverse video and a banner saying what is mounted. It has no network
unless you pass `--net`, and the key variable is never passed in.

`sandbox/run genai` keeps its own config in `~/.local/share/genai-sandbox/config`; put and bless
endpoints there, and set `GENAI_KEY_ENV` to the key variable to pass in.

| Variable | Meaning |
| --- | --- |
| `GENAI_SANDBOX_WORK` | Host directory mounted at `/work` (default `./sandbox-work`). |
| `GENAI_SANDBOX_PYTHON` | The Python to run inside (default `python3`). It must be under `/usr` or a directory you add. |
| `GENAI_SANDBOX_RO` | More host directories to mount read-only at the same path, colon-separated, for a Python or editor outside `/usr`. Your home directory is refused. |
| `GENAI_SANDBOX_HOOKS` | Directory of sandbox hooks (default `~/.local/share/genai-sandbox/hooks`). See Hooks below. |

Anything your user can read under `/usr` and `/etc` can be read inside the sandbox, and
`sandbox/run genai` has network access, since genai has to reach the endpoint. `sandbox/run shell`
does not, unless you pass `--net`.

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
   `proxy`, `headers` or `ca_bundle`. The skills and plugins in use (the profile's, or `default_skills` and
   `default_plugins`, plus `-k`) go with their files, if they are blessed here; one that isn't is left out, with a
   message. On the host they are checked against the hashes blessed here, the plugins' programs are looked for there
   (a command whose program is missing there is left out, as locally), and nothing blessed only on the host can be
   used. So the Slurm plugin's commands run on the cluster. `-S` is not available with `--remote` yet.
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
blessed there, because the blessing happened here. Test it against a SLES container with `tests/remote_sshd.sh test`, or against a real host of yours with
`GENAI_TEST_REMOTE_HOST=me@devbox python3 -m unittest tests.test_remote.TestRealHost` (a turn, then a sandboxed run and a
stopped one if the host has rootless podman and `GENAI_TEST_REMOTE_IMAGE`, default `docker.io/library/alpine:3`, pulled; it works in a
`mktemp -d` directory there and removes it).

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

genai-tui's `/model`, `/copy`, `/decline`, `/raw`, `/system`, `/files`, `/sandbox`, `/edits` and `/plugins`, and the `genai-ssh` handshake, are
features, each in its own module under `tui/features/`. To switch some off, list them in
`~/.config/genai/tui.json` (`$XDG_CONFIG_HOME/genai/tui.json`):

```json
{"disable": ["copy", "model"]}
```

or set `GENAI_TUI_DISABLE=copy,model` for one run. Names are `model`, `copy`, `decline`, `raw`,
`ssh`, `files`, `sandbox`, `edits` and `plugins` (without it, no plugin pane can connect). A disabled feature has no command, help line or menu item, and genai-tui's socket
refuses its requests. Without `ssh`, a genai-ssh session isn't followed. No file means
everything is on; a file that can't be read is ignored, with a note in the conversation.

## Searching files

In coding mode the model also has `search_files`: a literal string (not a regular expression), optionally
under a directory and only in files whose name matches a `glob` such as `*.py`, case-insensitive if it asks.
It runs without asking, like a read, and gets back each matching line as `path:line: text`. It looks only where
an automatic read would: hidden files and directories and symlinks are skipped, and so are binary files, files
over the read limit and files that look like they hold secrets (it is told how many). It stops after 2000 files,
200 matches or 5 seconds, and each line is cut to 200 characters around the match.

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

**Seeing it in genai-tui.** The info panel has a SANDBOX section: whether genai itself runs in a
sandbox (`in the podman sandbox`, green, or `not sandboxed`, the usual way to run it, from `hello`'s `sandbox`), and
the model's sandbox: `none`, `ready · offline` or `ONLINE` with its image, `can't run: …`, or
`RUNNING p3 · 12s` while a run is going. tmux's status line says `⊡ sandbox` or `not sandboxed`,
and `⏵ sandbox p3 12s` during a run. A running card counts its seconds. Ctrl-C on an
empty input, or `/stop`, stops the run: genai-tui sends genai the `stop` message, which works in
remote mode too (an older genai without it gets a SIGINT here, and in remote mode its run ends at
its time limit). `/sandbox` prints all of it, with the image, network and limits.

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

## Work sessions

A work session lets the model work through a task on its own, editing, building and testing without
asking, on a copy of your project. Nothing reaches your own files: at the end you review one branch
and merge what you want, with git.

```
:work make the parser accept quoted fields, with tests
```

1. **genai makes the copy.** It needs a git repository with no uncommitted changes to tracked
   files (commit or stash first). It adds a git worktree from your last commit, on a new branch
   `genai/work-N`, under `~/.local/state/genai/work/`. Untracked files aren't in the copy.
2. **The model proposes a plan:** items in order, each small enough to finish and commit on its
   own. You approve it once. A new plan later asks again.
3. **It works through the items without asking.** Reads, searches, edits and writes in the copy
   apply at once (you see each one), except to hidden or secret-looking files, which still ask.
   Offline sandbox runs work on a throwaway overlay of the copy, as usual: they see the model's
   edits, and their own changes are discarded.
4. **It finishes each item with `finish_item`.** genai runs the profile's test command in the
   sandbox, if there is one, and commits the changes on the branch. An item whose test fails, or
   that changed nothing, isn't finished, and the model is told why. The commit's title is the
   model's short summary; its body names the plan item and holds the model's notes for later items.
5. **At the end** genai commits anything left, removes the copy and shows the branch: its commits,
   the diff stat, and the commands to review it, merge it or delete it. genai never touches your
   own branch. A session that changed nothing deletes its branch.

**Contexts.** Each plan item runs in a fresh context: the model sees the task, the plan and the
notes earlier items left, not their conversations, so a long session doesn't fill up. The context
that made the plan waits meanwhile. Within an item, old file contents and command output are
dropped once the context is 40% full (the model reads them again if it needs to), and an item
that still fills it goes on in a fresh context, with notes the model writes first.

**Limits.** Each plan item gets a budget of model steps (default 20). Finishing an item starts the
next with a fresh budget. An item that uses up its budget goes back to the planning context, with
what it changed and its last words; the planner finishes it, starts a fresh attempt with a hint, or
proposes a new plan. If the planner uses up its steps too, or the item gets stuck again, the session
pauses. There is also a time
limit, 2 hours without you by default. A message from you, such as "go on", gives a fresh budget
and time. `:work end` (or exiting genai) ends the session; `:work` shows how far it has got. Ctrl-C
stops a sandboxed run, as usual.

The profile sets the defaults, and `:work --steps N --minutes M TASK` overrides them for one session:

```json
{
  "name": "coder-py",
  "endpoint": "work-llm",
  "coding": true,
  "sandbox": {"image": "localhost/myproject-dev:latest"},
  "work": {"test": "python3 -m unittest -q", "steps_per_item": 20, "time_limit": 7200}
}
```

**A model for planning.** `planner` in `work` sends the planning contexts (making the plan, and
reviewing a stuck item) to another model, such as a stronger one, while the items run on the
profile's own:

- `"planner": {"model": "big-model"}`: another model on the same endpoint. genai checks the server
  lists it before the session starts, and uses the context length the server gives for it.
- `"planner": {"endpoint": "other", "model": "big-model"}`: another blessed endpoint (without
  `model`, its own default). It reads files, so it sees the project too. In remote mode only the
  profile's endpoint can be reached, so it plans as well, with a notice.

**Git stays on your side.** genai runs every git command itself, on your machine, with hooks,
fsmonitor and signing off, and it names the copy's git directory explicitly instead of trusting
the copy's `.git` file. No file tool may write anything under `.git`, and the sandbox only ever
gets an overlay of the copy. So nothing the model or a test writes can make git run a program on
your machine. Commits use your git identity, or `genai` if you have none. Running a work session
needs the REPL or a wrapper (the JSON `work` message); genai-tui support comes later.

## JSON protocol

`genai --json` speaks JSON Lines over stdin and stdout: one object per line, each with a
`type`. A wrapper is the user interface. It shows proposals and sends back decisions, and genai
still enforces its own rules: blessing, coding-mode refusals, masking. Text from the model
arrives as it is, so the wrapper must escape it before it displays it.

genai's first line is `hello`. It has `protocol` (1), `version`, `features`, `endpoint`,
`profile`, `session`, `resumed`, `coding`, `coding_from`, `coding_override`, `cwd`, `sandbox`,
`runner` (the profile's sandbox for `run_in_sandbox`: `engine`, `image`, `network`, `timeout`,
`memory`, `pids` and `problem`, or `null`), the active `skills` and `plugins`, and the model's `tools`. Check `features` before you use a message added
later. With the feature `interrupt`, a SIGINT to genai `--json` stops a sandboxed run as Ctrl-C does in
the REPL (its result's `status` is `stopped`, and the turn's `done` reason is `stopped`); at any
other time it is ignored. The feature `stop` is the same as a message, for when genai is out of reach
of a signal, as over ssh.

`hello` also says where genai runs: `host` (its host name), `user` (the effective user's name, or the uid
if it has none), `uid`, `remote` (true with `--config-stdin`) and `audit` (the path of its audit log). They are
always present. As root, the REPL
banner adds a `[!]` line saying every command it proposes would run as root.

`--remote-config [-e NAME] [-p NAME] [-k NAME] [--plugin NAME]` prints, as one JSON line and then exits, what
genai-tui's remote mode sends: `{"endpoint": {...}, "profile": {...} or null, "endpoint_name": "...", "base_path":
"/v1", "skills": [...], "plugins": [...]}`. Each skill and plugin is `{"name", "hash", "files"}`: its blessed hash
and every file of its directory, base64 (at most 1 MiB in all). Those in use are sent (the profile's skills and
plugins, or `default_skills` and `default_plugins`, plus `-k` and `--plugin`); one that is missing, not blessed,
changed or too big is left out with a message on stderr. The endpoint and profile
are resolved and checked as for a normal start (default endpoint and profile, blessing, files changed since
blessing are refused with the usual error and status 3), and the endpoint has no `key_env`, `key_file`, `proxy`,
`headers` or `ca_bundle`: no key, and nothing that could carry a secret. The key proxy on your machine uses the
endpoint's own proxy and headers.

`--config-stdin` (with `--json` only; not with `-e`, `-p`, `-S` or `-k`) is for genai-tui's remote mode, where genai
runs on another host over ssh. genai first reads one line from stdin, before `hello`:

```
{"type": "config", "endpoint": {...}, "profile": {...} or null, "token": "<32 to 128 hex characters>",
 "skills": [...], "plugins": [...]}
```

The endpoint and profile are the fields of an endpoint file and a profile file, checked the same way. Here they
come from the wrapper, which checked the user's blessed files on its side, so genai blesses nothing on this host
for them; the audit log records a `remote_config` event with their hashes. The endpoint may not name a key
(`key_env`, `key_file`) or a `proxy`, and its `base_url` must be `http://127.0.0.1:PORT`, with an optional path:
the key stays on the user's machine, and the port is a tunnel to a proxy there. genai sends the token in an
`X-Genai-Token` header and never shows it. The profile's `endpoint` is ignored. `skills` and `plugins` (optional)
are as `--remote-config` prints them: genai hashes each one's files again and refuses the line unless the hash is
the one sent, and makes the skill or plugin from those bytes, never from this host's files. They are all on at the
start, and they are the only ones there are: the `skill` and `plugin` messages can switch them off and on again,
and get the error `remote` for any other name. A plugin's programs are looked for on this host, with the usual
checks, and `--plugin` is ignored. A bad line ends genai with exit status 3 and an `error` with code
`bad_config`, and nothing is sent to any endpoint.

**Digests.** Each proposal that waits for an answer carries a `digest`: 16 hex characters over the event exactly
as genai emitted it and a random value that genai picks when it starts. A `decision` or `result` must echo it
unchanged, without computing it. An answer whose digest is missing or different gets a `digest_mismatch` error and
decides nothing: the proposal still waits. This catches mix-ups (an answer meant for another card, a queue race, a
reconnect to a new genai whose IDs start again at `p1`), not a wrapper that means harm.

**Messages a wrapper sends**

| `type` | Fields | What happens |
|---|---|---|
| `user` | `text` | Sends a message to the model, with any queued attachments. |
| `attach` | `text`, optional `label`, `ref` | Queues text for the next `user` message. Secrets in it are masked. Answered by `attached`. |
| `decision` | `id`, `digest`, `decision` (`accept` or `decline`); for a plan, optional `approve` and `edits` | Decides a read, list, write, edit or plan proposal. For a plan, `approve` lists the step numbers approved (default: all), and `edits` maps a command step's number to the command the user wants instead. A malformed choice declines the plan, with the error `bad_message`. |
| `result` | `id`, `digest`, `status` (`ok`, `declined`, `error`), `output` | Answers a proposal for a tool the wrapper registered. |
| `register_tools` | `tools`: a list of `{name, description, parameters}` | Adds tools the model can call. The wrapper performs them. Answered by `registered`. |
| `compact` | | Shrinks the conversation. Answered by `compacted`. |
| `models` | | Answered by `models`: `current`, and `models`, each `{"id", "context"}` (the server's list, `GET /models`, without names genai won't use). |
| `model` | `model` (a name from `models`) | Uses that model from the next message, on the same server. Answered by `model` (`endpoint`, `context`), or an `unknown_model` error. |
| `stop` | | Stops the sandboxed run going on now, as SIGINT does: genai reads it even while busy, the container is killed, the result's `status` is `stopped` and the turn ends `stopped`. When nothing is running (including while the model is still replying), answered by a `not_running` error, which doesn't end the turn. |
| `clear` | | Starts the conversation over: the model gets none of it from now on, and an approved plan is dropped. The saved session's history keeps it. Answered by `cleared` (`dropped`: how many messages, `context`). |
| `coding` | `on` (true or false) | Turns coding mode on or off. The same places are refused as for `--coding` (error `coding_refused`), and `--coding-anywhere` stays a command-line flag. Always answered by a `coding` event with the state now. |
| `skills` | | Answered by `skills`: every skill, its blessing `status`, and whether it is `active`. |
| `skill` | `name`, `on` (true or false) | Uses a skill from the next request on, or stops using it. Only a blessed skill can be turned on (errors `unknown_skill`, `not_blessed`; `remote` with `--config-stdin` for a skill the wrapper didn't send). Always answered by a fresh `skills` list. |
| `plugins` | | Answered by `plugins`: every plugin found, each `{name, description, enabled, blessed, status, pane, dir, commands}`; a command has `name`, `describe`, `params` (each `{pattern or enum or int, describe}`), `read_only` and `argv` (with `{placeholders}`). genai also sends it once after `hello`. |
| `plugin` | `name`, `on` (true or false) | Turns a plugin's trusted commands on or off. Only a blessed plugin whose programs pass the checks can be turned on (errors `unknown_plugin`, `not_blessed`, `bad_plugin`; `remote` with `--config-stdin` for a plugin the wrapper didn't send). Always answered by a fresh `plugins` event. |
| `run` | `id` (a string or number), `plugin`, `command`, `params` | Runs one command of an enabled plugin for the user, with the same checks, limits, cache and rate limits as the model's calls (source `user` in the audit log). Answered by `ran`: `id`, then `exit` (`null` if killed), `output`, `note`, `cached`, `ms`, `redacted`, or `id` and `error`. Between turns only; while proposals wait it gets `busy`. |
| `files` | optional `path` (default `.`) | Lists one directory under the working directory, for a file tree. The response includes `truncated` when the scan reaches its limit. Each entry has `name`, `kind` (`file`, `dir`, `symlink`, `other`), `size` and `hidden`. A file also has `secret` (it looks like it holds secrets; `null` if genai would not read it at all, such as a binary or a file over the read limit) and `asks` (reading it would need your OK, even in coding mode). Contents are read only to classify them and never reach the model. Symlinks are listed, not followed; a bad path gets the error `bad_path`. |
| `read` | `path` | Shows one file to the user, for a file viewer. It is read through the same checks as the model's reads (no symlinks, regular files only, the read limit, no binary), answered by `file` with `path`, `text`, `size` (bytes), `secret` (it looks like it holds secrets) and `redacted` (counts, as for `attached`: the endpoint key and common secret patterns are masked in `text`). The text goes to the wrapper only, never to the model or the session, and each read is written to the audit log (no content). A file that cannot be read gets the error `bad_path` with the `path`. |
| `trace` | optional `from`, `to` (exchange numbers) | Answered by `trace`: the exchanges with the endpoint in that range (default: the latest), exactly as sent and received. Each has `n`, `time`, `method`, `url`, `headers` (the API key and the endpoint's own header values are not shown), `request` (the body, as sent), `http`, `stream`, `response` (the reply as it arrived: each streamed line, or the whole body), `status` (`ok` or `error`, with `error`), `message` (the reply as genai put it together), `usage` and `ms`. genai keeps the last 30, in memory only; `kept` gives the range it still has. |
| `system` | | Answered by `system`: the system prompt and the tool definitions the next request will carry, with `model` and `params`. |
| `plan` | optional `clear: true` | Answered by `plan`: the approved plan, or `null`. With `clear`, drops it first, so everything asks again. |
| `edits` | optional `mode` (`ask` or `approved-files`), optional `reset: true` | Switches edits for this session, or forgets the approved files. Switching to `ask` forgets them too. Always answered by `edits`: `mode` and `files` (the approved set, as relative paths). genai also sends `edits` when the set changes, and `hello` has the same `edits` object. |
| `network` | optional `on` (true or false) | Turns the sandbox's network on or off for this session (error `no_sandbox` without one). Answered by `network`: `runner` as in `hello`, and `available` (whether `run_in_sandbox` is offered now). |
| `work` | `task`, optional `steps_per_item`, `time_limit` (seconds) | Starts a work session (see Work sessions) and sends the task; the turn ends with `done` as for `user`. Refused with `work_refused`: no sandbox, network on, not a git repository, no commits, uncommitted changes, or a session already on. Later `user` messages go on with the session. |
| `work_end` | | Ends the work session: answered by a `work` event with `state` `ended` (or `not_running`). |

While proposals are waiting for decisions, only `decision`, `result`, `attach`, `trace` and `system` are accepted.
Anything else gets the error `busy`, so coding mode and skills never change halfway through a
decision.

**Events genai sends**

| `type` | Meaning |
|---|---|
| `text` | A piece of the model's reply. |
| `proposal` | `id`, `kind` (`read`, `list`, `search`, `write`, `edit`, `command`, `plan`, `sandbox`, `trusted`, `external`, `invalid`), `digest` when it waits for an answer, and the proposal's fields, such as `path`, `content`, `diff`, `command` and `reason`. `auto: true` means it needed no approval: coding mode, or a read the approved plan names. A `plan` proposal has `goal` and numbered `steps` (`n`, `kind`: `read`, `edit`, `write` or `command`, a `path` or the exact `command`, and `why`). While a plan is approved, a proposal it covers has `plan_step`; a command matching an approved step character for character also has `preapproved: true`, so the wrapper may run it without asking again; a command, edit or write the plan doesn't cover has `off_plan: true`. Edits and writes ask, except with edits `approved-files`, where one to a file you already approved has `auto: true` and `approved_file: true` (it is applied at once; show its diff). `secret: true` means the file looks like it holds secrets. A command is only shown: the wrapper runs it, if the user wants to, and can share the output with `attach`. A `sandbox` proposal (`command`, `reason`, `network`, `image`) is genai running the command in the sandbox: offline it has `auto: true` and runs at once; online it waits for a `decision`. A `trusted` proposal (`plugin`, `command`, `params`, `argv`, `read_only`) is a plugin's trusted command: `read_only` ones have `auto: true` and run at once; others wait for a `decision`. A `search` proposal (`pattern`, `path`, optional `glob` and `ignore_case`) is the model's `search_files`, offered only in coding mode, and always `auto`. In a work session, a `work_plan` proposal (`goal`, `items`: lines of text) waits for a `decision`; an edit or write in the copy has `auto: true` and `work: true`; a `finish` proposal (`n`, `summary`, `notes`, `test`) is always `auto`, and its `result` says whether the item was committed; a `retry` proposal (`n`, `hint`) is the planner starting a fresh attempt at a stuck item, always `auto`. |
| `result` | `id`, `status`, `detail`: what happened to a proposal that was performed. For a `sandbox` or `trusted` run, also `exit` (`null` if it was killed at the time limit or never ran) and `output`, exactly what the model is sent. |
| `done` | The turn ended. `reason` is `reply`, `command`, `stopped` (Ctrl-C during a sandboxed run), `step_limit` or `work_limit` (a work session paused at a limit); `context` gives `used` and `limit`; `exchanges` gives the first and last exchange numbers of this turn, for `trace` (also on an `error` that ends a turn). |
| `context` | The conversation is getting near the context limit. |
| `notice` | Something the user should know. |
| `error` | `code` and `message`. Among the codes: `api` (the endpoint failed or couldn't be reached; nothing was performed), `busy` (not allowed while proposals wait), `unknown_id` (no waiting proposal with that ID and kind), `digest_mismatch` (see Digests), `not_running` (a `stop` with nothing running), `work_refused` and `work` (a work session can't start, or git failed), `bad_message`, `unknown_type`, and, at the start, `session_endpoint_changed` (a resumed session was made with another endpoint; `--allow-session-endpoint-change` allows it). |
| `work` | A work session changed. `state` is `started` (`branch`, `base`, `path`, `steps_per_item`, `time_limit`, `test`, `planner`: `endpoint` and `model` for planning, or `null` when the profile's own model plans), `plan` (the plan was approved: `items`, each `n`, `title`, `status`, `commit`, and `current`), `item` (an item was committed), `context` (a fresh context: `role` `item`, or `review` for the planner taking back a stuck item; `item`; `why`), or `ended` (`why`: `done`, `ended` or `genai exited`; `branch`, `null` if nothing changed; `commits`, `stat`, `unfinished` (the commit of work left when it ended), `commands` to review and merge). |
| `plan` | The approved plan changed: approved, a step done, or dropped. `plan` has `id`, `goal` and `steps`, each with `approved`, `status` (`todo`, `done`, `left out`) and `edited` when the user changed its command; `null` when there is none. A plan is never saved with the session. |
| `attached`, `registered`, `compacted`, `cleared`, `models`, `model`, `coding`, `skills`, `files`, `file`, `trace`, `system`, `network`, `plugins`, `ran`, `edits` | Answers to the messages above. |

When stdin closes, every waiting proposal is declined and genai exits.

## Live test

The live test takes any endpoint file that uses `key_env`. It checks the endpoint and model list,
has the model write a bash script to stdout,
redirected to a file with `>`, then checks it with `bash -n` without running it. It also runs the
JSON proposal/decision round trip, and checks that a persona profile picks its endpoint and that
its persona text reaches the model.

## License

MIT. See [LICENSE](LICENSE).
