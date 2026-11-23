# genai

A small command-line client for OpenAI-compatible APIs, standard library only, Python 3.10+.
**The model never executes anything:** it proposes a command or a file operation, and you decide.
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
| Sessions | `genai -p NAME -S nightly "summarize" > out.txt` |
| Coding mode | `genai -e NAME --coding` (reads stop asking, except files that look like secrets; writes still ask) |
| Coding mode where it is refused | `genai -e NAME --coding-anywhere` (prints what that exposes) |
| Wrappers | `genai -p NAME --json` (JSON Lines over stdio) |
| Lists | `genai --list-endpoints`, `--list-profiles`, `--list-skills`, `--list-sessions` (each with `--json`) |

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
sandbox/run                podman sandbox for running genai and its tests, on this machine's OS
sandbox/hooks/modules.sh   sandbox hook: makes loaded environment modules work inside the sandbox
hooks/notify-desktop       example notify hook: a desktop notification when genai waits for you
tests/                     offline suite, live endpoint test, harness self-check
```

## Configuration

On first run genai creates `~/.config/genai` (or `$XDG_CONFIG_HOME/genai`), mode 0700, with
`endpoints/`, `profiles/`, `skills/`, `sessions/` and a `config.json` that lists every setting at its
default. It says so once on stderr and never changes what is already there.

| Setting | Default | Meaning |
| --- | --- | --- |
| `default_profile` | `null` | Profile used when `-p` is not given. |
| `default_endpoint` | `null` | Endpoint used when neither `-e` nor the profile names one. |
| `default_skills` | `[]` | Skills used when the profile doesn't list its own. |
| `endpoint_dirs` | `[]` | Extra directories searched for endpoints, after `~/.config/genai/endpoints/`. |
| `profile_dirs` | `[]` | Extra directories searched for profiles, after `~/.config/genai/profiles/`. |
| `skill_dirs` | `[]` | Extra directories searched for skills, after `~/.config/genai/skills/`. |

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

When a saved session's endpoint destination changes, or an older session has no recorded
destination, genai requires `--allow-session-endpoint-change` before sending the saved
conversation. Check the URL and model shown in the error before approving the transfer.

## JSON protocol

`genai --json` speaks JSON Lines over stdin and stdout: one object per line, each with a
`type`. A wrapper is the user interface. It shows proposals and sends back decisions, and genai
still enforces its own rules: blessing, coding-mode refusals, masking. Text from the model
arrives as it is, so the wrapper must escape it before it displays it.

genai's first line is `hello`. It has `protocol` (1), `version`, `features`, `endpoint`,
`profile`, `session`, `resumed`, `coding`, `coding_from`, `coding_override`, `cwd`, `sandbox`,
the active `skills`, and the model's `tools`. Check `features` before you use a message added
later.

**Messages a wrapper sends**

| `type` | Fields | What happens |
|---|---|---|
| `user` | `text` | Sends a message to the model, with any queued attachments. |
| `attach` | `text`, optional `label`, `ref` | Queues text for the next `user` message. Secrets in it are masked. Answered by `attached`. |
| `decision` | `id`, `decision` (`accept` or `decline`); for a plan, optional `approve` and `edits` | Decides a read, list, write, edit or plan proposal. For a plan, `approve` lists the step numbers approved (default: all), and `edits` maps a command step's number to the command the user wants instead. A malformed choice declines the plan, with the error `bad_message`. |
| `result` | `id`, `status` (`ok`, `declined`, `error`), `output` | Answers a proposal for a tool the wrapper registered. |
| `register_tools` | `tools`: a list of `{name, description, parameters}` | Adds tools the model can call. The wrapper performs them. Answered by `registered`. |
| `compact` | | Shrinks the conversation. Answered by `compacted`. |
| `coding` | `on` (true or false) | Turns coding mode on or off. The same places are refused as for `--coding` (error `coding_refused`), and `--coding-anywhere` stays a command-line flag. Always answered by a `coding` event with the state now. |
| `skills` | | Answered by `skills`: every skill, its blessing `status`, and whether it is `active`. |
| `skill` | `name`, `on` (true or false) | Uses a skill from the next request on, or stops using it. Only a blessed skill can be turned on (errors `unknown_skill`, `not_blessed`). Always answered by a fresh `skills` list. |
| `files` | optional `path` (default `.`) | Lists one directory under the working directory, for a file tree. Each entry has `name`, `kind` (`file`, `dir`, `symlink`, `other`), `size` and `hidden`. A file also has `secret` (it looks like it holds secrets; `null` if genai would not read it at all, such as a binary or a file over the read limit) and `asks` (reading it would need your OK, even in coding mode). Contents are read only to classify them and never reach the model. Symlinks are listed, not followed; a bad path gets the error `bad_path`. |
| `trace` | optional `from`, `to` (exchange numbers) | Answered by `trace`: the exchanges with the endpoint in that range (default: the latest), exactly as sent and received. Each has `n`, `time`, `method`, `url`, `headers` (the API key and the endpoint's own header values are not shown), `request` (the body, as sent), `http`, `stream`, `response` (the reply as it arrived: each streamed line, or the whole body), `status` (`ok` or `error`, with `error`), `message` (the reply as genai put it together), `usage` and `ms`. genai keeps the last 30, in memory only; `kept` gives the range it still has. |
| `system` | | Answered by `system`: the system prompt and the tool definitions the next request will carry, with `model` and `params`. |
| `plan` | optional `clear: true` | Answered by `plan`: the approved plan, or `null`. With `clear`, drops it first, so everything asks again. |

While proposals are waiting for decisions, only `decision`, `result`, `attach`, `trace` and `system` are accepted.
Anything else gets the error `busy`, so coding mode and skills never change halfway through a
decision.

**Events genai sends**

| `type` | Meaning |
|---|---|
| `text` | A piece of the model's reply. |
| `proposal` | `id`, `kind` (`read`, `list`, `write`, `edit`, `command`, `external`, `invalid`), and the proposal's fields, such as `path`, `content`, `diff`, `command` and `reason`. `auto: true` means it needed no approval: coding mode, or a read the approved plan names. A `plan` proposal has `goal` and numbered `steps` (`n`, `kind`: `read`, `edit`, `write` or `command`, a `path` or the exact `command`, and `why`). While a plan is approved, a proposal it covers has `plan_step`; a command matching an approved step character for character also has `preapproved: true`, so the wrapper may run it without asking again; a command, edit or write the plan doesn't cover has `off_plan: true`. Edits and writes always ask. `secret: true` means the file looks like it holds secrets. A command is only shown: the wrapper runs it, if the user wants to, and can share the output with `attach`. |
| `result` | `id`, `status`, `detail`: what happened to a proposal that was performed. |
| `done` | The turn ended. `reason` is `reply`, `command` or `step_limit`; `context` gives `used` and `limit`; `exchanges` gives the first and last exchange numbers of this turn, for `trace` (also on an `error` that ends a turn). |
| `context` | The conversation is getting near the context limit. |
| `notice` | Something the user should know. |
| `error` | `code` and `message`. |
| `plan` | The approved plan changed: approved, a step done, or dropped. `plan` has `id`, `goal` and `steps`, each with `approved`, `status` (`todo`, `done`, `left out`) and `edited` when the user changed its command; `null` when there is none. A plan is never saved with the session. |
| `attached`, `registered`, `compacted`, `coding`, `skills`, `files`, `trace`, `system` | Answers to the messages above. |

When stdin closes, every waiting proposal is declined and genai exits.

## Live test

The live test takes any endpoint file that uses `key_env`. It checks the endpoint and model list,
has the model write a bash script to stdout,
redirected to a file with `>`, then checks it with `bash -n` without running it. It also runs the
JSON proposal/decision round trip, and checks that a persona profile picks its endpoint and that
its persona text reaches the model.

## License

MIT. See [LICENSE](LICENSE).
