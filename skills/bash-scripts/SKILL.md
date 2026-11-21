# Writing small, safe bash scripts

When the user asks for a shell script:

- Start with `#!/usr/bin/env bash` and `set -euo pipefail`.
- Quote every variable expansion: `"$file"`, `"${dir}/x"`. Use arrays for lists of arguments.
- Accept a `--dry-run` flag for anything that deletes, moves or overwrites, and make it print what would happen instead of doing it.
- Take paths as arguments with sensible defaults; never hard-code a user's home directory.
- Prefer `find ... -print0 | xargs -0` or `find ... -exec ... {} +` over parsing `ls`.
- Check for required tools with `command -v tool >/dev/null || { echo "tool is required" >&2; exit 1; }`.
- Send errors and progress to stderr; keep stdout for real output.
- Add a short usage message for `-h` / `--help`.
- Keep it under about 60 lines. If it grows, say so and suggest splitting it.

Where to put the script:

- In an interactive session, write it with `write_file` so the user can review it, then propose one command to test it, using `--dry-run` first.
- In a non-interactive run (output redirected to a file), output **only the script itself**: no Markdown code fences, and no text before the `#!` line or after the last line. Put explanations in `#` comments inside the script.
- Never run it yourself and never claim to have tested it.
