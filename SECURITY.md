# Security

This document says what can go wrong in an application like hoplon, a language model that can
ask for commands and file operations on your machine, and what hoplon does about each risk. It
ends with what a review of the code on 2026-11-21 found, how each finding was resolved, and what
stays your job.

[DECISIONS.md](DECISIONS.md) records the individual design choices. This file is the map from
recognised agent-security concerns to those choices.

## The short version

- **Assume the model can be turned against you.** Any text it reads can carry instructions: a
  file, a command's output, an attachment. No prompt wording reliably prevents that, so hoplon's
  rules are enforced in code, not in the system prompt.
- **The model proposes, the human acts.** None of its tools executes anything on your system.
  A command is only ever shown. A file write happens only after you accept the exact change. The
  one opt-in exception is a profile's sandbox: in coding mode the model may run commands in an
  offline container, on a throwaway copy of the project, with secret files hidden (section 5).
- **The model has no way out of the machine by itself.** There is no fetch tool, no rendered
  links or images, and one network destination: the endpoint you reviewed.
- **What you see is what runs.** Model text is escaped before it reaches the terminal, and an
  approval is bound to the state of the file you looked at.
- **Nothing is trusted because of where it sits.** Endpoints, personas and skills are used only
  after you bless them, and are refused again the moment they change. hoplon reads no
  configuration from the working directory.

## What hoplon trusts

| Party | Trust | Why |
| --- | --- | --- |
| You, your shell environment (`EDITOR`, `SHELL`, variables) and `~/.config/hoplon` | Trusted | Anything that can change these already acts as you. |
| A wrapper driving `--json` | Trusted | It is the user interface: it shows proposals and returns decisions. |
| Programs running as you, including commands you run from hoplon-tui's shell pane | Trusted, unavoidably | They can reach the TUI's socket and its tmux server as you can. Run hoplon-tui in the sandbox to keep proposed commands outside that boundary. |
| Endpoints, profiles, skills | Trusted only as blessed | You reviewed a specific version. Its sha256 is recorded, and any change is refused. |
| The endpoint's operator | Sees everything you send | That is what an endpoint is. Its replies are still treated as untrusted text. |
| The model's output: text, tool names, tool arguments | Untrusted | It may be wrong, or steered by something it read. |
| Everything the model reads: files, command output, attachments, tool results | Untrusted | This is where injected instructions come from. |

hoplon runs as you, with your permissions. It is not a sandbox, and it says so wherever a command
is about to run.

## The concerns, and how hoplon addresses them

Identifiers refer to the sources listed at the end: `LLMnn` is the OWASP Top 10 for LLM
Applications 2025, `ASInn` the OWASP Top 10 for Agentic Applications 2026, `AML.Tnnnn` MITRE
ATLAS, and `NISTAML.nnn` NIST AI 100-2e2025.

### 1. Prompt injection and goal hijack

*LLM01, ASI01, NISTAML.015 and .018, AML.T0051.* Text the model reads is indistinguishable, to
the model, from instructions. The UK NCSC's position is that this may never be fully fixed and
that the defence has to be deterministic controls around the model.

- hoplon does not try to detect injections. It limits what a hijacked model can do: see 2 to 5.
- The system prompt states the rules, but every rule is also enforced in code: the path check,
  the approval step, the step limit. A persona is appended after the rules and cannot grant
  anything.
- The ways untrusted text gets in are few and known: `read_file`, `list_dir` and `search_files`, `-f` and
  `:attach`, output you share from `:run` or `:sh`, and tool results from a wrapper. There is no
  web or URL tool.
- What you share reaches the model fenced, after your words: a header hoplon writes says it is
  shared data, not instructions, and a random tag made after the text exists closes the block, so
  the text can't pass itself off as your message. That helps the model read it; it is not a
  control. The audit log records each share's size and SHA-256.

What remains: a hijacked model can still propose a harmful command or edit that looks
reasonable. Your review is the control, so read proposals as if a stranger wrote them.

### 2. Data exfiltration and the "lethal trifecta"

*LLM02, AML.T0086; Willison's lethal trifecta; Meta's Agents Rule of Two.* An agent that holds
private data, reads untrusted content and can communicate outward can be made to leak. The
advice is to remove one of the three, or put a human in front of it.

- hoplon removes the third leg from the model's reach. The model cannot make a request, and
  nothing it writes is rendered: no Markdown, no images, no clickable links (OSC 8 hyperlink
  sequences are escaped like any other control sequence).
- The only network destination is the blessed endpoint. Redirects are refused and nothing more
  is sent. `OPENAI_BASE_URL` and the proxy environment variables are ignored.
- Switching models (`"model": "auto"`, `:model`, hoplon-tui's `/model`, a profile's `model`) stays on
  that server: only a model it lists, or an alias in the blessed endpoint file, can be chosen, and every
  switch is in the audit log. An alias's name hides a listed model of the same name. The list comes from the
  server, so it is treated as untrusted text. A name with anything outside letters, digits and
  `._:/@+=-` is left out rather than cleaned, the list is capped at 1 MB and 200 names, and
  hoplon-tui checks each name again before it shows it in a tmux menu (whose item names tmux
  expands as formats). Each menu item sends back only its position, never the name.
- Anything else that leaves the machine goes through a command you chose to run.
- A sandboxed run (section 5) has no network unless you switch it on for the session, and then
  each run asks first. Its output goes to the endpoint, like a file the model reads, after the
  same masking; files that look like secrets are hidden from it.

What remains: the endpoint receives every prompt, attachment and file the model reads. In
coding mode the model reads non-hidden files in the working directory without asking (hidden: a
name starting with `.`, or one the sandbox hides as a secret, such as `id_rsa` or `*.key`; a file
that looks like it holds secrets still asks), so treat the directory as shared with the
endpoint. When you allow such a read and say the file holds no secrets after all, hoplon
remembers the sha256 of that text (`not-secret.json`, and a `not_secret` event in the audit log):
that text no longer asks, is searched and is not hidden in the sandbox; any change asks again. A
file holding the endpoint key is never excused.

### 3. Excessive agency and tool misuse

*LLM06, ASI02, AML.T0053, AML.T0101.* The causes OWASP names are too much functionality, too
much permission and too much autonomy.

- **Functionality.** Six tools: propose a command, read a file, list a directory, write a
  file, edit a file, propose a plan. No delete, no fetch, no execute on your system. A seventh,
  `run_in_sandbox`, exists only in coding mode with a profile's sandbox (section 5). Command sets you bless and enable add their scoped commands as tools (below).
- **Permission.** File tools take relative paths only. `..`, absolute paths and `~` are
  rejected, and each path is walked one component at a time with `O_NOFOLLOW`, so a symlink
  anywhere in it is refused. Reads refuse FIFOs, devices and binary files. New files are
  created without execute permission. A hard link can't be told apart: it is the same file
  under a second name, so a hard link in your project to a file elsewhere is read like any
  other file there. The model can't make one, and Linux normally lets you hard-link only
  your own files, so only a hard link you made yourself does this. Files that look like
  secrets still ask, wherever they are linked from.
- **Autonomy.** A turn ends when a command is proposed. A turn pauses after 50 model steps in a
  row in which nobody decided anything (`max_steps` in config.json, 1 to 1000), which also caps
  how many automatic sandboxed runs one turn makes. A step where you approve or decline something
  starts the count again: you are the brake then.
  Reads are capped at 256 KiB, writes at 1 MiB, overwrite snapshots at 8 MiB, and directory scans at 500 entries plus a truncation check. Without a terminal and
  outside JSON mode, nothing is performed at all.

#### Scoped commands (command sets)

A command set is blessed as a directory, like a skill: its hash covers every file, and a changed command set is refused
until you bless it again. What you bless is a list of commands, each a fixed argument list with named inputs
and the rules for each input (a path inside named directories or among named files, a host from a list, a
number range, a list of values, or a regular expression matched against the whole value). Together they are the
whole of what the model and the user's panes can run.

- **No shell.** The program is started directly with an argument list. A value fills one argument and is
  never split, expanded or re-read, so `;`, `$(...)` and quotes in a value are only characters. Values that
  break the rules (or are over 256 characters, or have a NUL or a newline) are refused, never cleaned.
- **Paths.** A `path` value is resolved (`..` and symlinks) before it is checked, and the program gets the
  resolved path. Hidden and secret-looking names are refused unless the set lists them as exact files. The check
  and the program's open are two steps, so a symlink swapped in between can get past it: a command set can't
  close that race, and only the sandbox does. Don't give a `path` under a directory that others can write to.
- **Fixed programs.** The program is found only in the command set's `search` directories (or by an absolute path),
  never through your `PATH`. It must be a regular executable file that neither group nor others can write, as
  must its directory, owned by you or root. hoplon records the file it found and refuses to run it if it was
  replaced afterwards.
- **Minimal environment.** The program gets `PATH` (the search directories), `LANG`, `HOME`, `USER`, `LOGNAME`
  and the variables the command set lists; `PATH` and `LD_*` can't be listed. Standard input is empty.
- **Limits.** A time limit (at most 120 s), an output cap (at most 1 MiB, then the program is killed), its own
  process group (killed whole), at least `min_interval` seconds between real runs of one command, a cache so a
  repeated question is answered from the last result, and 30 real runs a minute across all command sets.
- **Auto-run versus asking.** Each command must say `"access": "read"` or `"write"`; there is no default. Read
  commands run without asking, and the blessing screen lists them apart from the write commands. A write command
  asks, in the normal approval flow, each time, unless you let it run without asking: blessing asks about each
  one, after you accept the set, and the answer is kept with the blessing, so it lapses when the set changes. The
  model never sets this: it can write a command set, but not bless one. Commands that run without asking work only
  where someone can see what runs (not in plain, non-interactive mode), like automatic reads. In remote mode the
  answers go with the set from your machine.
- **Commands run as root.** A command whose program is `sudo`, `doas` or `run0` runs as root (or the user `-u`
  names). It asks every time, reads included, unless you let it run without asking when blessing; the blessing
  screen lists it apart, every approval says AS ROOT, and a hoplon-tui pane never runs it. It must say it never asks for a password (`-n`, or `--no-ask-password`); only `-u` and sudo's
  `-H` are accepted besides, so no option can start a shell, edit a file or keep the environment; and the program
  it runs must follow as an absolute path. `pkexec`, `su` and `sudoedit` are refused. hoplon checks the escalating
  program like any other; what it then runs is limited by your sudoers (or doas.conf) rule, which should allow
  exactly those commands. Placeholders still can't start with `-`, so the model can't add options to sudo.
- **Audit.** Each run (or refusal) is logged as `trusted_run`: command set, command, the validated values, who asked
  (`model` or `user`), exit code, time, bytes, whether it was cached or rate limited, and the program's path.
  Output is masked for the endpoint key and secret patterns, cut, and escaped wherever it is shown.
- **No options.** No parameter value may start with `-`, whatever its pattern says, so the model can't pass the program an option
  (another cluster, an output file, a format). A pattern, enum value or number range that would allow one is
  refused when the command set loads.

What remains: a read-only command's output goes to the endpoint like any file the model reads, and cluster
data can be sensitive (other users' names, job names, node names, paths in job details). Read the commands
before you bless them: a broad pattern still lets the model choose freely within it. Command sets are not available
through `--config-stdin` yet.

A command set's pane (hoplon-tui) is third-party code too, blessed with the command set's directory and run as you. It runs
out of process, in a tmux pane of its own: never inside the conversation pane, which holds the answers to hoplon
(and, in remote mode, the ssh control socket and the key proxy's token). Its socket role, `commands:NAME`, may only
run its own command set's read-only commands through hoplon (with hoplon's checks, cache and rate limits), put text in
the input box, where you see it before anything is sent, and offer text that opens in your editor before it can
go with a message. It can't answer a question, approve anything, or run a command that would ask. As for every
pane, the boundary is your user account: another program running as you could connect in that role, as it could
drive tmux. hoplon-tui starts the pane from the directory hoplon reported; it isn't re-hashed at that moment.

### 4. Identity, privilege and credentials

*ASI03, AML.T0083, AML.T0098.* Agents get compromised through the credentials within their
reach, and through configuration that redirects those credentials.

- The key is never in an endpoint file. The file names a variable (`key_env`, a plain
  identifier) or a file (`key_file`, which must be mode 0600 and yours).
- The key goes in one header to one URL. `Authorization` and `Cookie` are refused in `headers`,
  credentials are refused in `base_url`, and redirects are never followed, so the key cannot be
  forwarded to another host.
- The key is never put in a prompt. Blessing shows where the key comes from.
- The key's exact value is masked in command output, wrapper attachments and wrapper tool
  results before they go to the model, whatever variable name it appears under.
- hoplon holds no extra privilege and has no way to escalate. The system prompt tells the model
  to say when a step needs root, and you do it.

What remains: a command you run with `:run` or `:sh` inherits your environment, the key
included, exactly as it would in your own shell.

### 5. Unexpected code execution and sandbox escape

*ASI05, AML.T0105.* The usual failure is model output reaching an interpreter without a person
in between.

- No model output is ever executed by hoplon on its own. `:run` opens the command in your
  editor, shows the result with hidden characters escaped, says where it will run, and asks
  `[y/N]`.
- The core has no sandbox by design. `sandbox/run` supplies one with rootless podman and no
  image: the root is an empty tree with this machine's `/usr` and `/etc` mounted read-only, so
  there is nothing to pull and no base image to trust or keep patched. Your home directory,
  `/root`, `/var`, `/run`, `/mnt` and `/media` are not in it. All capabilities are dropped,
  `no-new-privileges` is set, processes and memory are limited, the repo is read-only, one
  directory is writable, and hoplon uses its own config rather than yours.
- The banner, the `:run` confirmation, the `hello` event and the audit log all say whether
  commands run inside a sandbox or directly on the host. Inside `sandbox/run hoplon` the REPL also
  marks itself: a `(sandbox) ` prompt, a bold banner line, and `:sandbox` for details. This is
  display only, from `HOPLON_SANDBOX` and `HOPLON_SANDBOX_WORK_HOST`.
- `sandbox/run shell` opens a bash in the same sandbox (same root, `/work`, config, flags and hooks).
  It has `--network=none` unless you pass `--net`, and the endpoint's key variable is not passed in.
  Its prompt carries a `[sandbox:podman /work]` marker, and its banner lists what is mounted.
- Sandbox hooks ([HOOKS.md](HOOKS.md)) can only add read-only mounts and pass variables.
  They cannot make anything writable, change the container's flags, or pass `LD_PRELOAD`.
  They cannot mount your home directory, or anything under `/proc`, `/sys`, `/dev` or `/run`.
  Search paths are filtered to directories mounted read-only, so the writable `/work` is never
  on `PATH` or `LD_LIBRARY_PATH`. The hook files must be owned by you and not writable by
  anyone else, and they are never mounted into the sandbox.

- **Sandboxed runs** (`run_in_sandbox`, only in coding mode, only with a profile's `sandbox`) are
  the one place hoplon executes model output without a person in between, so the container
  carries the safety instead:
  - It never runs where coding mode is refused (section 7), and `--coding-anywhere` doesn't
    change that: the overlay would copy your whole home directory, or a credential directory,
    into a container that runs commands without asking.
  - The project is a copy-on-write overlay (`-v DIR:/work:O`). Whatever the command writes,
    including `.git`, build files and test configuration, is discarded with the container, so it
    can't leave behind something you later run on the host. Real changes are only the edits and
    writes you approve.
  - Secret-looking files and directories (by name, or by contents, including the endpoint key)
    are covered by `/dev/null` or an empty read-only directory. A guard inside the container
    checks each of them is empty before the command starts, and the run is refused if one
    isn't, so a mount that silently failed doesn't expose it. (A `tmpfs` over a path inside the
    overlay was found not to hide it, which is why the guard exists.)
  - hoplon `--json` ignores SIGINT, except during a sandboxed run, which it stops (the container is
    killed and the turn ends `stopped`). The JSON `stop` message does the same, from whoever
    holds hoplon's stdin (hoplon-tui, here or over ssh, for Ctrl-C or `/stop`): it can only stop a
    run, never start or approve anything. An older hoplon without it gets a SIGINT, only from
    hoplon-tui to its own local child; a stray signal can't end the conversation.
  - `--network=none` and no proxy variables, unless you switch the network on for the session;
    then every run asks first.
  - No home directory, `/tmp` or `/run` from the host, so no SSH agent, tmux or hoplon-tui
    sockets. A read-only root, `--cap-drop=all`, `no-new-privileges`, `--userns=keep-id`, a time
    limit, a 1 GB `/tmp`, and memory and process limits where cgroups are delegated.
  - The command's output is masked and clipped before the model sees it, and the user sees the
    same output. The audit log records each run by fingerprint, with its exit code and duration.
  - Real samples (`/fixtures`) reach a run only through a folder outside the project, mounted
    read-only at `/fixtures`, after the user approves exactly what is in it. Secret-looking names,
    contents hoplon would redact, links and non-regular files block the approval. The approval is a
    digest of names, sizes and contents, checked again before every run; any change drops it. The
    model can only ask for samples (`request_samples` shows a card and ends its turn); it never
    chooses or reads a host path. What a run prints still goes to the model, which the approval
    question says. Adds, approvals, drops and clears are in the audit log.

- **Work sessions** (`:work`, or the JSON `work` message) let the model edit, test and commit
  without asking, so the boundary moves from each edit to the branch you review at the end:
  - The model works in a copy outside your project: a new git repository (`git init`) holding
    your last commit and nothing else, fetched with depth 1, so neither the model nor the sandbox
    can read your history, other branches or tags, including a secret that was committed once and
    removed. Your own files and branch are never written; you merge the branch yourself, with git.
  - hoplon reads your repository at the start: `git status`, in your own checkout with your own
    config, as you would run it, and read-only lookups of your last commit and branch names (and
    the fetch of that commit into the copy reads it too). It writes it in one place only: `git
    fetch` of each commit made in the copy into `refs/heads/hoplon/work-N`, without recursing into
    submodules or running maintenance. That fetch only adds to the branch: a branch moved or
    deleted meanwhile is an error, never overwritten, and so is one found changed at the end when
    nothing new is left to add; a name taken meanwhile moves to the next free one, and a branch
    named `hoplon` refuses the session. SIGTERM and SIGHUP wait for the fetch, so git is never
    killed halfway with a lock file left, and one that finished just as a signal came counts as
    done.
  - The copy's config is only what `git init` writes, so your repository's own config (filters,
    hooks, includes) never applies there; your global and system config does, so a filter driver
    set there still runs. A repository with Git LFS files is refused: the copy can't reach the
    LFS store. hoplon runs every git command itself, on the host, with `GIT_DIR` set to the
    copy's git directory, all `GIT_*` variables from the environment dropped, and
    `core.hooksPath=/dev/null`, `core.fsmonitor=false`, signing, external diff and textconv, and
    bare-repository discovery off. No file tool may write under `.git`, and the sandbox still runs
    on a throwaway overlay of the copy, so a test can't leave anything behind. The copy's git
    directory is beside its files, not among them, and the sandbox mounts only the files: the
    git directory holds every file of the commit, the ones the sandbox hides as secrets too.
  - Edits and writes apply without asking only while an item of an approved plan is worked on, or
    the planner reviews a stuck item (held to the same step budget and time limit); before
    approval they ask, whatever the edits mode:
    files you approved with `approved-files` count only outside the session. Hidden and
    secret-looking files always ask, and so does a `pyvenv.cfg` or anything under a directory of
    that name, as the sandbox's secret scan treats a virtual environment differently (it skips
    only its package directories, never the whole directory, and only for a real
    `pyvenv.cfg` file).
  - The sandbox still hides secrets and runs offline: a session can't start with the network on,
    the network can't be turned on during one, and a finish test checks again before it runs.
  - Only the tools offered at that point can be called: a call to any other is refused.
  - The session's state is its own: coding mode, if the session turned it on, and approved files
    go back to what they were when it ends, or when starting fails (`:edits reset` or `:edits ask`
    during it forgets the set-aside ones too). While it is on, the saved session keeps a copy of the
    conversation and coding mode from before it, so one resumed after a crash doesn't carry the
    session's. A planner on another endpoint starts from the task alone, without the earlier
    conversation or attachments queued in hoplon-tui or the REPL; every read question while it plans
    names the endpoint the file would go to; and when an item's context replaces the plan, the
    planner keeps its own context and is only told the new plan. `/clear` drops the planner's
    waiting context as well, and the saved conversation from before the session.
  - The user can stop the turn at any time (`stop`, Ctrl-C, `/stop`), including while the model
    writes tool calls or rollover notes, and end the session mid-turn (`work_end`), even while it
    is starting; quitting the front end, SIGTERM and SIGHUP end it too, committing what is there
    and removing the copy. If git fails while ending, or a file the model wrote is not in its
    commits (git ignores it, or it is inside a submodule's directory), the copy is not deleted;
    what is committed still goes to the branch. A hoplon killed outright (SIGKILL, a
    crash) leaves its copy in `~/.local/state/hoplon/work/`; your repository has whatever reached
    the branch. Once a plan, finish or retry takes effect, the rest of
    that reply's calls are not run: they were decided for the state before it.
  - A profile can plan with another model (`work.planner`). On the same endpoint, nothing new
    is trusted. A second endpoint must be blessed like any other, the profile's summary says it
    sees the project, and its key is masked in output too. It reads files, so whatever the
    profile's endpoint may see, it may see.
  - Each plan item has a step budget and the session a time limit without you, which also cuts
    off a sandboxed run or test that would outlast it. A stuck item goes back to the planning
    context once; after that, or at the time limit, it pauses. Every step,
    edit, test, commit and context swap is in the audit log.
  - What remains: the branch contains whatever the model wrote, including test files and build
    configuration, and nothing in it has been reviewed until you review it. Running its tests or
    build on your machine before reading the diff runs the model's code as you. Filter drivers
    from your global and system config still run when git checks out and commits the copy, and
    the model's `.gitattributes` can choose which files they get.

What remains: the indicator is display only. A command you approve outside a sandbox can do
anything you can. Inside `sandbox/run hoplon` the network is open, because hoplon has to reach
the endpoint, so a command run there can reach any host. It can also read whatever your user
can read under `/usr` and `/etc`, and under anything a sandbox hook mounted.

For sandboxed runs: a container shares the host's kernel, so it holds against a model that is
wrong or has been steered by what it read, not against a kernel exploit. Detecting secrets by name
and content is a heuristic; a secret in an unusual form in a non-hidden file can be read and sent
to the endpoint, as it could by `read_file` in coding mode. Virtual environments and
`node_modules` are not scanned. Overlay writes go to podman's storage and are not size-limited
until the run ends. Without delegated cgroups, only the time limit and `/tmp`'s size bound a run.

### 6. Improper output handling: the terminal as an attack surface

*LLM05; Embrace The Red, "Terminal DiLLMa"; Trail of Bits on ANSI codes.* A model can emit, or
echo from a file, escape sequences that move the cursor, overwrite earlier lines, hide text,
set the window title or write to the clipboard. General frameworks say little about this, but
it is the main way a terminal agent's approval screen gets falsified.

- Streamed replies, reasons, paths, diffs, file contents, notices, compaction summaries and
  error text from the endpoint pass through `exact()`. It works from Unicode categories, not a
  list: every control, format, private-use and surrogate character and every line or paragraph
  separator becomes a visible escape, along with variation selectors and the filler characters
  that render as nothing. That covers C0 and C1 controls, bidirectional overrides, zero-width
  characters and Unicode tag characters. A warning is printed when anything was escaped.
- Escaping is per character, so a sequence split across stream chunks cannot slip through.
- A value that belongs on one line (a reason, a path, a name, a URL) also has its newlines,
  returns and tabs shown as `\n`, `\r`, `\t`, so it cannot draw extra lines.
- File contents and diffs in an approval prompt carry a `    | ` margin on every line, so they
  cannot pass for hoplon's own prompts. hoplon's proposal headers are bold, and the model cannot
  produce bold.
- A proposed command that contains non-ASCII characters gets a line naming them, such as
  `U+0441 CYRILLIC SMALL LETTER ES`, so a look-alike letter in a host name or flag shows.
- The review screens for endpoints, profiles and skills, the listings and the banner are
  escaped the same way. An endpoint whose host name is not ASCII is shown with its punycode.
- In JSON mode text is JSON-encoded. Proposals carry `flagged`, and commands carry `non_ascii`,
  so a wrapper can warn.

What remains: look-alike letters are named for commands, not blocked, and not named in prose or
file contents. A wrapper has to do its own escaping.

### 7. Approval integrity and human trust

*ASI09, LLM06; Meta.* A human in the loop helps only if the prompt is truthful, the default is
safe and prompts are rare enough to be read.

- The default answer is no. End of input, an unknown proposal ID and a malformed decision all
  mean decline. Pending proposals never survive a crash or a resumed session.
- An approval is bound to what you reviewed: the target file's sha256, or the fact that it did
  not exist. If the file changed before the write, nothing is written.
- A wrapper's answer is bound to the proposal it shows. Each proposal that waits carries a
  digest of the event exactly as hoplon emitted it, keyed with a random value chosen per hoplon
  process. A decision must echo that digest, or it is refused and the proposal still waits. This
  catches an answer meant for another card, a queue race, or a stale card after a reconnect to a
  new hoplon process, whose IDs start again at `p1`. It does not stop a wrapper that means harm: a
  wrapper that can read the digest can approve anything, and it runs as you anyway.
- Every kind of proposal is one row of a table (`PROPOSAL_KINDS`), and `perform` runs a kind's
  handler only after an accept, except for commands (shown, never run) and kinds the wrapper
  answers itself. `tests/test_invariants.py` checks this, and the audit and end-of-input rules
  above, over every row.
- An edit that cannot be previewed is not applied.
- An overwrite that cannot be shown as a diff (the existing file is binary or too large) shows
  the whole new content instead, says the file is replaced entirely, and says when it will stay
  executable.
- Coding mode exists to cut prompt fatigue, and it removes only read prompts. Writes always
  ask (unless you opt into edits `approved-files`, below). It never applies to hidden files, is tied to the directory it was granted in, and is
  shown in the banner with where it came from.
- `"edits": "approved-files"` (opt-in, off by default) is the one exception to "writes always
  ask". After you approve an edit or write to a file, further ones to that same path apply
  without asking. The trade-off: text the model reads (a prompt injection in a file, a web page
  you shared, a tool's output) could then rewrite a file you approved once, without a question.
  What limits it:
  - It covers only files you approved by hand in this process. The set is in memory only, never
    saved with a session, so a resume or a new session starts empty; `:edits reset` or switching
    to `ask` empties it, and declining any edit to a file takes that file out.
  - New files, hidden files, a file or new text that looks like a secret (including the endpoint
    key), and a whole replacement of a file that can't be diffed always ask.
  - Each one is still shown with its diff, written to the audit log (`auto_edit`, and `file_op`
    with `approved_file`), and bound to the file's state when it was proposed, like any write.
    The model can't change a file's mode, so that can't happen behind it either.
  - It is independent of plans: approving a plan adds no files to the set (a planned edit you
    then approve counts like any other), and the set doesn't approve plan steps.
- Coding mode is refused in your home directory, in any directory that contains it (so `/`),
  in hoplon's own config directory and in the usual credential directories (`~/.ssh`, `~/.gnupg`,
  `~/.aws`, `~/.kube`, `~/.docker`, `~/.password-store`, `~/.config/gcloud`). A project
  directory under your home is fine. A resumed session is checked again.
- You can override the refusal with `--coding-anywhere`. Only the command line can: a profile,
  a saved session and the model cannot. hoplon then prints what the model can read without asking
  and which URL it goes to, and the banner, the `hello` event and the audit log record it.
- In coding mode a file that looks like it holds secrets is not read automatically. It asks,
  like any read outside coding mode. Automatic reads share the same bytes that were checked.
- A plan (`propose_plan`) lets you approve steps before they come, and is held to what you
  approved:
  - A planned read needs no further approval, unless the file is hidden or looks like it holds
    secrets.
  - A planned edit or write still shows its diff and asks. Otherwise an approved test command
    would run whatever the model had just written into the code it tests.
  - A planned command is marked `preapproved` only when it matches the approved step character
    for character; there are no patterns. hoplon still runs nothing: in hoplon-tui it is typed
    into your shell without another question only if you chose that for this plan, and
    hoplon-tui checks the command against its own record of what you approved, so it does not
    rely on hoplon's word alone. Its output still goes to the model only when you share it.
  - A command, edit or write the plan doesn't name is flagged "not in the plan" and asks as
    usual. A new plan needs a new approval.
  - A plan is never saved with a session, so it doesn't survive a resume. `/plan clear` (or
    `:plan clear`) drops it, and the audit log records the plan, each step it covered and its
    commands by fingerprint only.

The TUI (`tui/hoplon-tui`) is such a wrapper, split across tmux panes. Only its conversation pane
talks to hoplon; the prompt pane, the info panel and tmux menus reach it over a Unix socket in a
directory only you can open (`mktemp -d`, mode 0700, in `$XDG_RUNTIME_DIR` or `/tmp`, never a
shared filesystem; never an abstract socket, which has no permissions).

- A connection from another user is closed: the socket's directory keeps them out, and the
  kernel's peer credentials are checked as well.
- Each pane connects in a role and may send only that role's messages. The info panel can switch
  coding mode and blessed skills, as you could by typing; it cannot answer a question.
- An answer names the question it answers. One meant for a question that has gone, or not yet
  appeared, is ignored, and keys typed within 0.3 s of a question appearing count as typing.
- Messages are size-limited; a line that is too long or not JSON ends the connection.
- `hoplon-share` in the shell pane connects as `pipe` and can only queue output for your next
  message. It starts no turn and answers nothing: each share shows as a line in the conversation
  and in `/shared`, where you can take it back, and it reaches the model fenced as data, masked.
  The shell pane is told where the socket is (`HOPLON_TUI_STATE`), so a program you run there
  can queue a share too, which it could already do by finding the socket (see below).
- The file tree pane can only ask the conversation pane to list, view, attach or insert the path of a file.
  Listings and views go through hoplon's checks, and nothing goes to the model except an attachment you
  reviewed in your editor first.
- No text from the model goes into a tmux menu, pane title or popup command, where tmux would
  run a `#(...)`.
- The shell pane's output reaches the conversation pane through a FIFO in that same directory
  (tmux `pipe-pane`), and is kept in memory only, never written to disk. Commands are told apart
  by invisible markers the prompt hook prints, each carrying a random word the shell keeps in an
  unexported variable; a program you run can't read it, so it can't fake where a command ends or
  add one to the list. Nothing from the shell reaches the model until you say so: you review it
  in your editor first (`y`), or send it as it is (`s`), which shows nothing more than the
  question did. Either way hoplon masks the secrets it recognises, such as the endpoint key.

**Another host: `hoplon-ssh`.** In the shell pane, `hoplon-ssh [ssh options] HOST` connects with your
own ssh (your config, keys and agent; hoplon makes no connection) and starts bash there with the
same prompt hook, so hoplon-tui can follow commands on that host and `/run` can send one there. On
a shared login node other users can watch the process list, and anything on that host may print
text, so the session gets a key of its own and the key travels only where they can't see it:

- The local shell announces the session with a marker carrying its own word: a random id and the
  destination. hoplon-tui then makes a fresh 128-bit key, in memory. No program can announce one.
- On the other host, before your start-up files run, the shell turns the terminal's echo off and
  asks for the key with that id. Only the session just announced, only once, and only within
  20 seconds; any other ask is ignored. hoplon-tui types the key back through ssh's encrypted
  connection. With echo off it is not shown or recorded, and it is never on a command line, in
  the environment, in shell history or in a file. The remote bash keeps it in an unexported
  variable. (The id is on the command line for a moment while bash starts; it is not secret.)
- Remote prompts and command boundaries count only with that key, so a program run there can't
  fake them. When the local prompt comes back, the session and its key are forgotten.
- A command sent there asks first like any other, and the question says it runs ON ANOTHER HOST,
  naming it, in red; so does the info panel. Shared output says which host it came from. An
  automatic plan stops at a change of host, because the reviewed host and directory no longer
  match. Host names and directories reported from there are reduced to printable text.
- Plain `ssh` still works, but hoplon-tui can't follow it: `/run` refuses and says to use
  `hoplon-ssh`. Nothing is installed on the other host; it needs bash.

What remains: anyone who can read your processes' memory on that host (root, or a program
running as you there) can read the key, as on this one, and could then fake that session's
command list. The key only ever lets a program fake markers; it approves nothing.

What this cannot do is keep out a program running as you: it can connect to the socket, or send
keys through tmux, exactly as you can. The shell pane runs as you, on the host unless hoplon-tui
runs in the sandbox, so a harmful command you run there could approve further proposals. The
TUI requires a full command preview before execution, and checks that the shell pane
still matches the reviewed working directory. Automatic plans require a full preview
and reserve each approved command step for a single dispatch. Shell dispatch uses
tmux keystrokes, so avoid typing in the shell pane while the TUI is dispatching.

What remains: for commands, you are the last control. That is the design, and it is why the
other sections matter. With `--coding-anywhere` in your home directory, everything there that is
not hidden is one injected instruction away from the endpoint: reads no longer ask, and a file
the model reads can tell it what to read next.

#### Remote hoplon

hoplon-tui's remote mode runs hoplon on another host over ssh with `--json --config-stdin`. The API key never
leaves the user's machine. Over ssh go the endpoint and profile (without any key) and a per-session token.
hoplon there sends its requests to `http://127.0.0.1:PORT`, which ssh's reverse tunnel carries back to a proxy
on the user's machine; the proxy requires the token (`X-Hoplon-Token`) and adds the key. hoplon refuses `key_env`,
`key_file` and `proxy` in this mode, and any `base_url` that is not `http://127.0.0.1:PORT`.

The config is trusted because it arrived over the user's own ssh channel: hoplon-tui checked the user's blessed
files before sending, and hoplon blesses nothing on the remote host for it. It logs a `remote_config` event with
the hashes of what it received, never the token.

Skills and command sets follow the same rule. The ones in use go in the config line with every file of their
directories, and only if they are blessed on the user's machine. hoplon on the host hashes those bytes again and
refuses the whole config if a hash differs from the one blessed there. It builds the skill or command set from those
bytes in memory and never reads or writes this host's copies. Nothing blessed only on the remote host can be
turned on, so whoever controls that host's `~/.config/hoplon` can't add a skill to the prompt or a scoped command
to the model's tools. A command set's programs are looked for on the host, with the usual checks (its fixed search
path, never `PATH`; owned by the user or root, not writable by others), so the Slurm command set's commands run on the
cluster. The program that runs is whatever is installed there, and a command whose program is missing there is
left out. Root on the remote host can use the session's token while the
tunnel is up, and can read that session's prompts, replies and files, which are on that host anyway. It cannot
read the key, which was never sent. Everything else (coding-mode refusals, the sandbox, the audit log, sessions)
works for the user hoplon runs as. As root, the REPL banner warns that proposed commands would run as root.

How hoplon-tui sets it up (`hoplon-tui --remote`), and what each part is for:

- The launcher runs `hoplon --remote-config` here: the endpoint, profile, skills and command sets are checked and blessed on your machine,
  and exported without `key_env`, `key_file`, `proxy`, `headers` or `ca_bundle`, so neither the key nor a secret in a
  header goes over the connection. Only the remote half of the config lacks them: `tui/keyproxy.py`, here, uses the
  endpoint's own proxy and headers.
- ssh runs as a master connection started in your terminal, with `-a -x` (no agent, no X11 forwarding). Its control socket
  is in hoplon-tui's state directory (mode 0700, in `$XDG_RUNTIME_DIR` or `/tmp`), where only you can reach it: whoever
  can use that socket can open sessions as you on the host. Use ProxyJump (`-J`) for a bastion rather than `-A`, which
  would let root on the host use your agent. The master, the key proxy and the state directory go when the launcher
  exits (also on Ctrl-C and a closed terminal).
- The key proxy listens on a Unix socket in the same directory and `ssh -O forward -R 127.0.0.1:0:SOCKET` makes a
  loopback port on the host lead to it. Its token (32 random bytes) is in memory, in the launcher's 0600
  `remote.json`, on the proxy's stdin and in hoplon's config line; never in a command line. The remote port can be
  reached by every user of that host, so the token is the only protection: without it the proxy answers 401, and
  only the two allowed requests are ever forwarded (see `tui/keyproxy.py`).
- hoplon's source goes to the host on stdin, not as a file: the remote command is a fixed Python bootstrap that reads
  exactly N bytes and executes them, so the host can't have a different or older hoplon than the one you have here.
- The shell pane is `hoplon-ssh` over the same master, with the same handshake and key as always (see 7); with `--as`,
  `sudo -n -H -u USER` starts its bash, and `--dir` is where it starts. `--as` takes only a user that `sudo -n`
  accepts without a password, which the launcher checks first.

Root on the remote host can, while the session is up, use the token to send chat and model-list requests to your
endpoint (spending your quota, within the proxy's allowlist), read everything that session sends, receives and
reads on that host. It cannot get the key, other URLs of the endpoint, or the
proxy once hoplon-tui has exited. A remote root could also change what hoplon does there: it is your hoplon's code,
but running on a host you don't control, so approvals still happen on your screen and commands still run only
where you send them, but what the model is *shown* from that host (file contents, hello, the audit log's path) is
only as honest as the host.

### 8. Supply chain and configuration trust

*LLM03, ASI04, AML.T0081, AML.T0104, AML.T0110; Invariant Labs on tool poisoning.* Recent
attacks on coding agents came through files the agent trusted by location: project settings
that started processes or redirected the API key before the user agreed to anything, and tool
descriptions that changed after approval.

- hoplon reads no configuration from the working directory. A cloned repository cannot change
  its endpoint, tools or behaviour. Configuration comes only from `~/.config/hoplon`.
- Nothing is sent anywhere until an endpoint has been blessed. Blessing needs a terminal. The one
  exception is `hoplon --new-endpoint`, which lists the models at the address you just typed only if
  you say yes, after it says whether your key goes with the request.
- Blessing records a sha256 of exactly the bytes shown. Each use reads the file once and
  hashes and parses the same bytes. Skills are hashed across every file, with symlinks
  refused. Any change means refusal until you bless again.
- hoplon's own state (`config.json`, the blessings, sessions, the audit log) must be yours and
  not writable by other users, and so must every directory above it, up to one owned by root.
  A sticky directory such as `/tmp` may hold it. Group write counts as safe only when the group
  is your personal one (named after you, with no other members), as with `umask 002` on
  user-private-group systems. State files must be regular files, not symlinks. A first run
  creates any missing parent, such as `~/.config`, private. Each refusal says which `chmod`
  fixes it.
- Blessing a skill scans every file for hidden and control characters and lists what it found
  (`U+E0049 x3`), and says which files are binary and so could not be checked. A skill's text
  goes to the model, so text you cannot see is the risk.
- Unknown fields in an endpoint or profile are errors. `params` cannot replace `model`,
  `messages` or `tools`.
- A skill is prompt text only. It cannot add a tool or turn on coding mode. Extra tools can be
  registered only by a wrapper over JSON, never from a file.
- hoplon is one file with no dependencies outside the Python standard library.
- Hooks ([HOOKS.md](HOOKS.md)) are programs of yours that run without asking each time, so
  they are kept narrow. A hook is never read from the working directory. The notify hook is a single
  file in `~/.config/hoplon/hooks`, blessed like an endpoint, and checked before every run: a
  regular file, owned by you, not writable by others, with the blessed hash. It is given the
  name of the event and a count. It never gets file contents, proposal paths, command text, model
  text or the key, and its output is discarded, so nothing the model writes reaches it and nothing
  it writes reaches your terminal or the model.
- No hook can approve a proposal, add a tool, or run when a session starts. Those are how
  project-level hooks became an attack path in other agents.

What remains: a hook is your code and runs as you, outside any sandbox. The notify hook's hash
is checked just before it runs, and it is then run by path, so a change in that instant would
not be caught. Only something already running as you could make that change.

### 9. Memory and context poisoning

*ASI06, AML.T0080; Microsoft's failure-mode taxonomy.* Whatever an agent remembers can carry an
injected instruction into later sessions.

- hoplon keeps no memory on its own. A conversation is saved only when you name a session with
  `-S`, in a 0600 file.
- Compaction is the one place the model writes its own context. The summary is shown to you,
  `:compact -e` lets you edit it first, and the full history stays in the session file.
- A resumed session that had coding mode on for another directory comes back with it off.

### 10. Sensitive information disclosure

*LLM02, LLM07; NIST AI 600-1 (information security, data privacy).*

- Command output is masked for common secret formats and for the endpoint key's exact value,
  clipped, and opened in your editor before anything is sent. Colour codes are stripped and
  hidden characters are shown as escapes, with a header line saying so. What you save is sent
  exactly, and an empty buffer sends nothing.
- Files are sent as they are, deliberately, because masking would corrupt code. hoplon warns
  when a file looks like it holds secrets.
- A wrapper's file viewer (the `read` message) reads through the same checks as the model's reads.
  The text goes only to the wrapper, never to the model or the session, with the endpoint key
  masked, and each read is audit-logged with its path and size, not its content.
- A failed edit on a file the model may not read reports only "declined", so edits cannot be
  used to probe file contents. The same holds for paths: where the model may not read without
  asking, an error that would reveal a symlink or a file in the middle of a path is shown to you
  and reported to the model as "declined".
- The audit log stores a command's hash and length, never its text. Config, sessions and the
  audit log are 0600 inside a 0700 directory, and temporary files are 0600.
- The system prompt contains no secrets, so leaking it reveals nothing.

What remains: masking is pattern matching and will miss secrets it has no pattern for.
Sessions are plain text on disk.

### 11. Unbounded consumption

*LLM10, ASI08.* The step limit, the size caps, a per-request timeout and a context check before
every send (at the limit, nothing is sent) bound what a looping model can cost. A sandboxed run
has a time limit, and memory and process limits where the host allows them. A reply from the
endpoint is capped at 8 MB, as is a single streamed line; past that the request fails and
nothing is performed. The masking patterns run in linear time, so crafted command output cannot
stall hoplon.

### 12. Logging and accountability

*NCSC guidance; LLM06.* `~/.config/hoplon/audit.log` records, one JSON object per line: session
start, every proposal and decision, every file operation and its outcome, each `:run` and its
exit status, each time output was shared, blessings, coding-mode changes, tool registrations
and compactions. It records what was proposed and decided, not file contents or command text.

### 13. Transport

HTTPS is required for anything but loopback unless the endpoint sets `allow_http`, which
blessing flags. Certificates are verified with the system store or a named `ca_bundle`. Because
the model cannot cause a request, there is no server-side request forgery surface.

### Not applicable

Inter-agent trust and rogue agents (*ASI07, ASI10*) and vector-store weaknesses (*LLM08*): hoplon
is a single agent with no retrieval store.

## Findings from the 2026-11-21 review

Each finding was confirmed by running the code, and each fix was checked the same way and has
tests. None broke the core rule: in every case a person still had to approve something. They
weakened how truthful or complete that approval was.

| # | Severity | Finding | Now |
| --- | --- | --- | --- |
| 1 | Medium | **The endpoint review screen was not escaped.** `--bless` printed an endpoint's `name`, `base_url`, `model`, `key_file` and `proxy` raw, so a file from someone else could redraw the URL line. | Fixed. Every field is escaped to one line, with a warning when anything was escaped. Listings and skill file names too. See 6. |
| 2 | Medium | **Overwriting a non-text file was approved blind.** The prompt showed only "cannot diff the existing file", never the new content, and an executable stayed executable. | Fixed. The whole new content is shown, with the mode it keeps. See 7. |
| 3 | Medium | **`exact()` missed several invisible characters:** Unicode tag characters, line and paragraph separators, variation selectors, invisible operators, fillers. Look-alike letters were not addressed. | Fixed for hidden characters, by Unicode category. Look-alike letters are named for commands only. See 6. |
| 4 | Low | **Endpoint error text reached the terminal raw.** | Fixed. |
| 5 | Low | **A proposal's fields could imitate hoplon's prompts,** through newlines in `reason` and `path` and content shown without a margin. | Fixed. One-line fields and margins. See 6. |
| 6 | Low | **Hidden text reached the model unseen,** in skills and in output reviewed in the editor. | Fixed. Blessing a skill lists hidden characters, and the editor buffer shows them as escapes. See 8 and 10. |
| 7 | Low | **Coding mode's location check was narrow,** and a file that tripped the secret detector was still read automatically. | Fixed. See 7. |
| 8 | Low | **hoplon did not mask its own key.** | Fixed. Masked by exact value. See 4. |
| 9 | Low | **Masking was quadratic on some input.** 32,000 characters of `a.a.a.` took about 23 seconds. | Fixed. 256 KB of the same input takes under a tenth of a second. |
| 10 | Low | **Sandbox hardening.** Open network in `sandbox/run hoplon`, and a base image pinned by tag on a Python that had reached end of life. | Partly. There is no image any more: the sandbox uses this machine's OS. The network is still open where hoplon needs the endpoint. See 5. |

Smaller notes from the review: the path probe and the uncapped reply size are fixed (see 10 and
11), and DECISIONS.md is corrected about what part of a skill is sent.

Still open:

- `sandbox/run hoplon` and `sandbox/run live` have an open network.
- The audit log is not tamper-evident. Anything running as you can edit it.
- Files you attach with `-f` and files the model reads are sent unmasked, by design.

## What stays your job

- Read a proposed command before running it, and a proposed diff before accepting it.
- Bless an endpoint only after checking its URL and key source in the file itself.
- Run coding mode only in a project directory you would share with the endpoint, and use
  `--coding-anywhere` only when you have read what it says it exposes.
- Use a sandbox for commands you would not run as yourself.
- Read the host in a run question before you answer it: with `hoplon-ssh` it may be another one.
- Keep secrets out of skills and out of files you attach.

## Reporting a problem

Report a suspected vulnerability privately to the maintainer, not in a public issue. Include
the hoplon version (`hoplon --version`), what you did and what you saw.

## Sources

Standards and frameworks

- OWASP Top 10 for LLM Applications 2025: <https://genai.owasp.org/llm-top-10/>
- OWASP Top 10 for Agentic Applications 2026 (announced 9 December 2025):
  <https://genai.owasp.org/2025/12/09/owasp-top-10-for-agentic-applications-the-benchmark-for-agentic-security-in-the-age-of-autonomous-ai/>
- OWASP, Agentic AI: Threats and Mitigations, v1.0, February 2025:
  <https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/>
- NIST AI 100-2e2025, Adversarial Machine Learning: A Taxonomy and Terminology of Attacks and
  Mitigations: <https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-2e2025.pdf>
- NIST AI 600-1, AI Risk Management Framework: Generative AI Profile, July 2024:
  <https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf>
- NIST CAISI, Strengthening AI Agent Hijacking Evaluations, January 2025:
  <https://www.nist.gov/news-events/news/2025/01/technical-blog-strengthening-ai-agent-hijacking-evaluations>
- MITRE ATLAS, data version 5.6.0: <https://github.com/mitre-atlas/atlas-data>

Government guidance

- UK NCSC, Prompt injection is not SQL injection (it may be worse), December 2025:
  <https://www.ncsc.gov.uk/blog-post/prompt-injection-is-not-sql-injection>
- UK NCSC and CISA, Guidelines for secure AI system development, November 2023:
  <https://www.ncsc.gov.uk/collection/guidelines-secure-ai-system-development>
- CISA and partner agencies, Careful Adoption of Agentic AI Services, May 2026:
  <https://www.cisa.gov/resources-tools/resources/careful-adoption-agentic-ai-services>

Vendor and research

- OpenAI, Designing agents to resist prompt injection, March 2026:
  <https://openai.com/index/designing-agents-to-resist-prompt-injection/>
- Google, Mitigating prompt injection attacks with a layered defense strategy, June 2025:
  <https://blog.google/security/mitigating-prompt-injection-attacks/>
- Microsoft AI Red Team, Taxonomy of failure modes in agentic AI systems, April 2025:
  <https://www.microsoft.com/en-us/security/blog/2025/04/24/new-whitepaper-outlines-the-taxonomy-of-failure-modes-in-ai-agents/>
- Meta, Agents Rule of Two, October 2025: <https://ai.meta.com/blog/practical-ai-agent-security/>
- Simon Willison, The lethal trifecta, June 2025:
  <https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/>
- Beurer-Kellner et al., Design Patterns for Securing LLM Agents against Prompt Injections,
  2025: <https://arxiv.org/abs/2506.08837>
- Debenedetti et al., Defeating Prompt Injections by Design (CaMeL), 2025:
  <https://arxiv.org/abs/2503.18813>
- Embrace The Red, Terminal DiLLMa, December 2024:
  <https://embracethered.com/blog/posts/2024/terminal-dillmas-prompt-injection-ansi-sequences>
- Trail of Bits, Deceiving users with ANSI terminal codes in MCP, April 2025:
  <https://blog.trailofbits.com/2025/04/29/deceiving-users-with-ansi-terminal-codes-in-mcp/>
- Invariant Labs, MCP tool poisoning attacks, April 2025:
  <https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks>
