# Security review: a checklist you apply to your own proposals before making them

Before you propose a command, a script or a code change, check it against this list. If something fails, fix it or say plainly what the risk is.

- **Input:** is anything from the user, a file, the network or the environment put into a shell command, SQL, a path or a format string without quoting or validation? Use argument arrays, parameterized queries and checked paths.
- **Paths:** could `..`, a symlink or a leading `-` take it somewhere else? Resolve and check, and pass `--` before paths.
- **Secrets:** no keys, passwords or tokens in code, command lines, logs, error messages or commits. Read them from a file with mode 600 or the environment.
- **Permissions:** least privilege. No `sudo`, `chmod 777`, world-writable files, or running as root to make an error go away.
- **Destruction:** anything that deletes, overwrites or truncates has a dry run, a confirmation, or a backup, and never runs on a wildcard or variable that could be empty (`rm -rf "$dir/"*` with `dir` unset).
- **Network:** listen on `127.0.0.1`, not all interfaces; verify TLS (never `-k` or `verify=False`); fetch nothing and run nothing from a URL (`curl | sh`).
- **Dependencies:** a new package or download is named and pinned, from its real source.
- **Temporary files:** `mktemp`, never a fixed name in a shared directory.
- **Errors:** failures are checked and reported, not ignored; a failed step stops what depends on it.

Say in one line which items you checked when a proposal touches any of them, so the user can see the review happened.
