# Endpoints and profiles

An **endpoint** says where prompts go: URL, model, key source, context size. It is the
security-relevant file, because it decides where your prompts, files and key are sent.
`hoplon --new-endpoint` asks for these, lists the server's models if you let it (that request goes
to the address you typed, with your key, before anything is blessed, so it asks first), writes the file
with private permissions and tells you to bless it; it never blesses it for you.

A **profile** is a persona. It can name an endpoint and add standing instructions for the
model, default skills, sampling settings and coding mode:

```json
{
  "name": "coder",
  "description": "Careful senior engineer: small diffs, tests first",
  "endpoint": "work-llm",
  "persona": "You are a careful senior software engineer pairing with the user. ...",
  "skills": ["bash"],
  "coding": true
}
```

- `"model"` in a profile picks the model: one of the endpoint's aliases (below) or a name the server
  lists. A model the endpoint doesn't have refuses the session; there is no fallback.
- `-p NAME` picks a profile and `-e NAME` picks an endpoint. `-e` overrides the profile's endpoint, and
  `hoplon -e NAME` works with no profile at all. A profile without an endpoint (like the bundled
  `coder`) uses `-e` or `default_endpoint`.
- The persona text is sent after hoplon's own rules and can't change them: the model still only proposes.
- `skills` replaces `default_skills` from `config.json`. Leave it out to keep the defaults; `[]` means
  none. `-k` always adds more.
- A profile's `params` override the endpoint's. Put the model maker's recommended sampling settings in
  the endpoint, and only change them in a profile for a reason.
- `"coding": true` turns coding mode on, as `--coding` does. A project directory under your home is
  fine. It is refused in `~` itself, `/`, hoplon's config directory and credential directories such as
  `~/.ssh`, where hoplon prints a notice and leaves it off. Only you can override that, with
  `--coding-anywhere` on the command line. The REPL banner and the JSON `hello` event say where coding
  mode came from.
- `"edits": "approved-files"` (default `"ask"`): once you approve an edit or write to a file, further
  edits and writes to that same file apply without asking, for the rest of the session. They are
  still shown with their diff, logged, and bound to the file as it was. New files, hidden files (a
  name starting with `.`, or one the sandbox hides as a secret, such as `id_rsa` or `*.key`),
  anything that looks like a secret, and a whole replacement of a file that can't be diffed always
  ask; declining an edit to a file takes it out of the set. `:edits` (or `/edits` in hoplon-tui)
  shows the files, `:edits reset` forgets them, and `:edits ask|approved-files` switches for this
  session. The set is never saved, so a resumed session starts empty. Blessing flags the field.
- `"layout"`: which of hoplon-tui's panes start shown, as `{"files": "shown", "panel": "hidden",
  "shell": "shown"}` (any of the three, each `"shown"` or `"hidden"`). Panes it leaves out start as
  they always do. `--files` and `--no-panel` on the command line win over it; `/files`, `/panel` and
  Ctrl-` still show and hide them afterwards. The REPL ignores it.
- Endpoints, profiles and skills are each blessed with `hoplon --bless PATH`. A profile can only use an
  endpoint and skills that are blessed on their own. If any file changes, hoplon refuses it until you
  bless it again.

## Model aliases

An endpoint can give its models names of your own:

```json
{"name": "cloud", "base_url": "…", "model": "medium",
 "aliases": {"low": "flash-model", "medium": "mid-model", "high": "pro-model"}}
```

Anywhere a model is named, an alias works: the endpoint's own `model`, a profile's `model`, a work
session's `planner` (`{"model": "high"}`) and `:model high` (or `/model high` in hoplon-tui, whose
drop-down lists the aliases first). An alias is looked up first; otherwise the name is the server's
own, and must be one it lists. An alias needn't be listed by the server: it is in the endpoint file, so
blessing shows which model each name sends your work to, and a changed alias needs blessing again.

The names are yours (a letter, then letters, digits, `_` and `-`; not `auto`). Using the same names on
every endpoint, such as `low`, `medium` and `high`, lets one profile work with any of them. A name an
endpoint doesn't define refuses the session, with what to add to that endpoint. hoplon shows both
names, as `high (pro-model)`, with a long server name cut to its last part; the audit log records the
server's name.

| Use | Command |
| --- | --- |
| Attach files | `hoplon -p NAME -f app.log -f app.conf "why is it crashing?"` |
| Skills | `hoplon -p NAME -k slurm` (skills must be blessed) |
| Command sets (scoped commands) | `hoplon -p NAME --commands slurm` (command sets must be blessed; [Command sets and scoped commands](commands.md)) |
| Sessions | `hoplon -p NAME -S nightly "summarize" > out.txt` |
| Coding mode | `hoplon -e NAME --coding` (reads stop asking, except files that look like secrets; writes still ask; the model can also search the files) |
| Coding mode where it is refused | `hoplon -e NAME --coding-anywhere` (prints what that exposes) |
| Builds and tests the model runs itself | a profile with `"coding": true` and a `"sandbox"` ([Sandboxed runs](sandbox.md#sandboxed-runs)) |
| Start over, keeping the session | `:clear` in the REPL, `/clear` in hoplon-tui (the model forgets the conversation; the saved session keeps it) |
| See exactly what was sent | `:raw [N\|all]` and `:system` in the REPL, `/raw` and `/system` in hoplon-tui (in `$PAGER`, default `less`; your API key is never shown) |
| Wrappers | `hoplon -p NAME --json` (JSON Lines over stdio) |
| Lists | `hoplon --list-endpoints`, `--list-profiles`, `--list-skills`, `--list-commands`, `--list-sessions` (each with `--json`) |
| What is left over | `hoplon --leftovers` (with `--json`), `:leftovers` in the REPL, `/leftovers` in hoplon-tui: see [Leftovers](sandbox.md#leftovers); `hoplon --clean-stale` removes the stale ones after asking |

Exit codes: 0 ok, 1 error, 2 usage, 3 endpoint or profile (none, unblessed, changed, key missing),
4 endpoint request failed, 5 context full, 130 interrupted.

## Searching files

In coding mode the model also has `search_files`: a literal string (not a regular expression), optionally
under a directory and only in files whose name matches a `glob` such as `*.py`, case-insensitive if it asks.
It runs without asking, like a read, and gets back each matching line as `path:line: text`. It looks only where
an automatic read would: hidden files and directories and symlinks are skipped, and so are binary files, files
over the read limit and files that look like they hold secrets (it is told how many). It stops after 2000 files,
200 matches or 5 seconds, and each line is cut to 200 characters around the match.
