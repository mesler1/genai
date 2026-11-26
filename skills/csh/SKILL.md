# C shell (csh and tcsh): the syntax, not Bash's

The user's shell or scripts are csh or tcsh. Models slip into Bash syntax here, which fails or, worse, does something else. Check every line against this list.

| Bash | csh / tcsh |
|---|---|
| `x=1` | `set x = 1` |
| `export X=1` | `setenv X 1` (no `=`) |
| `unset X` (env) | `unsetenv X` |
| `arr=(a b c)`, `${arr[0]}` | `set arr = (a b c)`, `$arr[1]` (1-based), count `$#arr` |
| `if [ "$x" = y ]; then ... fi` | `if ("$x" == "y") then` ... `endif` |
| `for f in *.txt; do ... done` | `foreach f (*.txt)` ... `end` |
| `while ...; do ... done` | `while (cond)` ... `end` |
| `case ... esac` | `switch ($x)`, `case foo:`, `breaksw`, `endsw` |
| `$(cmd)` | `` `cmd` `` (no `$(...)`) |
| `cmd 2>&1` | `cmd >& file`; both to a pipe: `cmd \|& next` |
| `cmd 2>/dev/null` (stderr only) | not possible directly: `(cmd > out) >& /dev/null` |
| `$?` | `$status` |
| `source ~/.bashrc` | `source ~/.cshrc` (or `~/.tcshrc`) |
| `function f { }` | none: use `alias` or a separate script |
| `PATH=$PATH:/x` | `set path = ($path /x)` or `setenv PATH ${PATH}:/x` |

More gotchas:

- `if` needs `then` on the same line, and `endif` alone on its line. Expressions use `==`, `!=`, `=~` (glob), `&&`, `||`, and file tests like `-e file`, `-d dir`.
- A variable that isn't set is an error: test with `$?x` (1 if set).
- History expansion: `!` inside double quotes is still special; use single quotes or `\!`.
- `$var:t`, `:h`, `:r`, `:e` give a path's tail, head, root and extension.
- Scripts start with `#!/bin/csh -f` (or `#!/bin/tcsh -f`); `-f` skips the user's `.cshrc`.
- For anything beyond simple setup, say so and suggest writing the logic in Bash or Python, called from csh.
- Environment modules in csh: `module load NAME` works the same; `eval` lines differ, so use the site's csh init file.
