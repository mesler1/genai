# Testing terminal programs (curses and other full-screen TUIs)

You can't see a terminal, but tmux can show you one as text. Use it to check what a full-screen
program actually draws.

- Keep the program's logic in code the unit tests can reach without a terminal. Use tmux for the
  screen: layout, glyphs, colours, keys. Don't write your own pty harness: tmux already is one.
- Each `run_in_sandbox` call is a fresh container, so start, drive, read and stop the program in
  ONE command:

  ```
  tmux new-session -d -x 80 -y 24 'python3 app.py'; sleep 1
  tmux capture-pane -p                       # the screen, as text
  tmux send-keys l l j; sleep 0.3            # keys: letters as they are; Enter Escape Up Down C-c
  tmux capture-pane -p
  tmux capture-pane -p -e | head -5          # with colour escape codes, to check colours
  tmux kill-server
  ```

- `sleep` after starting the program and after keys, so it has drawn before you capture.
  `tmux has-session 2>/dev/null || echo exited` tells you if it crashed; run it once without
  tmux (`timeout 2 python3 app.py`) to see a traceback.
- Read the capture before you call the screen done: are things aligned, does anything overlap
  or wrap, are the right characters where you expect them, does the status line fit? Check
  several sizes (`-x 80 -y 24` and something small, such as `-x 60 -y 20`).
- To check a property rather than eyeball it, pipe the capture into a few lines of Python, for
  example to assert every wall glyph joins its neighbours.
- Unicode: the sandbox runs with `LANG=C.UTF-8`. To check an ASCII fallback, start the program
  with `LANG=C LC_ALL=C`.
- If `tmux` is not installed, say so in your reply; the sandbox is offline, so you can't install it.
