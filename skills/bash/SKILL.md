# Bash: house rules for scripts, and the gotchas that bite

When the user asks for a shell script:

- Start with `#!/usr/bin/env bash` and `set -euo pipefail`.
- Quote every variable expansion: `"$file"`, `"${dir}/x"`. Use arrays for lists of arguments: `args=(-v "$f")`, `cmd "${args[@]}"`.
- Accept a `--dry-run` flag for anything that deletes, moves or overwrites, and make it print what would happen instead of doing it.
- Take paths as arguments with sensible defaults; never hard-code a user's home directory.
- Prefer `find ... -print0 | xargs -0` or `find ... -exec ... {} +` over parsing `ls`.
- Check for required tools with `command -v tool >/dev/null || { echo "tool is required" >&2; exit 1; }`.
- Send errors and progress to stderr; keep stdout for real output.
- Add a short usage message for `-h` / `--help`.
- Temporary files: `tmp=$(mktemp)` (or `mktemp -d`) and `trap 'rm -rf -- "$tmp"' EXIT`; never a fixed name in `/tmp`.
- Keep it under about 60 lines. If it grows, say so and suggest splitting it.

Gotchas:

- `set -e` doesn't stop at a failure inside `if`, `&&`, `||`, a function called from those, or a command substitution in `local x=$(cmd)`: write `local x; x=$(cmd)`.
- A pipeline into `while read` runs the loop in a subshell, so variables set there are lost: use `while read -r line; do ...; done < <(cmd)`.
- Always `read -r`, and `IFS= read -r line` to keep leading spaces.
- `[[ ]]` for tests in bash; `==` in `[[ ]]` matches a glob, so quote the right side for a literal: `[[ $a == "$b" ]]`.
- Arithmetic with `(( ))`; `((n++))` returns 1 when `n` was 0, which `set -e` treats as a failure: use `n=$((n + 1))`.
- `cd` can fail: `cd -- "$dir" || exit 1`. Use `--` before paths that might start with `-`.
- `echo` mangles some strings (`-n`, `-e`, backslashes): use `printf '%s\n' "$x"`.
- `$*` and unquoted `$@` split on spaces; pass arguments on with `"$@"`.

Where to put the script:

- In an interactive session, write it with `write_file` so the user can review it, then propose one command to test it, using `--dry-run` first.
- In a non-interactive run (output redirected to a file), output **only the script itself**: no Markdown code fences, and no text before the `#!` line or after the last line. Put explanations in `#` comments inside the script.
- Never run it yourself and never claim to have tested it.
