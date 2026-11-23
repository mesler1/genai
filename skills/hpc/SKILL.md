# Working on an HPC cluster

The user is on a shared cluster, usually on a login node, with Slurm and environment modules. Others share
every node, filesystem and queue, so what you propose has to be fair to them too.

Where things run:

- The login node is for editing, compiling small things and submitting jobs. Don't propose anything there
  that runs for more than a minute or uses many cores or much memory: no parallel builds with a high `-j`,
  no test suites, no data processing. Propose it inside an allocation instead.
- One command, interactively: `srun --ntasks=1 --cpus-per-task=4 --time=00:30:00 make -j4`. A shell on a
  compute node: `srun --pty --time=01:00:00 bash`. Anything long or unattended: a batch script and `sbatch`.
- Always give a `--time` limit, and ask for only the cores, memory and GPUs the work needs. Don't guess the
  partition, account or QOS: ask the user, or propose `sinfo -s` and `sacctmgr -nP show assoc user=$USER
  format=account,partition,qos` to find out.
- Before proposing a run inside an existing job, check it with `squeue --me`. To look at a running job's
  node, `srun --jobid=JOBID --overlap --pty bash`.

Batch scripts:

- Write them with `write_file`, then propose `sbatch script.sh`. Put every resource request in `#SBATCH`
  lines at the top: `--job-name`, `--time`, `--nodes`, `--ntasks`, `--cpus-per-task`, `--mem` (or
  `--mem-per-cpu`), and `--output=%x-%j.out` so each run's log is easy to find.
- Load modules inside the script, after `module purge`, so the job doesn't depend on the login shell.
- Start MPI programs with `srun`, not `mpirun`, unless the site says otherwise.
- Use `$SLURM_CPUS_PER_TASK` for thread counts (`export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK`), never a
  hard-coded number.
- For many similar runs, propose a job array (`#SBATCH --array=1-100%10`) rather than a loop of `sbatch`.

The environment comes from modules:

- Don't assume a compiler, MPI, CUDA or Python version. Propose `module list` to see what is loaded and
  `module avail NAME` (or `module spider NAME` with Lmod) to see what exists.
- Name exact versions in scripts (`module load gcc/12.2.0`), so a later default doesn't change the build.
- A build and the job that runs it need the same modules loaded.

Filesystems:

- Home directories are small and backed up; scratch is large, fast and often purged. Build and write big
  outputs in scratch (`$SCRATCH` or what the site provides), and keep source in home or a project space.
- Don't propose thousands of small files in one directory, or `find` / `du` over a whole shared filesystem.
- Before a large write, propose a quota check if the site has one (`quota -s`, `lfs quota -h -u $USER`).

When a job fails:

- Ask for the job's `.out` file and `sacct -j JOBID --format=JobID,State,ExitCode,Elapsed,MaxRSS,ReqMem,Timelimit`.
  `OUT_OF_MEMORY`, `TIMEOUT` and `NODE_FAIL` each need a different fix: more memory, more time or
  checkpointing, and a resubmit.

Never propose `scancel` without a specific job id the user has confirmed, and never `scancel -u $USER`.
Never propose changes to Slurm or module configuration, or anything that needs root: say what an
administrator would need to do.
