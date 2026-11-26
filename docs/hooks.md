# Hooks

A hook is a small program of yours that runs at a fixed point. There are two kinds, and neither
can approve anything, add a tool, or make the sandbox less strict. [HOOKS.md](../HOOKS.md) has the
full rules and the reasons.

**Sandbox hooks** run before `sandbox/run` starts the container. Each one prints requests: mount a
host directory read-only (`ro DIR`), pass a search path (`path NAME`), or pass a variable
(`env NAME`). `sandbox/run` checks every request and prints what each hook added. The bundled
`modules.sh` makes software from environment modules (Lmod, EasyBuild) work inside:

```bash
mkdir -p ~/.local/share/hoplon-sandbox/hooks
ln -s /path/to/hoplon/sandbox/hooks/modules.sh ~/.local/share/hoplon-sandbox/hooks/
module load gcc/13 openmpi
sandbox/run hoplon -e NAME
```

**The notify hook** tells you when hoplon is waiting for you: proposals to review, a proposed
command, or a finished reply. It is the file `~/.config/hoplon/hooks/notify`, and it runs only
after you bless it. It gets the event's name and a count, never file contents, commands or the
key.

```bash
mkdir -p ~/.config/hoplon/hooks
cp /path/to/hoplon/hooks/notify-desktop ~/.config/hoplon/hooks/notify
hoplon --bless ~/.config/hoplon/hooks/notify
```

To keep it quiet without removing it, set `"alerts": false` in `config.json`; `:alerts off` and
`:alerts on` switch it for one session. It runs only when hoplon is at a terminal, never under
`--json` (hoplon-tui has its own bell: `/alerts`).

Anything else, such as another front end or automatic decisions, belongs in a wrapper over
`--json`.

When a saved session's destination changes (the server's address or port, or the endpoint's
proxy, CA bundle or headers), or an older session has no recorded destination, hoplon and
hoplon-tui require `--allow-session-endpoint-change` before sending the saved conversation. Check
the URL shown in the error before approving the transfer. Another model on the same server needs
no consent.

**Models.** `"model": "auto"` in an endpoint uses the first model the server lists (`GET /models`;
vLLM and most local servers list the one they serve) and takes its context length from the list
when the server gives one (vLLM's `max_model_len`). To switch while you work: `:models` lists them
and `:model NAME` switches in the REPL; in hoplon-tui, `/model` (or a click on the model in the
info panel) opens a drop-down, and `/model NAME` switches directly. Only a model the server lists
can be chosen, the conversation carries on with it, and the audit log records the switch. It
lasts for this run: the endpoint file isn't changed, and a resumed session starts with the file's
model again.
