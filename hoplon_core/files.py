# hoplon_core/files.py: the file tools, confined to the working directory (a handler: the edit request).
#
# Workdir: reads, writes and edits that stay inside the project, through file descriptors so a symlink or a rename
# can't lead them out, and that refuse a file changed since it was shown. It performs; the gate decides.
#
# Uses: base

# --------------------------------------------------------------------------
# File tools, confined to the working directory

def split_rel(path):
    """Split a model-supplied path into components, or raise ValueError."""
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("path must be a non-empty string")
    if path.startswith("/") or path.startswith("~"):
        raise ValueError("absolute paths are not allowed; use a path relative to the working directory")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("'..' is not allowed in paths")
    return parts


def is_hidden(parts):
    """A path whose reads and writes always ask: a part starting with '.', or a name the sandbox hides as a secret
    (id_rsa, *.key and the like), so the file tools never take automatically what the sandbox keeps from a run."""
    return any(p.startswith(".") or secret_name(p) for p in parts)


class DiskError(ValueError):
    """A failure that reveals something about what is on disk (a symlink, a file where a directory was expected)."""


NO_HARD_LINKS = {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS}


class StaleApproval(ValueError):
    """The target no longer matches the approved snapshot."""


_UNCHECKED = object()


class Workdir:
    """Every operation walks the path one component at a time with O_NOFOLLOW,
    relative to a directory fd for the working directory."""

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def _open_dir(self, parts, create=False):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for p in parts:
                try:
                    nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(p, 0o777, dir_fd=fd)
                    nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as e:
                    if e.errno in (20, 40):  # ENOTDIR, ELOOP (symlink)
                        raise DiskError(f"'{p}' is a symlink or not a directory")
                    raise
                os.close(fd)
                fd = nfd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def stat(self, path):
        """lstat of a confined path, or None if it doesn't exist."""
        parts = split_rel(path)
        if not parts:
            return os.stat(self.root)
        try:
            dfd = self._open_dir(parts[:-1])
        except FileNotFoundError:
            return None
        try:
            return os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        finally:
            os.close(dfd)

    def read(self, path, cap=READ_CAP):
        parts = split_rel(path)
        if not parts:
            raise ValueError("that is a directory")
        dfd = self._open_dir(parts[:-1])
        try:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            except OSError as e:
                if e.errno == 40:
                    raise ValueError("symlinks are not followed")
                raise
        finally:
            os.close(dfd)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("not a regular file")
            if st.st_size > cap:
                raise ValueError(f"file is {st.st_size} bytes; the limit is {cap}")
            data = f.read(cap + 1)
        return self.decode(data, cap)

    @staticmethod
    def decode(data, cap):
        if len(data) > cap:
            raise ValueError(f"file exceeds the {cap}-byte limit")
        if b"\0" in data[:8192]:
            raise ValueError("binary files are refused")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("binary (non UTF-8) files are refused")

    @staticmethod
    def identity(st):
        return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    @classmethod
    def _snapshot(cls, dfd, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except FileNotFoundError:
            return None, None
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise DiskError("the target is not a regular file")
            if st.st_size > SNAPSHOT_CAP:
                raise DiskError(f"existing file exceeds the {SNAPSHOT_CAP}-byte overwrite limit")
            identity = cls.identity(st)
            data = f.read(SNAPSHOT_CAP + 1)
            if len(data) != st.st_size or identity != cls.identity(os.fstat(f.fileno())):
                raise StaleApproval("the target changed while being read")
        return (identity, hashlib.sha256(data).digest()), data

    def snapshot(self, path):
        parts = split_rel(path)
        if not parts:
            raise ValueError("that is a directory")
        try:
            dfd = self._open_dir(parts[:-1])
        except FileNotFoundError:
            return None, None
        try:
            return self._snapshot(dfd, parts[-1])
        finally:
            os.close(dfd)

    def list(self, path):
        parts = split_rel(path) if path not in ("", ".", "./") else []
        dfd = self._open_dir(parts)
        out = []
        try:
            with os.scandir(dfd) as it:
                for e in it:
                    if len(out) == LIST_CAP:
                        return sorted(out), True
                    st = e.stat(follow_symlinks=False)
                    kind = ("dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode)
                            else "symlink" if stat.S_ISLNK(st.st_mode) else "other")
                    out.append((e.name, kind, st.st_size if kind == "file" else None))
        finally:
            os.close(dfd)
        return sorted(out), False

    def walk(self, path):
        """The regular files under a directory, as relative paths, in order. Hidden names and symlinks are skipped
        (never followed), and directories are opened one component at a time like everything else here.
        A path naming one regular file gives just that file, unless its name is hidden."""
        start = split_rel(path) if path not in ("", ".", "./") else []
        if start:
            st = self.stat(path)                       # lstat: a symlink is not a regular file, and is refused below
            if st is not None and stat.S_ISREG(st.st_mode):
                if not is_hidden(start):
                    yield "/".join(start)
                return
        stack = [(start, self._open_dir(start))]
        try:
            while stack:
                parts, fd = stack.pop()
                files = []
                try:                                   # every fd is closed before anything is yielded
                    try:
                        with os.scandir(fd) as it:
                            entries = sorted((e.name, e.stat(follow_symlinks=False).st_mode) for e in it
                                             if not e.name.startswith("."))
                    except OSError:
                        entries = []
                    subdirs = [name for name, mode in entries if stat.S_ISDIR(mode)]
                    files = [name for name, mode in entries if stat.S_ISREG(mode)]
                    for name in reversed(subdirs):
                        try:
                            sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        except OSError:
                            continue
                        stack.append((parts + [name], sub))
                finally:
                    os.close(fd)
                for name in files:
                    yield "/".join(parts + [name])
        finally:
            for _, fd in stack:
                os.close(fd)

    def write(self, path, content, expected=_UNCHECKED):
        data = content.encode("utf-8")
        if len(data) > WRITE_CAP:
            raise ValueError(f"content is {len(data)} bytes; the limit is {WRITE_CAP}")
        parts = split_rel(path)
        if not parts:
            raise ValueError("a file name is required")
        dfd = self._open_dir(parts[:-1], create=True)
        name, stage = parts[-1], f".hoplon-tmp-{secrets.token_hex(6)}"
        try:
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                st = None
            if st is not None and not stat.S_ISREG(st.st_mode):
                raise StaleApproval("the target exists and is not a regular file")
            identity = self.identity(st) if st else None
            if expected is not _UNCHECKED and identity != (expected[0] if expected else None):
                raise StaleApproval("the target changed")
            os.mkdir(stage, 0o700, dir_fd=dfd)
            try:
                self._publish(dfd, stage, name, data, st, expected)
            finally:
                os.rmdir(stage, dir_fd=dfd)
        finally:
            os.close(dfd)
        return len(data)

    @staticmethod
    def _create_new(dfd, name, data):
        """Where the filesystem has no hard links (CIFS, some FUSE mounts): create the name only if it is still
        free, then write. Never replaces a file, but another program could see it half-written."""
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=dfd)
        except FileExistsError as e:
            raise StaleApproval("the target now exists") from e
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def _publish(self, dfd, stage, name, data, st, expected):
        staging_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
        try:
            directory = os.fstat(staging_fd)
            if directory.st_uid != os.getuid() or directory.st_mode & 0o077:
                raise StaleApproval("the staging directory is not private")
            fd = os.open("file", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666,
                         dir_fd=staging_fd)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    if st is not None:
                        os.fchmod(f.fileno(), stat.S_IMODE(st.st_mode))
                    os.fsync(f.fileno())
                if st is None:
                    try:
                        os.link("file", name, src_dir_fd=staging_fd, dst_dir_fd=dfd)
                    except FileExistsError as e:
                        raise StaleApproval("the target now exists") from e
                    except OSError as e:
                        if e.errno not in NO_HARD_LINKS:
                            raise
                        self._create_new(dfd, name, data)
                else:
                    if expected is not _UNCHECKED:
                        try:
                            actual, _ = self._snapshot(dfd, name)
                            latest = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                        except (OSError, DiskError) as e:
                            raise StaleApproval("the target changed") from e
                        if actual != expected or self.identity(latest) != expected[0]:
                            raise StaleApproval("the target changed")
                    os.rename("file", name, src_dir_fd=staging_fd, dst_dir_fd=dfd)
            finally:
                try:
                    os.unlink("file", dir_fd=staging_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(staging_fd)


def unified_diff(old, new, path):
    d = difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}")
    return "".join(x if x.endswith("\n") else x + "\n\\ No newline at end of file\n" for x in d)
