# Working on a shared HPC cluster: login nodes, modules, file systems (any scheduler)

The user is on a shared cluster, usually on a login node, with environment modules. Others share every node,
file system and queue, so what you propose has to be fair to them too. For Slurm itself, the `slurm` skill
says how to submit and watch jobs; a site skill gives this site's facts and wins where they disagree.

The login node:

- It is for editing, compiling small things and submitting jobs. Don't propose anything there that runs for
  more than a minute or uses many cores or much memory: no parallel builds with a high `-j`, no test suites,
  no data processing. Propose it inside an allocation from the scheduler instead.

The environment comes from modules:

- Don't assume a compiler, MPI, CUDA or Python version. Propose `module list` to see what is loaded and
  `module avail NAME` (or `module spider NAME` with Lmod) to see what exists.
- Name exact versions in scripts (`module load gcc/12.2.0`), so a later default doesn't change the build.
- A build and the job that runs it need the same modules loaded.

File systems:

- Home directories are small and backed up; scratch is large, fast and often purged. Build and write big
  outputs in scratch (`$SCRATCH` or what the site provides), and keep source in home or a project space.
- Don't propose thousands of small files in one directory, or `find` / `du` over a whole shared file system.
- Before a large write, propose a quota check if the site has one (`quota -s`, `lfs quota -h -u $USER`).

Never propose changes to module or system configuration, or anything that needs root: say what an
administrator would need to do.
