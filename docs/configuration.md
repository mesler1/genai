# Configuration

## Settings

On first run hoplon creates `~/.config/hoplon` (or `$XDG_CONFIG_HOME/hoplon`), mode 0700, with
`endpoints/`, `profiles/`, `skills/`, `plugins/`, `sessions/` and a `config.json` that lists every setting at its
default. It says so once on stderr and never changes what is already there.

| Setting | Default | Meaning |
| --- | --- | --- |
| `default_profile` | `null` | Profile used when `-p` is not given. |
| `default_endpoint` | `null` | Endpoint used when neither `-e` nor the profile names one. |
| `default_skills` | `[]` | Skills used when the profile doesn't list its own. |
| `default_plugins` | `[]` | Plugins used when the profile doesn't list its own (`--plugin` adds more). |
| `endpoint_dirs` | `[]` | Extra directories searched for endpoints, after `~/.config/hoplon/endpoints/`. |
| `profile_dirs` | `[]` | Extra directories searched for profiles, after `~/.config/hoplon/profiles/`. |
| `skill_dirs` | `[]` | Extra directories searched for skills, after `~/.config/hoplon/skills/`. |
| `plugin_dirs` | `[]` | Extra directories searched for plugins, after `~/.config/hoplon/plugins/`. |
| `max_steps` | `50` | Model steps in a row that run without you deciding anything (automatic reads, searches, offline sandboxed runs) before the turn pauses; send a message to go on. A step where you approve or decline something starts the count again, so a turn you steer has no limit. 1 to 1000. |
| `alerts` | `true` | `false` keeps the notify hook quiet ([Hooks](hooks.md)); `:alerts on` or `off` switches it for a session. hoplon-tui's bell is set in `tui.json` (`/alerts`). |
| `fixtures_dir` | `null` | Where `/fixtures` keeps real samples for sandboxed runs; `null` means `~/.local/state/hoplon/fixtures` (`$XDG_STATE_HOME/hoplon/fixtures`). It must be outside every project and must not be or hold your home directory. See [Sandbox](sandbox.md). |

Keys starting with `_` are comments. Any other unknown key gets a warning, so a typo isn't silently
ignored. A blessed endpoint or profile is also found by name wherever it lives, so you can bless the
repo's files in place. For example, to use the coder persona by default and the bundled skills:

```json
{"default_profile": "coder", "default_endpoint": "work-llm", "skill_dirs": ["/path/to/hoplon/skills"]}
```

```bash
hoplon --bless /path/to/hoplon/profiles/coder.json
hoplon --bless /path/to/hoplon/skills/bash
```

### Skill names

A skill's name is its directory's: letters, digits, `.`, `_` and `-`, starting with a letter or digit,
at most 48 characters. A skill with any other name is listed as `bad name` (escaped and cut short)
and can't be blessed or switched on. Every listing (`--list-skills`, hoplon-tui's skills, the
`skills` message) escapes names and descriptions and cuts descriptions to 160 characters, so what a
skill's files say can't fake or break the list.

### Generic and site skills

A convention, not a mechanism. A generic skill (`slurm`, `hpc`) holds only what is true on every site.
A site skill holds only one site's facts and says that it wins where they disagree; if its generic
skill isn't on, it tells the model to say its general knowledge may not match the site and to suggest
switching that skill on. hoplon never loads one skill because of another. The `hoplon-skill`
authoring skill has a template for a site skill.

On a shared cluster, `multi-user` and `parallel-fs` are worth having on all the time: bless them
and add them to `default_skills`. hoplon doesn't switch any skill on by itself.

### Skills the model can suggest

The model sees one line for each blessed skill: its name and description (the first line of its
`SKILL.md`). Unblessed skills are never named to it. Before it plans, or when a request clearly needs a
skill that is off, it may ask to switch some on (`suggest_skills`); you answer yes or no, as with any
proposal. Only you switch skills on, and the model can't switch one off. If you decline, it may not
suggest those skills again in that run unless you ask.

A skill switched on after the conversation has begun, at the model's suggestion or with `:skill NAME
on` (`/skills` in hoplon-tui), comes into the conversation where it was switched on: with the
suggestion's answer, or with your next message. The system prompt stays the same, so the server can
keep using what it has cached. Switching a skill off takes its text out of the conversation again.

`not-secret.json`, next to `config.json`, holds the files you said hold no secrets though they look
as if they do (the sha256 of each text, and where it was); remove an entry to be asked again.

## Files

```
hoplon                      the client (one file)
profiles/coder.json        example persona with coding mode
skills/hoplon-endpoint/     helps you write an endpoint
skills/hoplon-profile/      helps you write a profile (persona)
skills/hoplon-skill/        helps you write a skill
skills/bash/                Bash: house rules for scripts, and the gotchas that bite
skills/c/                   C: house rules and gotchas
skills/csh/                 C shell: csh and tcsh syntax, not Bash's
skills/multi-user/          being safe and fair on a machine others share
skills/parallel-fs/         Lustre, GPFS and the like: metadata, small files, striping
skills/rust/                Rust: house rules and gotchas
skills/security-review/     a checklist the model applies to its own proposals
skills/hpc/                 a shared cluster: login nodes, modules, file systems (any scheduler)
skills/slurm/               generic Slurm: srun/sbatch, batch scripts, job arrays, failed jobs
plugins/slurm/             trusted read-only Slurm commands: sinfo, squeue, scontrol show, sacct
sandbox/run                podman sandbox for running hoplon and its tests, on this machine's OS
sandbox/hooks/modules.sh   sandbox hook: makes loaded environment modules work inside the sandbox
hooks/notify-desktop       example notify hook: a desktop notification when hoplon waits for you
tests/                     offline suite, live endpoint test, harness self-check
```

## Live test

The live test takes any endpoint file that uses `key_env`. It checks the endpoint and model list,
has the model write a bash script to stdout,
redirected to a file with `>`, then checks it with `bash -n` without running it. It also runs the
JSON proposal/decision round trip, and checks that a persona profile picks its endpoint and that
its persona text reaches the model.
