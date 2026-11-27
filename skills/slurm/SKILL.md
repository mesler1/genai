# Slurm: running work on a Slurm cluster (generic, true on any site)

The user is on a cluster scheduled by Slurm, usually on a login node. This skill holds only what is true
on every Slurm site. A site skill, if one is on, gives this site's partitions, accounts, QOS and rules,
and wins where the two disagree.

Where things run:

- The login node is for editing, compiling small things and submitting jobs. Propose anything that runs for
  more than a minute or uses many cores or much memory inside an allocation instead.
- One command, interactively: `srun --ntasks=1 --cpus-per-task=4 --time=00:30:00 make -j4`. A shell on a
  compute node: `srun --pty --time=01:00:00 bash`. Anything long or unattended: a batch script and `sbatch`.
- Always give a `--time` limit, and ask for only the cores, memory and GPUs the work needs.
- Don't guess the partition, account or QOS. Without a site skill that names them, ask the user, or
  propose `sinfo -s` and `sacctmgr -nP show assoc user=$USER format=account,partition,qos` to find out.
- Before proposing a run inside an existing job, check it with `squeue --me`. To look at a running job's
  node: `srun --jobid=JOBID --overlap --pty bash`.

Batch scripts:

- Write them with `write_file`, then propose `sbatch script.sh`. Put every resource request in `#SBATCH`
  lines at the top: `--job-name`, `--time`, `--nodes`, `--ntasks`, `--cpus-per-task`, `--mem` (or
  `--mem-per-cpu`), and `--output=%x-%j.out` so each run's log is easy to find.
- Load modules inside the script, after `module purge`, so the job doesn't depend on the login shell.
- Start MPI programs with `srun`, not `mpirun`, unless the site says otherwise.
- Use `$SLURM_CPUS_PER_TASK` for thread counts (`export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK`), never a
  hard-coded number.
- For many similar runs, propose a job array (`#SBATCH --array=1-100%10`) rather than a loop of `sbatch`.
- Chain dependent jobs with `--dependency=afterok:JOBID` rather than a script that sleeps and polls.

Watching jobs:

- `squeue --me` for what is queued or running, `sacct -j JOBID` once it has ended. Don't propose a loop
  that runs `squeue` every few seconds: it loads the controller for everyone.
- Pending reasons (`squeue --me -o "%.10i %.9P %.20j %.8T %.20R"`): `Priority` and `Resources` mean wait;
  `QOSMaxCpuPerUserLimit`, `AssocGrpCPULimit` and similar mean a limit, which waiting won't fix.

When a job fails:

- Ask for the job's `.out` file and `sacct -j JOBID --format=JobID,State,ExitCode,Elapsed,MaxRSS,ReqMem,Timelimit`.
  `OUT_OF_MEMORY`, `TIMEOUT` and `NODE_FAIL` each need a different fix: more memory, more time or
  checkpointing, and a resubmit.

Never propose `scancel` without a specific job id the user has confirmed, and never `scancel -u $USER`.
Never propose changes to Slurm's configuration, or anything that needs an administrator: say what one
would need to do.
