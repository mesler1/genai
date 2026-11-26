#!/bin/bash
# Build the test cluster image and open a shell in it as the user "hpc". The hoplon checkout is
# mounted read-only at /hoplon, and your hoplon config (if any) is NOT mounted: set one up inside.
# Usage: tests/slurm/run.sh [command...]
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
# chroot isolation: without a systemd user session, rootless podman can't run build steps otherwise.
podman build -q --isolation chroot -t hoplon-slurm "$here" >/dev/null
exec podman run --rm -it --hostname slurmbox -v "$repo:/hoplon:ro,Z" hoplon-slurm "$@"
