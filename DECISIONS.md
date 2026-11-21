# Design decisions and known limitations

genai's core rule is **the model proposes, the human acts**. The model has five intrinsic tools,
none of which runs anything: it can propose a command or a file operation, and genai performs a
file operation only after you accept it. Sandboxing is left to whatever wraps genai.

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
| Coding mode in `~` and `/` | Refused. From a profile it is turned off with a notice instead of failing. |
| Proxy variables | Ignored. genai builds its own opener and never reads `https_proxy`. An endpoint can set an explicit `"proxy"`, which blessing shows. |
| Key sources | `key_env` or `key_file`. A key file must be mode 0600 and owned by you. With neither, no `Authorization` header is sent, which suits loopback test servers. Blessing shows "key from: none". |
| Skill format | A directory with `SKILL.md`. Only its first line, the description, is sent with every request. Every file in it is hashed. |
| `-S` | Takes a name: `-S nightly` creates the session or resumes it. |
| Shared endpoints | A blessed endpoint, profile or skill can be found by its `name` wherever it lives. |
| Endpoint fields | Unknown fields are errors. Credentials in `headers` are refused. `params` may not override `model`, `messages` or `tools`. |
| REPL input | `..` on a line by itself sends, and so does Ctrl-D. A `:` command works on an empty buffer. |
| `:run` and `:sh` output | One editor buffer holds everything that will be sent: where it ran, the command as run, and the output (redacted and clipped). What you save is sent exactly, with nothing added, so you can remove a secret from the command as well as from the output. An empty buffer sends nothing. |
| Edits the model may not read | If an edit can't be previewed (text not found or ambiguous, file missing or binary) and the model isn't allowed to read the file without asking, you see why locally. The model only gets "declined by the user", so it learns nothing about the file's contents. |
| Approval and the file's state | Each write or edit is bound to the target's state when you reviewed it: its sha256, or that it didn't exist. If the file changed before it's applied, nothing is written and the model is told to propose again. In a batch of two writes to one file, the second is refused. |
| Blessed files are read once | An endpoint or profile is hashed and parsed from the same bytes. A skill is read into memory when it's loaded, and only that copy is used, so editing it mid-session changes nothing until it's reloaded. `--bless` records the hash of exactly what it showed you. |
| Model text on the terminal | Streamed replies, reasons, paths, notes and summaries have control, bidi and invisible characters escaped before display, with a warning on stderr. JSON mode is JSON-encoded instead. |
| Commands in the audit log | Recorded as `command_sha256` and `command_len`, never as text, since a command you edited may hold a secret. |
| `key_env` | Must be a plain variable name (`[A-Za-z_][A-Za-z0-9_]*`), in genai and in `sandbox/run`, which uses it in bash indirect expansion. |
| Context full | The new message is not kept. Compact, then send again. |
| Compaction | Prunes bulk older than the last 2 user turns, and summarizes only if still above 80%. The summary goes in front of the first kept message. The full history stays in the session file. |
| JSON while proposals are pending | `attach` is queued. Anything else gets an error with code `busy`. |
| Coding mode over JSON | Only the `--coding` flag. There is no message for it. |
| `write_file` into a missing directory | Creates the directory, inside the working directory only. |
| Config location | `$XDG_CONFIG_HOME/genai`, default `~/.config/genai`. Created on first run (0700), with a starter `config.json` listing every setting. Existing files are left alone. The sandbox uses this to keep its own config. |

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

1. **Size.** `genai` is about 2,000 lines. The JSON protocol, the REPL with `:run`/`:sh`, compaction
   and blessing each take 200 to 300.
2. **`:sh` capture needs `script`** (util-linux), which is not in the standard library. If it's
   missing, genai opens the subshell without capture.
3. **Editor leftovers.** Only vi, vim and nvim get flags that turn off swap and backup files. Other
   editors may leave copies of the output on disk.
4. **What you see vs what runs.** Proposals are shown escaped. The `:run` editor shows the raw text,
   so a flagged command also prints a warning. The editor itself could still render hidden characters.
5. **SELinux.** `sandbox/run` doesn't relabel mounts. On SELinux hosts such as RHEL, it may need `:z`
   or a label policy.
6. **Apply-time race.** The state check runs just before the write's rename. An editor saving in
   that instant can still be overwritten; closing that fully would need file locking.
7. **Live test keys.** `sandbox/run live` passes in only a `key_env` variable. It can't test an
   endpoint that uses `key_file`.
