# hoplon-tui

hoplon-tui runs hoplon in tmux panes beside your own shell: the conversation, an info panel, and the shell (Ctrl-` shows or hides it). `/help` lists its commands.

## Another host from hoplon-tui

In hoplon-tui's shell pane, connect with `hoplon-ssh` instead of `ssh`:

```
hoplon-ssh -p 2222 me@login1.cluster      # ssh options as usual; no remote command
```

It is your own ssh (config, keys, agent) starting bash on that host with hoplon-tui's prompt hook,
so hoplon-tui follows each command there as it does here: `/run` sends a proposed command to that
host (the question says so, in red), `/share N` and the info panel show which host a command ran
on, and nothing is installed there. The session has a key of its own that never appears on a
command line or on screen; see SECURITY.md, section 7. Plain `ssh` still works, but hoplon-tui
can't follow it, and `/run` refuses while it runs.

## Remote mode

```
hoplon-tui --remote me@devbox --dir /srv/project                    # hoplon runs on devbox, in that directory
hoplon-tui --remote me@devbox --dir /srv/project --as svc           # ... as another user, through sudo -n
hoplon-tui --remote me@devbox -e work -p coder -- -J me@bastion     # a bastion; more ssh options after --
```

The screen, your endpoint's API key and your blessed configuration stay on your machine. hoplon runs on the
host, in PATH, as you or as `--as USER` (which needs `sudo -n -u USER` to work without a password there; the
launcher checks). The shell pane is a followed session on the same host, as the same user, in the same directory,
and says `disconnected from HOST` and offers to reconnect if it drops. Prefer ProxyJump (`-J`, or `ProxyJump` in
`~/.ssh/config`) to agent forwarding for a bastion: the connection never offers your agent to the host, and hoplon-tui
passes `-a` to ssh so a config file can't either.

What happens, before tmux starts (so a password or second factor is asked for in your own terminal):

1. `hoplon --remote-config` resolves `-e` and `-p` exactly as a normal start would (default endpoint and profile,
   blessing checks, a changed file is refused) and prints the endpoint and profile *without* `key_env`, `key_file`,
   `proxy`, `headers` or `ca_bundle`. The skills and plugins in use (the profile's, or `default_skills` and
   `default_plugins`, plus `-k`) go with their files, if they are blessed here; one that isn't is left out, with a
   message. On the host they are checked against the hashes blessed here, the plugins' programs are looked for there
   (a command whose program is missing there is left out, as locally), and nothing blessed only on the host can be
   used. So the Slurm plugin's commands run on the cluster. `-S` is not available with `--remote` yet.
2. An ssh master connection (`ssh -M -S STATE/cm -fN`, then the host's checks: Python 3.10 or newer as the target user,
   `sudo -n` for `--as`, the directory) whose control socket lives in hoplon-tui's private 0700 state directory.
3. `tui/keyproxy.py` starts here on a Unix socket, with a random token on its stdin.
4. `ssh -O forward -R 127.0.0.1:0:STATE/kp.sock` opens a loopback port on the host that leads to that proxy.
5. The conversation pane runs hoplon there over the same connection: a short Python program reads hoplon's own source
   from stdin (it is too big for a command line), runs it, and hoplon then reads a config line (the endpoint, whose
   `base_url` is that loopback port, the profile and the token) and the usual protocol. Nothing is installed or
   left on the host, and it is always the same hoplon version as yours.

The proxy accepts only `GET /v1/models` and `POST /v1/chat/completions` with the token, adds the key and the
endpoint's headers, and sends the request on through the endpoint's own TLS and proxy settings. On exit the proxy,
the forward and the ssh connection are closed; detaching tmux keeps the launcher running to hold them, so Ctrl-C there
ends the session.

You can see where it runs: the banner and the info panel say `hoplon runs ON host AS user in dir` (red as root), the
status line names the host, and the banner gives the audit log's path on the host (`hello`'s `audit`). The sessions,
audit log and `~/.config/hoplon` that hoplon uses are those of the user it runs as on the host; its endpoint is not
blessed there, because the blessing happened here. Test it against a SLES container with `tests/remote_sshd.sh test`, or against a real host of yours with
`HOPLON_TEST_REMOTE_HOST=me@devbox python3 -m unittest tests.test_remote.TestRealHost` (a turn, then a sandboxed run and a
stopped one if the host has rootless podman and `HOPLON_TEST_REMOTE_IMAGE`, default `docker.io/library/alpine:3`, pulled; it works in a
`mktemp -d` directory there and removes it).

Limits: the host's login shell must be POSIX-like, its sshd must allow TCP forwarding (`AllowTcpForwarding`), and
`python3`, `python3.13` ... `python3.10` is searched in that order for 3.10 or newer (SLES: `zypper install python311`).
Another user of that host can reach the forwarded port, which is why the token matters (SECURITY.md).

## File tree

`/files` shows or hides a file tree of the working directory on the left (`hoplon-tui --files` starts
with it shown). Move with the arrow keys; Right or Enter opens a directory and Left closes it; `.`
shows or hides hidden files and `r` refreshes. On a file, Enter views it in the pager, `a` attaches it
to your next message and `i` puts its path in the input box. A `⚠` marks a file hoplon thinks holds
secrets, `⚿` one the model would have to ask about first, and `↪` a symlink, which is not followed.

Browsing sends nothing to the model. Listings and files come through hoplon, which applies its usual
checks and masks secrets it recognises. `a` opens the file in your editor first, like shell context:
what you save goes with your next message, and nothing does if you save an empty file.

**Viewing.** A file, a whole proposal (`v`) and the raw view open in hoplon-tui's own pager, in a popup: Esc or `q`
closes it, the arrows, PgUp/PgDn and the wheel scroll, `/` searches and `n`/`N` move between matches. To use
another, set `HOPLON_TUI_PAGER` (for example `HOPLON_TUI_PAGER=less`).

## Features

hoplon-tui's `/model`, `/copy`, `/decline`, `/raw`, `/system`, `/files`, `/sandbox`, `/edits`, `/plugins` and `/work`, and the `hoplon-ssh` handshake, are
features, each in its own module under `tui/features/`. To switch some off, list them in
`~/.config/hoplon/tui.json` (`$XDG_CONFIG_HOME/hoplon/tui.json`):

```json
{"disable": ["copy", "model"]}
```

or set `HOPLON_TUI_DISABLE=copy,model` for one run. Names are `model`, `copy`, `decline`, `raw`,
`ssh`, `files`, `sandbox`, `edits`, `plugins` (without it, no plugin pane can connect), `work`, `fixtures`, `leftovers` and `alerts` (without it, no bell). A disabled feature has no command, help line or menu item, and hoplon-tui's socket
refuses its requests. Without `ssh`, a hoplon-ssh session isn't followed. No file means
everything is on; a file that can't be read is ignored, with a note in the conversation.

When something waits for your decision, hoplon-tui rings the terminal bell once and tmux's status
line shows `⏸ waiting for you` until you answer, so you notice from another window. Most terminals
turn the bell into an alert or a highlighted window. `/alerts off` silences the bell for this run
(`/alerts on` brings it back); `{"alerts": false}` in `~/.config/hoplon/tui.json` keeps it off from
the start, and `HOPLON_TUI_ALERTS=off` or `on` overrides that for one run. The status line shows
`⏸ waiting for you` either way. (hoplon's notify hook doesn't run under hoplon-tui.)

hoplon-tui shows a splash while tmux starts, for about a second, on a terminal of at least 60×24.
`HOPLON_TUI_SPLASH=0` or `NO_COLOR` turns it off.
