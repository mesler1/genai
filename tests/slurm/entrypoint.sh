#!/bin/bash
# Start munge and both Slurm daemons, wait for the node to come up, then run the command.
set -e
runuser -u munge -- /usr/sbin/munged
/usr/sbin/slurmctld
/usr/sbin/slurmd
for _ in $(seq 30); do
  sinfo -h -o %t 2>/dev/null | grep -qE '^(idle|mix|alloc)' && break
  sleep 1
done
sinfo
exec "$@"
