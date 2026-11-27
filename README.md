# Hoplon

A coding agent for power users who want full control over everything it does.

**Zero dependencies.** The core is a single Python file, about 6,300 lines, using only the standard
library. There's nothing to `pip install` and nothing pulled in. Your own agent can audit it in
minutes before you run it. All it needs is Python 3.10 or newer. The terminal UI is optional and
adds tmux 3.2 or newer.

- **Zero defaults.** No endpoint is built in, so nothing is sent anywhere until you name one and
  approve it.
- **You run the commands.** The model proposes, and you decide. The only exception is opt-in: an
  offline container on a throwaway copy of your project.
- **One way out.** No web access and no rendered links or images. The model reaches only the
  endpoint you approved.
- **Secrets stay put.** Files that look like secrets ask first and are hidden from the sandbox, and
  keys are masked in output.
- **Your project can't configure the agent.** Nothing is read from the folder you work in, and every
  endpoint, profile and skill needs your approval.
- **What you approve is what happens.** An approval is tied to the exact change you saw.
- **Autonomy on a leash.** Work sessions run in a private copy, and you merge the result yourself.

Full threat model: [SECURITY.md](SECURITY.md). Design choices and known limits:
[DECISIONS.md](DECISIONS.md).

## Quick start

From a clone of this repository:

```bash
mkdir -p ~/.local/bin                       # any directory on your PATH will do
ln -s "$PWD/hoplon" "$PWD/tui/hoplon-tui" ~/.local/bin/
hoplon --list-endpoints                      # the first run creates ~/.config/hoplon (none yet)
$EDITOR ~/.config/hoplon/endpoints/work-llm.json         # write your endpoint: see below
hoplon --bless ~/.config/hoplon/endpoints/work-llm.json   # shows URL, model, key source; asks you to confirm
export HOPLON_API_KEY=...                    # the endpoint names the variable, never the key
hoplon -e work-llm "what does 'set -euo pipefail' do?"
```

The endpoint file says where your prompts go. Fill in your server's OpenAI-compatible API address
(usually ending in `/v1`), its model name, the environment variable that will hold your API key (never
the key itself) and the model's context size in tokens:

```json
{
  "name": "work-llm",
  "base_url": "YOUR-SERVER-ADDRESS/v1",
  "model": "YOUR-MODEL-NAME",
  "key_env": "HOPLON_API_KEY",
  "context_limit": 128000
}
```

To use the example persona, which grants coding mode and uses a bundled skill, add
`"skill_dirs": ["/path/to/hoplon/skills"]` to `~/.config/hoplon/config.json` ([Configuration](docs/configuration.md)),
then bless both and start it in a project directory (coding mode is refused in your home directory):

```bash
hoplon --bless profiles/coder.json           # a persona: shows its text, endpoint, skills, coding grant
hoplon --bless skills/bash                   # the skill it uses
cd ~/src/some-project
hoplon -e work-llm -p coder                  # REPL: a line with only '..' sends; :help for commands
hoplon-tui -e work-llm -p coder              # the same in tmux panes, beside your shell (needs tmux)
```

## Documentation

- [Endpoints and profiles](docs/endpoints-and-profiles.md): where prompts go, personas, coding mode,
  approved files, command-line use
- [Configuration](docs/configuration.md): `~/.config/hoplon`, settings, the files in this repository,
  the live test
- [hoplon-tui](docs/hoplon-tui.md): the terminal UI, another host over ssh, remote mode, the file tree
- [Work sessions](docs/work-sessions.md): the model plans, codes, tests and commits on a branch of its own
- [Sandbox](docs/sandbox.md): running hoplon in a container, and the model's own sandboxed builds and tests
- [Command sets and scoped commands](docs/commands.md): narrow commands you approve once
- [Hooks](docs/hooks.md): your own programs at fixed points ([HOOKS.md](HOOKS.md) has the design)
- [JSON protocol](docs/json-protocol.md): driving hoplon from a wrapper

## License

MIT. See [LICENSE](LICENSE).
