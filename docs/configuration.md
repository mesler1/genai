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
hoplon --bless /path/to/hoplon/skills/bash-scripts
```

## Files

```
hoplon                      the client (one file)
profiles/coder.json        example persona with coding mode
skills/hoplon-endpoint/     helps you write an endpoint
skills/hoplon-profile/      helps you write a profile (persona)
skills/hoplon-skill/        helps you write a skill
skills/bash-scripts/       conventions for small, safe bash scripts
skills/hpc/                working on a Slurm cluster: login nodes, srun/sbatch, modules, scratch
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
