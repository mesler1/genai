# Sandbox

## Running hoplon in a sandbox

The core has no sandbox by design; that is left to whatever wraps it. `sandbox/run` runs hoplon or its
tests in a rootless podman container made from this machine's own OS. There is no image to build or
pull: the container's root is an empty tree with your `/usr` and `/etc` mounted read-only, so it uses
the Python and editor you already have. Your home directory is not in it. The container is read-only
and has no capabilities. Only `/work` is writable. It uses its own hoplon config, never your real
`~/.config/hoplon`.

```bash
sandbox/run tests          # offline suite, --network=none
sandbox/run selfcheck      # the live test's harness against a fake endpoint, --network=none
sandbox/run live ~/.config/hoplon/endpoints/work-llm.json   # live test; needs the endpoint's key variable
sandbox/run hoplon -e NAME  # interactive, with ./sandbox-work mounted at /work
sandbox/run shell [--net]  # a bash in the same sandbox: no network unless --net, no key variable
```

`sandbox/run` documents a small contract at its top: what is mounted where, which variables pass
in, and `HOPLON_SANDBOX=<name>`. Your own sandbox can replace podman by following it. hoplon shows
where commands run: the REPL banner, the `:run` confirmation and result line, the `hello` event, and
the audit log all say **inside the <name> sandbox** or **OUTSIDE any sandbox**. That is an
indicator, not a guarantee. It comes from `HOPLON_SANDBOX` or podman's or docker's marker file.

Inside `sandbox/run hoplon` the REPL marks itself so you can tell: the prompt is `(sandbox) > `, the
banner's second line is in bold (`IN THE PODMAN SANDBOX: /work = <host path>, no host home`), and
`:sandbox` says where hoplon and your commands run, the working directory, the config directory and
whether the network looks available. Outside a sandbox the prompt is the plain `> ` and the banner says
**OUTSIDE any sandbox**. `sandbox/run shell` opens a bash in the same sandbox, with the prompt
`[sandbox:podman /work] DIR$` in reverse video and a banner saying what is mounted. It has no network
unless you pass `--net`, and the key variable is never passed in.

`sandbox/run hoplon` keeps its own config in `~/.local/share/hoplon-sandbox/config`; put and bless
endpoints there, and set `HOPLON_KEY_ENV` to the key variable to pass in.

| Variable | Meaning |
| --- | --- |
| `HOPLON_SANDBOX_WORK` | Host directory mounted at `/work` (default `./sandbox-work`). |
| `HOPLON_SANDBOX_PYTHON` | The Python to run inside (default `python3`). It must be under `/usr` or a directory you add. |
| `HOPLON_SANDBOX_RO` | More host directories to mount read-only at the same path, colon-separated, for a Python or editor outside `/usr`. Your home directory is refused. |
| `HOPLON_SANDBOX_HOOKS` | Directory of sandbox hooks (default `~/.local/share/hoplon-sandbox/hooks`). See [Hooks](hooks.md). |

Anything your user can read under `/usr` and `/etc` can be read inside the sandbox, and
`sandbox/run hoplon` has network access, since hoplon has to reach the endpoint. `sandbox/run shell`
does not, unless you pass `--net`.

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
  the endpoint's key). Before the command runs, hoplon checks inside the container that each
  of them really is empty, and refuses to run it if one isn't. Virtual environments (directories
  with a `pyvenv.cfg`) and `node_modules` aren't scanned. Your home directory, `/tmp` and `/run`
  are not in the sandbox at all; its `HOME` is its own `/tmp`.
- **Offline.** `--network=none`, and no proxy variables. Nothing can be downloaded, so the image
  must already have the project's tools and dependencies, or the project a virtual environment.
- **Limited.** A time limit (`timeout`, default 300 s), a 1 GB `/tmp`, a read-only root, no
  capabilities and `no-new-privileges`. It also gets memory (`memory`, default `"4g"`) and process
  (`pids`, default 1024) limits where the host delegates cgroups to you. Where it doesn't, such as
  a login without a systemd user session, hoplon says so once and runs without them.

An offline run needs no approval: you see the command, then its exit code and output, and so does
the model. Ctrl-C stops it and ends the turn. Every run is in the audit log, by fingerprint.

**Seeing it in hoplon-tui.** The info panel has a SANDBOX section: whether hoplon itself runs in a
sandbox (`in the podman sandbox`, green, or `not sandboxed`, the usual way to run it, from `hello`'s `sandbox`), and
the model's sandbox: `none`, `ready · offline` or `ONLINE` with its image, `can't run: …`, or
`RUNNING p3 · 12s` while a run is going. tmux's status line says `⊡ sandbox` or `not sandboxed`,
and `⏵ sandbox p3 12s` during a run. A running card counts its seconds. Ctrl-C on an
empty input, or `/stop`, stops the run: hoplon-tui sends hoplon the `stop` message, which works in
remote mode too (an older hoplon without it gets a SIGINT here, and in remote mode its run ends at
its time limit). `/sandbox` prints all of it, with the image, network and limits.

**The network switch.** `:network on` (or the JSON `network` message) gives the sandbox the
network for this session; `:network off` takes it away, and a new session starts as the profile
says (`"network": true` starts it on, which blessing flags). While it is on, **every sandboxed
command asks first**, because code that can read the project and reach the network could send the
project anywhere.

**Not in your home directory.** The sandbox never runs where coding mode is refused (your home
directory, a directory above it, `~/.ssh` and other credential directories, hoplon's config
directory), even with `--coding-anywhere`: the copy would hold everything there, and offline runs
don't ask. `run_in_sandbox` is then not offered, and the banner says why. Start hoplon in the
project's own directory.

The image is used as it is (`--pull=never`); pull or build it yourself. Podman is the only engine
so far (`"engine": "podman"` or `"auto"`).
