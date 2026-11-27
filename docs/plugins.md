# Plugins and trusted commands

A plugin is a directory with a `plugin.json` (and, for hoplon-tui, optionally a pane program). It defines
**trusted commands**: narrow, shell-free programs with named inputs, such as `squeue -h -u {user}`. You bless
the whole directory once, like a skill (`hoplon --bless plugins/slurm` shows every command, what each input
may be, which program it would run, and which ones ask every time), and hoplon refuses it again if any file in
it changes. Then `--plugin slurm`, `default_plugins`, a profile's `plugins`, or a `plugin` message from a
wrapper turns it on.

Each enabled command is a tool for the model, named `slurm__queue_user`. A command marked `"read_only": true`
runs without asking, like an automatic read in coding mode: you see what ran and its output, and the audit log
records it. Any other command asks every time, like a write. You (a hoplon-tui pane, through the `run` message)
can run the same commands with the same checks and limits. You decided once, at blessing time, exactly these
commands with exactly these inputs.

```json
{"name": "slurm", "description": "Slurm cluster status", "search": ["/usr/bin", "/opt/slurm/bin"], "env": ["SLURM_CONF"],
 "commands": {"queue_user": {"describe": "jobs of one user", "argv": ["squeue", "-h", "-u", "{user}"],
   "params": {"user": {"pattern": "^[a-z_][a-z0-9_-]{0,31}$"}}, "read_only": true, "min_interval": 5, "cache": 15}}}
```

- `name` is the directory's name (`a-z`, digits, `-`); commands are `a-z`, digits, `_`. Unknown fields anywhere
  refuse the plugin. `pane` (optional) names a file in the directory for hoplon-tui; hoplon only reports it.
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
  the same command with the same values returns its last result, marked cached). hoplon also allows 30 real
  runs a minute across all plugins.
- `usage` (optional): a short section the model gets with the command every time, as a list of lines (or
  one string): what each argument means, the gotchas, what the output looks like. At most 20 lines and 1200
  characters, with no control or hidden characters. It is in `plugin.json`, so the blessing covers it, and the
  blessing screen shows it. Broader know-how (reading job states, choosing between commands) belongs in a
  skill, so it isn't sent with every request.
- `env`: names of environment variables passed on. The program otherwise gets only `PATH` (the `search`
  directories), `LANG=C.UTF-8`, `HOME`, `USER` and `LOGNAME`. `PATH` and `LD_*` can't be listed.
- The program must be a regular executable file, and neither it nor its directory may be writable by group or
  others or owned by another user than you or root. hoplon records which file it found at start and refuses to
  run it if it changed ("restart hoplon").

The model's output from a command is redacted for your endpoint key and common secret patterns, cut like
sandbox output, and escaped wherever it is shown. `plugins/slurm/` has `sinfo`, `squeue`, `scontrol show` and
`sacct` commands, all read-only. Over JSON, see the `plugins`, `plugin` and `run` messages in the [JSON protocol](json-protocol.md).

## Plugins in hoplon-tui

`/plugins` lists the plugins hoplon found; `/plugins NAME on|off` switches one, and `/help` and the info panel
list the enabled ones' commands. A plugin's `pane` is a program in its directory (blessed with the rest of it)
that hoplon-tui starts with `/plugins NAME pane`, in a tmux pane of its own, as a tab in the right column beside the
info panel (see "The right column" in [hoplon-tui.md](hoplon-tui.md)); the same command takes it out of the column
and brings it back. A `.py` pane runs with hoplon-tui's Python, with `tui/` on `PYTHONPATH` so it can use
`ipc.Client`; any other is run as a program.

A pane never runs inside the conversation pane, and it never reaches hoplon. It connects to hoplon-tui's private
socket in the role `plugin:NAME`, and may only send `run` (one of its own plugin's **read-only** commands, by
`command` and `params`, with its own `id`; hoplon runs it with the usual checks, cache and rate limits, and the
`ran` answer comes back to that pane alone), `insert` (text for the input box) and `attach` (`text` and a
`label`, which open in your editor first, like shell context). A plugin that is off gets `{"type": "refused"}`.
Panes don't run in remote mode yet.

**The Slurm console** (`plugins/slurm/slurm.py`, `/plugins slurm pane`) shows nodes by state (down and drained
ones in red, with their reasons; the healthy ones only as counts), partitions, how many jobs are in each state,
and your own jobs. ↑↓ choose a node or job, Enter or a click shows its details (`scontrol show`), `i` puts its
name in the input box, `a` attaches what is shown (after you review it), `/` filters (and shows the healthy nodes
that match), `r` refreshes (hoplon's cache and `min_interval` still apply) and Esc goes back. Adding the pane
changed the plugin, so bless `plugins/slurm` again.
