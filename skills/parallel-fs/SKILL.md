# Parallel file systems (Lustre, GPFS and the like): work with them, not against them

Shared parallel file systems are fast for big sequential I/O and slow, for everyone, under metadata load. A site skill says which one is in use and where.

- **Metadata is the scarce part.** Every `stat`, `open` and `ls -l` goes to a shared metadata server. Avoid `ls -l` or `find` over huge directories and whole file systems, and `du` over a project tree: use the file system's quota tools (`lfs quota -h -u $USER`, `mmlsquota`) instead.
- **No small-file storms.** Thousands of tiny files, or many processes creating files in one directory, hurt everyone. Pack small outputs into one file per task (HDF5, a tar, appended records), and spread many files across subdirectories.
- **Big files, big writes.** Write in large blocks (MiB, not bytes), buffer output, and don't open and close a file per line.
- **Striping (Lustre):** large shared files benefit from striping over several targets (`lfs setstripe -c 4 DIR` before writing), small files do not (`-c 1`). Check with `lfs getstripe`. Don't change striping on directories others use without asking.
- **Node-local scratch** (`$TMPDIR`, `/tmp` or the site's local disk on a compute node) is better for many small temporary files: work there and copy results back at the end of the job.
- **Locks:** avoid many processes writing the same file unless the library is built for it (MPI-IO, parallel HDF5).
- **Python and conda environments** on a parallel file system load slowly at scale (thousands of small files per import): suggest a container image or an environment packed into one file where the site supports it.
