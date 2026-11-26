# Hooks

A hook is a small program of yours that hoplon or its sandbox runs at a fixed point. This file is
the design: what hooks exist, what each one may do, and why the dangerous kinds are left out.
[SECURITY.md](SECURITY.md) explains the risks these rules answer.

## Rules for every hook

1. **A hook is sorted by what it may do.** It can observe, tighten, or set up the environment.
   Nothing that loosens a rule or acts for you is a hook.
2. **Hooks are never loaded from the working directory or from a repository.** A cloned project
   cannot bring one. They live in directories only you can write.
3. **A hook is your code and runs as you.** It is as trusted as your `.bashrc`. The protection is
   that nothing the model can reach can create or change one.
4. **hoplon itself loads no code.** A hook is a separate program. It gets a small, fixed input and
   can answer only in a small, fixed vocabulary. Anything else it prints is an error or is thrown
   away.
5. **What a hook did is visible.** It is printed or shown in the banner, and logged.
6. **A hook that fails closes.** A broken sandbox hook stops the sandbox from starting. A broken
   hoplon hook is not run, and hoplon carries on without it.

| Kind | May do | Where | Status |
| --- | --- | --- | --- |
| Environment | Add read-only mounts and pass variables into the sandbox | `sandbox/run` | Built |
| Observe | Learn that hoplon is waiting for you | `hoplon` | Built |
| Tighten | Decline a proposal | `hoplon` | Designed, not built |
| Command runner | Send `:run` commands into a sandbox | `hoplon` | Designed, not built |
| UI | Anything a front end does | A wrapper over `--json` | Exists; not a hook |
| Loosen or act | Auto-approve, add tools, run things at start | A wrapper only | Never a hook |

## Sandbox hooks (environment)

`sandbox/run` runs every executable file in its hooks directory, in name order, before it starts
the container.

- **Directory:** `$HOPLON_SANDBOX_STATE/hooks`, by default `~/.local/share/hoplon-sandbox/hooks`.
  `HOPLON_SANDBOX_HOOKS` names another directory. It is not mounted into the sandbox.
- **Checks:** the directory and each hook file (after resolving symlinks) must be owned by you and
  not writable by group or others. Otherwise the sandbox does not start. A file that is not
  executable is ignored, with a note. A hook name is letters, digits, `.`, `_` and `-`.
- **How a hook is run:** as its own process, with the mode (`tests`, `live`, `selfcheck`, `hoplon`, `shell`)
  as its one argument, your environment, and no input. If it exits non-zero, the sandbox does not
  start.
- **How a hook answers:** on standard output, one request per line. Blank lines and lines starting
  with `#` are ignored. Any other line, or a line with a control character in it, is an error, and
  the sandbox does not start. The output is read as text. It is never evaluated.

| Request | Meaning |
| --- | --- |
| `ro DIR` | Mount the host directory `DIR` read-only at the same path. |
| `path NAME` | Pass the variable `NAME` as a search path, filtered (below). |
| `env NAME` | Pass the variable `NAME` unchanged, by name. |

- **`ro`** goes through the same checks as `HOPLON_SANDBOX_RO`: an absolute path to a directory,
  not your home directory or anything that contains it, not covering the sandbox's own mount
  points, and not under `/proc`, `/sys`, `/dev` or `/run` (a socket or device there would work
  even through a read-only mount). A request that fails a check is skipped with a note, so a hook
  can guess. So is one for a directory that is already mounted.
- **`path`** keeps only the entries that are absolute and, after resolving symlinks, lie under a
  directory the sandbox mounts read-only. Empty and relative entries are dropped: to the loader
  they mean "the current directory", which in the sandbox is the writable `/work`. Each kept
  entry is passed as its resolved path. An entry whose resolved path has a `:` in it is dropped. If
  nothing is left, the variable is not passed. All `ro` requests from all hooks are handled before
  any `path` request, so order does not matter.
- **`env`** refuses `HOME`, `XDG_CONFIG_HOME`, `HOPLON_SANDBOX`, `PATH`, `PYTHONPATH` and every
  name starting with `LD_`. Search paths go through `path`. `path` accepts `LD_LIBRARY_PATH` but
  no other `LD_` name, so `LD_PRELOAD` and `LD_AUDIT` cannot be passed at all. A refused name is an
  error, and the sandbox does not start.
- **Only your environment.** A name is passed only if it was in the environment `sandbox/run`
  was started with. A hook cannot pass one of the launcher's own variables.
- **Values are never on a command line.** Variables are passed to podman by name. For `path`,
  the filtered value is put in podman's own environment first, so podman itself also runs with
  the filtered `PATH`. That matters only if podman's helper programs (`crun`, `conmon`) are
  outside every mounted directory: podman then fails to start the sandbox.
- **What you see:** one line per hook on standard error, listing what it added and what was
  skipped.

A hook cannot make anything writable, change the sandbox's flags, or open the network. The verbs
do not allow it, and the hook's process is separate from the launcher's.

### The modules hook

`sandbox/hooks/modules.sh` makes software from environment modules (Lmod, Environment Modules,
EasyBuild) work inside the sandbox. Load your modules in your own shell, then start the sandbox.

```bash
mkdir -p ~/.local/share/hoplon-sandbox/hooks
ln -s /path/to/hoplon/sandbox/hooks/modules.sh ~/.local/share/hoplon-sandbox/hooks/
module load gcc/13 openmpi
sandbox/run hoplon -e NAME
```

- It looks at `PATH`, `LD_LIBRARY_PATH`, `LIBRARY_PATH`, `CPATH`, `C_INCLUDE_PATH`,
  `CPLUS_INCLUDE_PATH`, `PKG_CONFIG_PATH`, `CMAKE_PREFIX_PATH` and `MANPATH`.
- For each entry outside `/usr`, `/etc` and your home directory, it asks for the package's
  prefix: `/apps/gcc/13/bin` becomes `ro /apps/gcc/13`. Then it asks for each variable with `path`.
  It cannot tell which entries came from a module, so every such entry is mounted, whatever put
  it on your `PATH`. The line `sandbox/run` prints lists them.
- `HOPLON_MODULE_ROOTS` (colon-separated) names whole software trees to mount, such as `/apps`.
- Any other variable whose value is a path under one of those prefixes is passed with `env`. That
  covers `EBROOTGCC`, `MKLROOT`, `CUDA_HOME` and the like.
- Entries under your home directory are left out. Mount such a tree yourself with
  `HOPLON_SANDBOX_RO`.
- It never asks for `/` or a top-level directory by itself: `/apps/bin` becomes `ro /apps/bin`,
  not `ro /apps`. Name a whole tree in `HOPLON_MODULE_ROOTS` if you want it. `/opt` is refused even
  then, because the sandbox keeps its own files in `/opt/hoplon`. `/opt/intel` works.
- It has been tried with a made-up module tree, not yet on a real Lmod or EasyBuild system.

## The notify hook (observe)

`~/.config/hoplon/hooks/notify` is run when hoplon starts waiting for you, so you can be told: a
desktop notification, a sound, a message to your phone.

- **Blessed like an endpoint.** `hoplon --bless ~/.config/hoplon/hooks/notify` shows the file and
  records its sha256. A hook that is not blessed, or changed since, is not run, and hoplon says so
  once. A hook must be a UTF-8 text file, so that it can be shown. Hidden characters are listed.
- **Checks at each use:** a regular file (not a symlink), owned by you, not writable by group or
  others, executable, and its hash matches.
- **When:** only with a person at the terminal (the REPL, or a one-shot run on a terminal). In
  `--json` mode the wrapper is the UI and sees every event already.

| Event | Meaning |
| --- | --- |
| `approval` | hoplon is about to ask you to accept or decline proposals. Carries `count` and `kinds`. |
| `command` | One or more commands were proposed. They are shown, not run. Sent once per batch. |
| `reply` | The model finished its turn with a reply. Not sent when the turn ended with a proposed command, an error or Ctrl-C. |

- **Input:** one line of JSON on standard input, then end of input:
  `{"event": "approval", "count": 2, "kinds": ["edit", "write"], "session": "nightly",
  "cwd": "/home/you/project", "endpoint": "work-llm", "sandbox": null, "version": "1.1.0"}`.
- **What it never gets:** file contents, proposal paths, command text, model text, or the key. The
  endpoint's key variable is removed from the hook's environment.
- **Output:** thrown away. A hook cannot write to your terminal through hoplon, and it has no
  controlling terminal. Nothing it returns changes what hoplon does.
- **Limits:** hoplon does not wait for it, and stops it (and anything it started) after 10
  seconds. If hoplon itself exits sooner, as a one-shot run does, a hook that is still running is
  left to finish. A hook that fails is ignored.
- **Record:** the banner says the hook is active, and each run is in the audit log as a `hook`
  record with the event's name in `when`.

`hooks/notify-desktop` in this repository is an example that calls `notify-send`. Copy it to
`~/.config/hoplon/hooks/notify`, read it, and bless it.

What remains: the hash is checked just before each run and the file is then run by path, so a
change in that instant would not be caught. Only something already running as you could make it.

## Designed, not built

### Policy hook (tighten)

`~/.config/hoplon/hooks/policy`, blessed the same way. It would see each proposal before you do and
could only decline it.

- Input: one proposal as JSON, with its kind and path, and the command text for a command. That is
  more than the notify hook gets, because a policy has to judge the content.
- Output: nothing, or one line `{"decision": "decline", "reason": "..."}`. There is no "accept".
- A non-zero exit, a timeout or output it can't parse means decline.
- It would run in every mode, `--json` included, and before coding mode's automatic reads.
- You would see "declined by policy" and the reason. The model would get "declined by the user".

### Command runner

A blessed setting that names a command prefix for `:run`, so that hoplon runs on the host and each
command runs in the sandbox. The key would then never be in a command's environment, which closes
a gap SECURITY.md lists. It needs `sandbox/run` to grow an `exec` mode, and the sandbox indicator
would have to come from the runner's own report, not from a variable.

### More sandbox requests

`network none` and lower limits could be added as requests, since they only tighten. Requests that
loosen (a writable mount, more capabilities, host networking) will not be.

### A reference wrapper

A small example front end over `--json`, to show that UI features belong there.

## Left out on purpose

- **Auto-approval rules.** Allowlists such as "always run `git status`" are how other agents get
  bypassed, through chained commands and crafted arguments. A wrapper can decide automatically; it
  is then plainly the wrapper author's choice.
- **Tools from a file.** New tools come only from a wrapper over `--json`.
- **Commands at session start.** Use your shell: `hoplon -f <(git status)`.
- **Code loaded into hoplon.** No plugins, no imports. hoplon stays one file.
