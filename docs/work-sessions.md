# Work sessions

A work session lets the model work through a task on its own, editing, building and testing without
asking, on a copy of your project. Nothing reaches your own files: at the end you review one branch
and merge what you want, with git.

```
:work make the parser accept quoted fields, with tests
```

1. **hoplon makes the copy.** It needs a git repository with no uncommitted changes to tracked
   files (commit or stash first). The copy is a new git repository under
   `~/.local/state/hoplon/work/` holding your last commit and nothing else: no history, no other
   branches. Each item the model finishes is committed there and fetched straight into a new
   branch `hoplon/work-N` in your repository. Untracked files aren't in the copy, and
   starting from a directory with nothing committed is refused. Coding mode turns on for the
   session if it was off, and goes back off when it ends; files you approved with edits
   `approved-files` are set aside until then.
2. **The model proposes a plan:** items in order, one line each, each small enough to finish and
   commit on its own. You approve it once. A new plan later asks again. Until a plan is approved,
   every edit and write asks.
3. **It works through the items without asking.** Reads, searches, edits and writes in the copy
   apply at once (you see each one), except to hidden or secret-looking files and `pyvenv.cfg`,
   which still ask. While the planner reviews a stuck item, its edits apply without asking too. The sandbox's
   network stays off for the whole session.
   Offline sandbox runs work on a throwaway overlay of the copy, as usual: they see the model's
   edits, and their own changes are discarded.
4. **It finishes each item with `finish_item`.** hoplon runs the profile's test command in the
   sandbox, if there is one, commits the changes in the copy, and fetches the commit into the
   branch in your repository. An item whose test fails, or
   that changed nothing, isn't finished, and the model is told why. The commit's title is the
   model's short summary; its body names the plan item and holds the model's notes for later items.
5. **At the end** hoplon commits anything left, removes the copy and shows the branch: its commits,
   the diff stat, and the commands to review it, merge it or delete it. hoplon never touches your
   own branch. A session that changed nothing leaves no branch. If git fails here, or a file the
   model wrote isn't in its commits (git ignores it, or it is inside a submodule's directory), the
   copy isn't deleted: hoplon says where it is, and what is committed still goes to the branch.

**Contexts.** Each plan item runs in a fresh context: the model sees the task, the plan and the
notes earlier items left, not their conversations, so a long session doesn't fill up. The context
that made the plan waits meanwhile. Within an item, old file contents and command output are
dropped once the context is 40% full (the model reads them again if it needs to), and an item
that still fills it goes on in a fresh context, with notes the model writes first.

**Limits.** Each plan item gets a budget of model steps (default 20). Finishing an item starts the
next with a fresh budget, but not fresh time. An item that uses up its budget goes back to the planning context, with
what it changed and its last words; the planner finishes it, starts a fresh attempt with a hint, or
proposes a new plan. If the planner uses up its steps too, or the item gets stuck again, the session
pauses. There is also a time
limit, 2 hours without you by default. A message from you, such as "go on", gives a fresh budget
and time. `:work end` (or exiting hoplon) ends the session; `:work` shows how far it has got. In
hoplon-tui, `/work TASK`, `/work` and `/work end` do the same, and the info panel's WORK section shows
the branch and each item as it is committed. Ctrl-C or `/stop` stops the turn, including a running
test, and the session pauses; `/work end` works mid-turn too, and quitting hoplon-tui ends the
session the same way, as does a SIGTERM or SIGHUP to hoplon.

The profile sets the defaults, and `:work --steps N --minutes M TASK` overrides them for one session:

```json
{
  "name": "coder-py",
  "endpoint": "work-llm",
  "coding": true,
  "sandbox": {"image": "localhost/myproject-dev:latest"},
  "work": {"test": "python3 -m unittest -q", "steps_per_item": 20, "time_limit": 7200}
}
```

**A model for planning.** `planner` in `work` sends the planning contexts (making the plan, and
reviewing a stuck item) to another model, such as a stronger one, while the items run on the
profile's own:

- `"planner": {"model": "big-model"}`: another model on the same endpoint. hoplon checks the server
  lists it before the session starts, and uses the context length the server gives for it.
- `"planner": {"endpoint": "other", "model": "big-model"}`: another blessed endpoint (without
  `model`, its own default). It reads files, so it sees the project too. In remote mode only the
  profile's endpoint can be reached, so it plans as well, with a notice.

A planner of its own starts from the task alone: the conversation before the session isn't sent to
it, and comes back when the session ends. A file that looks like it holds secrets asks before it is
read, and every read question while it plans names where the file would go. A planner on another
endpoint or with its own model must be listed by that server (`model` left out or `auto` takes
the first it lists); if the server can't be asked, the session doesn't start.

**Git stays on your side.** hoplon runs every git command itself, on your machine, with hooks,
fsmonitor and signing off. The copy has no config of its own beyond what `git init` writes, so your
repository's own config (its filters, hooks and includes) never applies there; your global and
system config does. Work sessions don't support Git LFS yet: a repository with LFS files is
refused, as the copy can't reach the LFS store. hoplon touches
your repository only to read your last commit into the copy and to fetch the copy's commits into
`hoplon/work-N`. No file tool may write anything under `.git`, and the sandbox only ever gets an
overlay of the copy's files: its git directory is beside them, out of the sandbox's reach. So
nothing the model or a test writes can make git run a program on your machine. Commits use your
git identity, or `hoplon` if you have none. If a session ends with a problem (git fails, or a file
the model wrote isn't in its commits), the copy stays where it is for you to look at, and git works
in it as in any checkout. One left by a hoplon that was killed outright stays too; there, git needs
`--git-dir=../git` (the sandbox gets no `.git` at all, so a build that runs git sees no repository).
