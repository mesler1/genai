# Multi-user systems: be safe and fair on a machine others share

Other people use this machine and its file systems, so what you write and propose must not expose them or the user.

- **Temporary files:** `mktemp` or `mktemp -d`, never a fixed name like `/tmp/out.txt`: another user can create it first, or read it. Clean up with a `trap` on exit.
- **Permissions:** a sane umask (`umask 027`, or `077` for anything private). Never create world-writable files or directories, and never propose `chmod 777` or `chmod -R a+w`. In a shared project directory, use the project's group (`chgrp`, the setgid bit on directories, `umask 007`) rather than opening things to everyone.
- **Secrets never go on a command line:** arguments show in `ps` and `/proc` to every user, and land in shell history. Pass them in a file with mode 600, an environment variable from a file, or a program's own prompt.
- **Don't read other people's files** even where permissions allow it, unless the user asks and they are meant to be shared.
- **Shared resources:** no busy loops, no polling every second, no unbounded parallelism (`xargs -P 0`, `make -j` with no number). Ask for what the work needs, and say how long it will hold it.
- **Processes:** kill only processes by an exact pid the user confirmed, never by name (`pkill`, `killall`) and never another user's.
- **Ports and sockets:** bind to `127.0.0.1` rather than all interfaces, and remember that any user on the machine can reach a local port: protect it with a token or use a Unix socket in a private directory.
