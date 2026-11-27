# Benchmark tasks

A few fixed tasks for checking a change to hoplon's prompts or tool descriptions, or a different model, against
real runs instead of guessing. Each task runs as a [work session](../docs/work-sessions.md) and ends as a branch,
which a check script then tests.

```bash
bench/run.py run -p PROFILE -o before.json        # a blessed profile with a "sandbox" (coder has none)
# ... change a prompt, or pick another model with -e ...
bench/run.py run -p PROFILE -o after.json
bench/run.py compare before.json after.json
```

Every run makes real model requests on the endpoint, and can take a while: up to `--time-limit` seconds per task
(an hour by default), then up to 10 minutes for its check. `-t NAME` runs one task, and `--keep` keeps each task's repository so you can look at the
branch.

**How to use it:** change a prompt only for a failure you saw in a run, then run the set again to see whether
it helped and whether anything else got worse.

## What it does

For each task, the runner:

1. Copies the task's `repo/` into a new git repository with one start commit.
2. Starts `hoplon --json -p PROFILE` there, with your own configuration (it changes nothing in it), and sends
   the task.
3. Approves the work plan, and any new plan. Declines anything else that asks. When a turn ends before the
   session does, it tells the model to go on by itself, up to `--nudges` times (3 by default).
4. Runs the task's `check.sh` on the session's branch, in the profile's sandbox image (or this host's OS), confined as hoplon's own
   sandbox is: offline, with its memory and process limits, no capabilities, and only the end of the output kept.
   If the session gave up or ran out of time, the runner stops answering, and still checks whatever branch it left.

It prints a table, and with `-o` it writes a JSON file for `compare`. For each task it records:

- whether the check passed, and how the session ended
- the items done, and the model requests each item took (hoplon's steps)
- retries (a stuck item going back to the planner) and fresh contexts
- model requests and generated tokens
- wall time, and the diff size

## The tasks

| Task | What it tests |
| --- | --- |
| `parser-fix` | Fixing code until existing tests pass, without changing the tests; two bugs, one not in the obvious place. |
| `refactor` | A rename and a constant moved into one module, across files, with no change in behaviour. |
| `read-first` | A JSON output that must follow a format document to the character: it needs reading before editing. |
| `roguelike` | A small game from an empty project, against a stated interface, with tests it writes itself. |

## Writing a task

A task is a directory in `bench/tasks/` with three things in it:

- `task.md`: what the model is told. Its first line is the title.
- `repo/`: the project's files at the start: plain files only, no links and no `.git`.
- `check.sh`: run with `sh` in the repository on the branch.
  - It exits 0 when the work is right.
  - It runs offline, in the profile's sandbox image or on this host's OS, with a read-only root and a private `/tmp`. It needs `python3` for the tasks here.
  - `$BENCH` is the task's own directory, mounted read-only. The model never sees it, so the check (and files beside it, such as `expected.py`) can hold tests the model doesn't know.

Check that the check fails on the start and passes on a solution you wrote yourself before adding the task.
