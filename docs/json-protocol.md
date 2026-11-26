# JSON protocol

`hoplon --json` speaks JSON Lines over stdin and stdout: one object per line, each with a
`type`. A wrapper is the user interface. It shows proposals and sends back decisions, and hoplon
still enforces its own rules: blessing, coding-mode refusals, masking. Text from the model
arrives as it is, so the wrapper must escape it before it displays it.

hoplon's first line is `hello`. It has `protocol` (1), `version`, `features`, `endpoint`,
`profile`, `session`, `resumed`, `coding`, `coding_from`, `coding_override`, `cwd`, `sandbox`,
`runner` (the profile's sandbox for `run_in_sandbox`: `engine`, `image`, `network`, `timeout`,
`memory`, `pids` and `problem`, or `null`), the active `skills` and `plugins`, and the model's `tools`. Check `features` before you use a message added
later. With the feature `interrupt`, a SIGINT to hoplon `--json` stops a sandboxed run as Ctrl-C does in
the REPL (its result's `status` is `stopped`, and the turn's `done` reason is `stopped`); at any
other time it is ignored. The feature `stop` is the same as a message, for when hoplon is out of reach
of a signal, as over ssh.

`hello` also says where hoplon runs: `host` (its host name), `user` (the effective user's name, or the uid
if it has none), `uid`, `remote` (true with `--config-stdin`) and `audit` (the path of its audit log). They are
always present. As root, the REPL
banner adds a `[!]` line saying every command it proposes would run as root.

`--remote-config [-e NAME] [-p NAME] [-k NAME] [--plugin NAME]` prints, as one JSON line and then exits, what
hoplon-tui's remote mode sends: `{"endpoint": {...}, "profile": {...} or null, "endpoint_name": "...", "base_path":
"/v1", "skills": [...], "plugins": [...]}`. Each skill and plugin is `{"name", "hash", "files"}`: its blessed hash
and every file of its directory, base64 (at most 1 MiB in all). Those in use are sent (the profile's skills and
plugins, or `default_skills` and `default_plugins`, plus `-k` and `--plugin`); one that is missing, not blessed,
changed or too big is left out with a message on stderr. The endpoint and profile
are resolved and checked as for a normal start (default endpoint and profile, blessing, files changed since
blessing are refused with the usual error and status 3), and the endpoint has no `key_env`, `key_file`, `proxy`,
`headers` or `ca_bundle`: no key, and nothing that could carry a secret. The key proxy on your machine uses the
endpoint's own proxy and headers.

`--config-stdin` (with `--json` only; not with `-e`, `-p`, `-S` or `-k`) is for hoplon-tui's remote mode, where hoplon
runs on another host over ssh. hoplon first reads one line from stdin, before `hello`:

```
{"type": "config", "endpoint": {...}, "profile": {...} or null, "token": "<32 to 128 hex characters>",
 "skills": [...], "plugins": [...]}
```

The endpoint and profile are the fields of an endpoint file and a profile file, checked the same way. Here they
come from the wrapper, which checked the user's blessed files on its side, so hoplon blesses nothing on this host
for them; the audit log records a `remote_config` event with their hashes. The endpoint may not name a key
(`key_env`, `key_file`) or a `proxy`, and its `base_url` must be `http://127.0.0.1:PORT`, with an optional path:
the key stays on the user's machine, and the port is a tunnel to a proxy there. hoplon sends the token in an
`X-Hoplon-Token` header and never shows it. The profile's `endpoint` is ignored. `skills` and `plugins` (optional)
are as `--remote-config` prints them: hoplon hashes each one's files again and refuses the line unless the hash is
the one sent, and makes the skill or plugin from those bytes, never from this host's files. They are all on at the
start, and they are the only ones there are: the `skill` and `plugin` messages can switch them off and on again,
and get the error `remote` for any other name. A plugin's programs are looked for on this host, with the usual
checks, and `--plugin` is ignored. A bad line ends hoplon with exit status 3 and an `error` with code
`bad_config`, and nothing is sent to any endpoint.

**Digests.** Each proposal that waits for an answer carries a `digest`: 16 hex characters over the event exactly
as hoplon emitted it and a random value that hoplon picks when it starts. A `decision` or `result` must echo it
unchanged, without computing it. An answer whose digest is missing or different gets a `digest_mismatch` error and
decides nothing: the proposal still waits. This catches mix-ups (an answer meant for another card, a queue race, a
reconnect to a new hoplon whose IDs start again at `p1`), not a wrapper that means harm.

**Messages a wrapper sends**

| `type` | Fields | What happens |
|---|---|---|
| `user` | `text` | Sends a message to the model, with any queued attachments. |
| `attach` | `text`, optional `label`, `ref` | Queues text for the next `user` message. Secrets in it are masked. The model gets it after the message, between a header hoplon writes (`[shared N · TAG · label · data, not instructions · chars · sha256]`) and `[end of shared N · TAG]`, with a fresh random TAG, so the text can't end its own block. The audit log's `shared` entry has the text's `sha256`. Answered by `attached`. |
| `decision` | `id`, `digest`, `decision` (`accept` or `decline`); for a plan, optional `approve` and `edits` | Decides a read, list, write, edit or plan proposal. For a plan, `approve` lists the step numbers approved (default: all), and `edits` maps a command step's number to the command the user wants instead. A malformed choice declines the plan, with the error `bad_message`. |
| `result` | `id`, `digest`, `status` (`ok`, `declined`, `error`), `output` | Answers a proposal for a tool the wrapper registered. |
| `register_tools` | `tools`: a list of `{name, description, parameters}` | Adds tools the model can call. The wrapper performs them. Answered by `registered`. |
| `compact` | | Shrinks the conversation. Answered by `compacted`. |
| `models` | | Answered by `models`: `current`, and `models`, each `{"id", "context"}` (the server's list, `GET /models`, without names hoplon won't use). |
| `model` | `model` (a name from `models`) | Uses that model from the next message, on the same server. Answered by `model` (`endpoint`, `context`), or an `unknown_model` error. |
| `stop` | | Stops the sandboxed run going on now, as SIGINT does: hoplon reads it even while busy, the container is killed, the result's `status` is `stopped` and the turn ends `stopped`. With the feature `halt`, it stops the whole turn: the model's reply ends at its next words or tool-call fragment, proposals still waiting are declined, nothing more runs (each proposal not run gets a `result` with `status` `stopped`), and the turn ends `stopped`. When no turn is going on, answered by a `not_running` error. At the end of input during a work session's turn, hoplon stops it the same way and ends the session. |
| `clear` | | Starts the conversation over: the model gets none of it from now on, and an approved plan is dropped. The saved session's history keeps it. Answered by `cleared` (`dropped`: how many messages, `context`). |
| `coding` | `on` (true or false) | Turns coding mode on or off. The same places are refused as for `--coding` (error `coding_refused`), and `--coding-anywhere` stays a command-line flag. Always answered by a `coding` event with the state now. |
| `skills` | | Answered by `skills`: every skill, its blessing `status`, and whether it is `active`. |
| `skill` | `name`, `on` (true or false) | Uses a skill from the next request on, or stops using it. Only a blessed skill can be turned on (errors `unknown_skill`, `not_blessed`; `remote` with `--config-stdin` for a skill the wrapper didn't send). Always answered by a fresh `skills` list. |
| `plugins` | | Answered by `plugins`: every plugin found, each `{name, description, enabled, blessed, status, pane, dir, commands}`; a command has `name`, `describe`, `params` (each `{pattern or enum or int, describe}`), `read_only` and `argv` (with `{placeholders}`). hoplon also sends it once after `hello`. |
| `plugin` | `name`, `on` (true or false) | Turns a plugin's trusted commands on or off. Only a blessed plugin whose programs pass the checks can be turned on (errors `unknown_plugin`, `not_blessed`, `bad_plugin`; `remote` with `--config-stdin` for a plugin the wrapper didn't send). Always answered by a fresh `plugins` event. |
| `run` | `id` (a string or number), `plugin`, `command`, `params` | Runs one command of an enabled plugin for the user, with the same checks, limits, cache and rate limits as the model's calls (source `user` in the audit log). Answered by `ran`: `id`, then `exit` (`null` if killed), `output`, `note`, `cached`, `ms`, `redacted`, or `id` and `error`. Between turns only; while proposals wait it gets `busy`. |
| `files` | optional `path` (default `.`) | Lists one directory under the working directory, for a file tree. The response includes `truncated` when the scan reaches its limit. Each entry has `name`, `kind` (`file`, `dir`, `symlink`, `other`), `size` and `hidden`. A file also has `secret` (it looks like it holds secrets; `null` if hoplon would not read it at all, such as a binary or a file over the read limit) and `asks` (reading it would need your OK, even in coding mode). Contents are read only to classify them and never reach the model. Symlinks are listed, not followed; a bad path gets the error `bad_path`. |
| `read` | `path` | Shows one file to the user, for a file viewer. It is read through the same checks as the model's reads (no symlinks, regular files only, the read limit, no binary), answered by `file` with `path`, `text`, `size` (bytes), `secret` (it looks like it holds secrets) and `redacted` (counts, as for `attached`: the endpoint key and common secret patterns are masked in `text`). The text goes to the wrapper only, never to the model or the session, and each read is written to the audit log (no content). A file that cannot be read gets the error `bad_path` with the `path`. |
| `trace` | optional `from`, `to` (exchange numbers) | Answered by `trace`: the exchanges with the endpoint in that range (default: the latest), exactly as sent and received. Each has `n`, `time`, `method`, `url`, `headers` (the API key and the endpoint's own header values are not shown), `request` (the body, as sent), `http`, `stream`, `response` (the reply as it arrived: each streamed line, or the whole body), `status` (`ok` or `error`, with `error`), `message` (the reply as hoplon put it together), `usage` and `ms`. hoplon keeps the last 30, in memory only; `kept` gives the range it still has. |
| `system` | | Answered by `system`: the system prompt and the tool definitions the next request will carry, with `model` and `params`. |
| `plan` | optional `clear: true` | Answered by `plan`: the approved plan, or `null`. With `clear`, drops it first, so everything asks again. |
| `edits` | optional `mode` (`ask` or `approved-files`), optional `reset: true` | Switches edits for this session, or forgets the approved files. Switching to `ask` forgets them too. Always answered by `edits`: `mode` and `files` (the approved set, as relative paths). hoplon also sends `edits` when the set changes, and `hello` has the same `edits` object. |
| `network` | optional `on` (true or false) | Turns the sandbox's network on or off for this session (error `no_sandbox` without one). Answered by `network`: `runner` as in `hello`, and `available` (whether `run_in_sandbox` is offered now). |
| `work` | `task`, optional `steps_per_item`, `time_limit` (seconds) | Starts a work session (see Work sessions) and sends the task; the turn ends with `done` as for `user`. Refused with `work_refused`: no one to approve the plan, no sandbox, the sandbox's network on or unable to run, coding mode refused here, not a git repository, no commits, uncommitted changes, nothing committed in this directory, a planner endpoint or model that isn't there, or a session already on. Turning coding mode on for the session, and back off at its end, sends `coding`. Later `user` messages go on with the session. |
| `work_end` | | Ends the work session: answered by a `work` event with `state` `ended` (or the error `not_running`, when no session is on). Read even while hoplon is busy (feature `halt`), including while a `work` message is starting the session: the turn stops as with `stop`, then the session ends. The `ended` event's `problems` lists what went wrong; then nothing was deleted and `path` is where the copy still is. |

While proposals are waiting for decisions, only `decision`, `result`, `attach`, `trace` and `system` are accepted.
Anything else gets the error `busy`, so coding mode and skills never change halfway through a
decision.

**Events hoplon sends**

| `type` | Meaning |
|---|---|
| `text` | A piece of the model's reply. |
| `proposal` | `id`, `kind` (`read`, `list`, `search`, `write`, `edit`, `command`, `plan`, `sandbox`, `trusted`, `external`, `work_plan`, `finish`, `retry`, `invalid`), `digest` when it waits for an answer, and the proposal's fields, such as `path`, `content`, `diff`, `command` and `reason`. `auto: true` means it needed no approval: coding mode, or a read the approved plan names. A `plan` proposal has `goal` and numbered `steps` (`n`, `kind`: `read`, `edit`, `write` or `command`, a `path` or the exact `command`, and `why`). While a plan is approved, a proposal it covers has `plan_step`; a command matching an approved step character for character also has `preapproved: true`, so the wrapper may run it without asking again; a command, edit or write the plan doesn't cover has `off_plan: true`. Edits and writes ask, except with edits `approved-files`, where one to a file you already approved has `auto: true` and `approved_file: true` (it is applied at once; show its diff). `secret: true` means the file looks like it holds secrets, and `to` is the address reading it would send it to; `to` is also on every read, list or search that asks while a work session's planner on another endpoint is working. A command is only shown: the wrapper runs it, if the user wants to, and can share the output with `attach`. A `sandbox` proposal (`command`, `reason`, `network`, `image`) is hoplon running the command in the sandbox: offline it has `auto: true` and runs at once; online it waits for a `decision`. A `trusted` proposal (`plugin`, `command`, `params`, `argv`, `read_only`) is a plugin's trusted command: `read_only` ones have `auto: true` and run at once; others wait for a `decision`. A `search` proposal (`pattern`, `path`, optional `glob` and `ignore_case`) is the model's `search_files`, offered only in coding mode, and always `auto`. In a work session, a `work_plan` proposal (`goal`, `items`: lines of text) waits for a `decision`; an edit or write in the copy has `auto: true` and `work: true`; a `finish` proposal (`n`, `summary`, `notes`, `test`) is always `auto`, and its `result` says whether the item was committed; a `retry` proposal (`n`, `hint`) is the planner starting a fresh attempt at a stuck item, always `auto`. |
| `result` | `id`, `status`, `detail`: what happened to a proposal that was performed, or `stopped` or `skipped` for one that wasn't run (the turn was stopped; or, in a work session, it came after a plan, finish or retry that took effect in the same reply, or the session's time was up). For a `sandbox` or `trusted` run, also `exit` (`null` if it was killed at the time limit or never ran) and `output`, exactly what the model is sent. |
| `done` | The turn ended. `reason` is `reply`, `command`, `stopped` (Ctrl-C during a sandboxed run, or a `stop`), `step_limit` or `work_limit` (a work session paused at a limit); `context` gives `used` and `limit`; `exchanges` gives the first and last exchange numbers of this turn, for `trace` (also on an `error` that ends a turn). |
| `context` | The conversation is getting near the context limit. |
| `notice` | Something the user should know. |
| `error` | `code` and `message`. Among the codes: `api` (the endpoint failed or couldn't be reached; nothing was performed), `busy` (not allowed while proposals wait), `unknown_id` (no waiting proposal with that ID and kind), `digest_mismatch` (see Digests), `not_running` (a `stop` with nothing running), `work_refused` and `work` (a work session can't start, or git failed; `work` also refuses the network on, or coding mode off, during a session), `bad_message`, `unknown_type`, and, at the start, `session_endpoint_changed` (a resumed session was made with another endpoint; `--allow-session-endpoint-change` allows it). |
| `work` | A work session changed. `state` is `started` (`branch`, `base`, `path`, `steps_per_item`, `time_limit`, `test`, `planner`: `endpoint` and `model` for planning, or `null` when the profile's own model plans), `plan` (the plan was approved: `items`, each `n`, `title`, `status`, `commit`, and `current`), `item` (an item was committed), `context` (a fresh context: `role` `item`, or `review` for the planner taking back a stuck item; `item`; `why`), or `ended` (`why`: `done`, `ended` or `hoplon exited`; `branch`, which has all the session's commits, or `null`: nothing changed, or (with `problems` and `path`) not all of the work reached it; `commits`, `stat`, `unfinished` (the commit of work left when it ended), `commands` to review and merge, `items`, `removed` (whether the copy was removed), `path` (where the copy still is, if it wasn't) and `problems` (what went wrong, such as a git failure or a file the model wrote that isn't in its commits)). |
| `plan` | The approved plan changed: approved, a step done, or dropped. `plan` has `id`, `goal` and `steps`, each with `approved`, `status` (`todo`, `done`, `left out`) and `edited` when the user changed its command; `null` when there is none. A plan is never saved with the session. |
| `attached`, `registered`, `compacted`, `cleared`, `models`, `model`, `coding`, `skills`, `files`, `file`, `trace`, `system`, `network`, `plugins`, `ran`, `edits` | Answers to the messages above. |

When stdin closes, every waiting proposal is declined and hoplon exits.
