# Slurm commands: which one to use

These read the cluster's live state; none of them changes anything. Prefer them to proposing `squeue` or `sinfo` yourself.

- **Why is my job not running?** `queue_user` (the user's own name) for its state and reason, then `job` for the details. A pending job's Reason says why it waits: Priority and Resources mean waiting will do it; a reason ending in Limit means it never will, as submitted.
- **Why did my job fail?** `jobs_history_user`, then read State and ExitCode. OUT_OF_MEMORY needs more memory, TIMEOUT more time, NODE_FAIL only a resubmit. The job's own log says more: ask the user to share it.
- **Is the cluster busy, or broken?** `partitions` for the summary, `down_nodes` for nodes that are down or drained, with their reasons; `node` for one node.
- **Whose jobs are in a partition?** `queue_partition`. `queue` lists every job: use it only when a narrower command doesn't answer the question.

Results are cached for 15 to 30 seconds, and the same command can't really run again sooner than a few seconds after the last time. Don't call the same command over and over to watch a job: say what to look for, and let the user ask again.

Ask the user for their user name if you don't know it. Never guess another user's.
