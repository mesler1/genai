# Design decisions and known limitations

genai's core rule is **the model proposes, the human acts**. The model has five intrinsic tools,
none of which runs anything: it can propose a command or a file operation, and genai performs a
file operation only after you accept it. Sandboxing is left to whatever wraps genai.
[SECURITY.md](SECURITY.md) maps these choices to the security concerns they answer.

The two riskiest parts, the path check and the JSON proposal/decision round trip, are covered by
tests:

- **Path escapes:** `..`, absolute paths, symlinked files, symlinked directories mid-path, FIFOs
  and binaries.
- **Proposals:** batch decisions, unknown IDs, EOF means decline, and resuming after a crash.

## Behaviour

| Topic | Decision |
| --- | --- |
| Coding mode without a terminal | Outside JSON mode with no terminal, nothing is performed, not even coding-mode reads. The model is told the run is non-interactive, so it answers in plain text. That is what makes `genai ... > script.sh` work. |
| Coding mode and the working directory | The grant covers "files in this directory". The session stores its directory, and resuming it somewhere else turns coding mode off with a notice. |
| Where coding mode is refused | Your home directory, any directory that contains it (so `/`), genai's config directory, and `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.docker`, `~/.password-store`, `~/.config/gcloud` and anything under them. Paths are compared after resolving symlinks, including a credential directory that is itself a link elsewhere. A project directory under your home is fine. From a profile or a resumed session it is turned off with a notice instead of failing. |
| Overriding the refusal | `--coding-anywhere` is `--coding` that also works in those places. It is a command-line flag only: a profile can't grant it, there is no REPL command or config setting for it, and a resumed session needs it again. Each time, genai prints what the model can now read without asking and where it goes, shows it in the banner, puts `coding_override` in the `hello` event and records it in the audit log. Hidden files, secret-looking files and writes still ask. |
| Secret-looking files in coding mode | A file that trips the secret detector is not read automatically. It becomes an ordinary read proposal (with `"secret": true` in JSON), so you decide. |
| Path errors the model may not see | Where the model can't read without asking, an error that would reveal what is on disk (a symlink, or a file where a directory was expected) is shown to you, and the model gets "declined by the user". |
| Proxy variables | Ignored. genai builds its own opener and never reads `https_proxy`. An endpoint can set an explicit `"proxy"`, which blessing shows. |
| Key sources | `key_env` or `key_file`. A key file must be mode 0600 and owned by you. With neither, no `Authorization` header is sent, which suits loopback test servers. Blessing shows "key from: none". |
| Skill format | A directory with `SKILL.md`. The whole `SKILL.md` is sent with every request; its first line is the description shown in listings. Every file in the directory is hashed. |
| Hidden characters in a skill | `--bless` scans each file and lists the hidden and control characters it finds by code point, before it asks. A binary file is named as not checked. Blessing is not refused: you decide. |
| `-S` | Takes a name: `-S nightly` creates the session or resumes it. |
| Shared endpoints | A blessed endpoint, profile or skill can be found by its `name` wherever it lives. |
| Endpoint fields | Unknown fields are errors. Credentials in `headers` are refused. `params` may not override `model`, `messages` or `tools`. |
| REPL input | `..` on a line by itself sends, and so does Ctrl-D. A `:` command works on an empty buffer. |
| `:run` and `:sh` output | One editor buffer holds everything that will be sent: where it ran, the command as run, and the output (redacted and clipped). Colour codes are stripped, and hidden and control characters are shown as escapes and sent that way, with a header line saying so. What you save is sent exactly, with nothing added, so you can remove a secret from the command as well as from the output. An empty buffer sends nothing. |
| Masking the endpoint key | Besides the patterns, the key's exact value is masked as `[REDACTED endpoint key]` in command output, JSON `attach` text and wrapper tool results. Values under 8 characters are not masked by value. `-f` files and `read_file` are still sent as they are. |
| Masking speed | Every pattern runs in linear time: the name after a keyword is bounded at 64 characters, a URL scheme at 32, and a private-key block is only looked for up to the last END line. |
| Edits the model may not read | If an edit can't be previewed (text not found or ambiguous, file missing or binary) and the model isn't allowed to read the file without asking, you see why locally. The model only gets "declined by the user", so it learns nothing about the file's contents. |
| Approval and the file's state | Each write or edit is bound to the target's state when you reviewed it: its sha256, or that it didn't exist. If the file changed before it's applied, nothing is written and the model is told to propose again. In a batch of two writes to one file, the second is refused. |
| Blessed files are read once | An endpoint or profile is hashed and parsed from the same bytes. A skill is read into memory when it's loaded, and only that copy is used, so editing it mid-session changes nothing until it's reloaded. `--bless` records the hash of exactly what it showed you. |
| Model text on the terminal | Streamed replies, reasons, paths, notes, summaries and endpoint error text are escaped before display, with a warning on stderr. What gets escaped is decided by Unicode category (controls, format characters, private use, surrogates, line and paragraph separators) plus variation selectors and invisible fillers. U+FE0F is left alone, since it follows ordinary emoji. JSON mode is JSON-encoded instead, and `--json` listings carry values as they are. |
| One-line fields | A reason, path, name, URL or model name also has newlines, returns and tabs shown as `\n`, `\r`, `\t`, so one field can't draw several lines. The review screens for endpoints and profiles do the same and warn when anything was escaped. |
| Margins | File content and diffs in an approval prompt have `    \| ` in front of every line, so they can't be laid out to look like genai's prompts. |
| Look-alike letters | Not blocked. A proposed command with non-ASCII characters gets a line naming them (`non_ascii` in JSON), and an endpoint with a non-ASCII host is shown with its punycode. |
| Overwrites that can't be diffed | If the existing file is binary or too large to diff, the prompt shows the whole new content and the mode the file keeps, and says so when it stays executable. In JSON the proposal carries `undiffable` and `mode`. |
| Reply size | One reply from the endpoint, and one streamed line, is capped at 8 MB. Past that the request fails with exit code 4 and nothing is performed. |
| Commands in the audit log | Recorded as `command_sha256` and `command_len`, never as text, since a command you edited may hold a secret. |
| `key_env` | Must be a plain variable name (`[A-Za-z_][A-Za-z0-9_]*`), in genai and in `sandbox/run`, which uses it in bash indirect expansion. |
| Context full | The new message is not kept. Compact, then send again. |
| Compaction | Prunes bulk older than the last 2 user turns, and summarizes only if still above 80%. The summary goes in front of the first kept message. The full history stays in the session file. |
| JSON while proposals are pending | `attach` is queued. Anything else gets an error with code `busy`. |
| Coding mode over JSON | Only the `--coding` flag. There is no message for it. |
| `write_file` into a missing directory | Creates the directory, inside the working directory only. |
| Work sessions | Git only: no repository, no session, and genai never makes one. The copy is a new repository holding only your last commit (fetched with depth 1), with its git directory beside its files so the sandbox, which mounts only the files, can't read the commit's objects; uncommitted changes to tracked files refuse it. genai writes your repository only by fetching each item's commit into `genai/work-N`. The sandbox keeps its throwaway overlay of the copy rather than writing to it, so only genai's file tools change the copy: formatter or code-generator output from a sandboxed run doesn't persist. You merge the branch yourself; genai never touches your own branch. Limits are per plan item (model steps) plus a time limit without you: a message from you, or your approval of a plan, renews both; finishing an item renews only the steps. Stopping is the user's at any point: `stop` ends the turn and pauses the session, `work_end` ends it mid-turn, and quitting the front end or a SIGTERM ends it cleanly; on any problem at the end (a git failure, or a file the model wrote that isn't in its commits) the copy stays, and what is committed still goes to the branch. Each item runs in a fresh context, with a handoff: the task, the plan, and notes earlier items left in `finish_item` (also kept in the commit body). A stuck item goes back to the planner's context once, which can finish it, retry it with a hint, or replan. Summarising compaction is only the fallback, for one item that fills the context. The planner may be another model (`work.planner`: `model`, `endpoint` or both), named in the profile, not chosen per session: it receives the project, so a planner endpoint must be blessed like any other. A model the server doesn't list refuses the session, so a typo doesn't cost a long run. Sampling settings in the profile's `params` apply to the profile's own endpoint only. There is no memory across sessions: lasting notes belong in a file in the repository, reviewed with the diff. A review screen in genai-tui comes later. |
| Config location | `$XDG_CONFIG_HOME/genai`, default `~/.config/genai`. Created on first run (0700), with a starter `config.json` listing every setting. Existing files are left alone. The sandbox uses this to keep its own config. |
| Sandbox without an image | `sandbox/run` builds no image. The container's root is an empty tree of mount points, and the host's `/usr` and `/etc` are mounted into it read-only, so it runs this machine's own Python and editor. Nothing is pulled, and there is no base image to keep patched. `/home`, `/root`, `/var`, `/run`, `/mnt`, `/media` and `/opt` are not there. `GENAI_SANDBOX_RO` adds more read-only host directories; it refuses your home directory. |

## Hooks

[HOOKS.md](HOOKS.md) is the full design. The decisions behind it:

| Topic | Decision |
| --- | --- |
| What a hook may do | Observe, or set up the sandbox's environment. A hook cannot approve a proposal, add a tool, run something at start, or loosen the sandbox. Those belong in a wrapper over `--json`, where they are plainly the wrapper author's choice. |
| Where hooks live | Only in directories you own: `~/.config/genai/hooks` and the sandbox's state directory. Never the working directory, so a cloned repository can't bring one. |
| No code loaded into genai | A hook is a separate program with a small fixed input and a small fixed set of answers. genai stays one file with no plugins. |
| Sandbox hooks | Programs in `~/.local/share/genai-sandbox/hooks`, run by `sandbox/run` before the container starts. They can ask for three things: `ro DIR`, `path NAME`, `env NAME`. Their output is parsed line by line, never evaluated, and an unknown line stops the sandbox from starting. |
| Search paths from a hook | Only entries that are absolute and lie under a read-only mount are kept. Empty and relative entries are dropped, because to the loader they mean the current directory, and in the sandbox that is the writable `/work`. |
| Variables a hook may not pass | `HOME`, `XDG_CONFIG_HOME`, `GENAI_SANDBOX`, `PATH` and `PYTHONPATH` (except filtered, through `path`), and every `LD_` name except `LD_LIBRARY_PATH` through `path`. So `LD_PRELOAD` can't be passed. |
| Notify hook | One file, `~/.config/genai/hooks/notify`, blessed like an endpoint and checked before each run. It is told that genai is waiting (`approval`, `command`, `reply`) and nothing about the content. Its output is discarded, genai doesn't wait for it, and it is stopped after 10 seconds while genai is still running. Off in `--json` mode and when output is piped. |
| Mounts a hook can't ask for | Besides the `GENAI_SANDBOX_RO` rules, nothing under `/proc`, `/sys`, `/dev` or `/run` is mounted, for a hook or for `GENAI_SANDBOX_RO`. A socket there would work through a read-only mount. |
| Hooks that fail | A sandbox hook that fails, or asks for something refused by name, stops the sandbox. A notify hook that fails a check is not run, genai says so once, and carries on. |

## Endpoints and profiles

- An **endpoint** says where prompts go: URL, model, key source, context size, request `params`.
- A **profile** is a persona on top. It can have an endpoint name, persona text, default skills,
  `params`, and `"coding": true`. `-e` picks an endpoint and overrides the profile's; `-p` picks a
  profile.
- The persona text follows genai's own system prompt, introduced as not changing those rules.
- A profile's `skills` replaces `default_skills`, and `-k` adds more. Its `params` override the
  endpoint's.
- A profile may grant coding mode. Blessing, the REPL banner and the `hello` event all show it.
- Profiles are blessed like endpoints and skills. Blessing shows the persona text with hidden
  characters escaped.
- Sessions record both names. A session saved before endpoints and profiles were split is read with
  its old `profile` as the endpoint.

## Sampling settings

An endpoint should carry the model maker's recommended sampling settings: the model card's values
for the mode you use, such as thinking or non-thinking. Don't guess at them. A profile overrides
them only for a stated reason, so the bundled `coder` sets none. The live test's persona check alone
uses temperature 0, to get a repeatable answer.

## Sandbox indicator

genai reports whether commands run inside or outside a sandbox:

- the REPL banner;
- the `:run` confirmation and result line;
- the text shared back to the model;
- the `hello` event;
- the audit log.

This is display only. It comes from `GENAI_SANDBOX`, or from podman's or docker's marker file, and is
not a guarantee.

## Known limitations

1. **Size.** `genai` is about 2,700 lines. The JSON protocol, the REPL with `:run`/`:sh`, compaction
   and blessing each take 200 to 300.
2. **`:sh` capture needs `script`** (util-linux), which is not in the standard library. If it's
   missing, genai opens the subshell without capture.
3. **Editor leftovers.** Only vi, vim and nvim get flags that turn off swap and backup files. Other
   editors may leave copies of the output on disk.
4. **What you see vs what runs.** Proposals are shown escaped. The `:run` editor shows the raw text,
   so a flagged command also prints a warning. The editor itself could still render hidden characters.
5. **SELinux.** `sandbox/run` doesn't relabel mounts, and it has not been run on an SELinux host. On
   one, such as RHEL, the work directory may need `:z` and the host mounts a label policy.
6. **Apply-time race.** The state check runs just before the write's rename. An editor saving in
   that instant can still be overwritten; closing that fully would need file locking.
7. **Live test keys.** `sandbox/run live` passes in only a `key_env` variable. It can't test an
   endpoint that uses `key_file`.
8. **Sandbox Python.** The sandbox runs the host's Python, so the suite is no longer run on 3.10, the
   minimum, unless the host has it: `GENAI_SANDBOX_PYTHON=python3.10 sandbox/run tests`.
9. **Sandbox reads.** Inside the sandbox, anything your user can read under `/usr` and `/etc` is
   readable, and `sandbox/run genai` has an open network because genai must reach the endpoint.
10. **Hooks and podman's own `PATH`.** When a sandbox hook passes `PATH`, podman itself runs with
    the filtered value. If podman's helpers (`crun`, `conmon`) are outside every mounted directory,
    the sandbox fails to start.
11. **Notify hook after genai exits.** The 10-second limit is enforced by genai, so a hook still
    running when a one-shot run exits is left to finish.
12. **Modules hook.** Tried with a made-up module tree, not yet on a real Lmod or EasyBuild system.
