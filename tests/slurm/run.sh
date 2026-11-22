#!/bin/bash
# Build the test cluster image and open a shell in it as the user "hpc". The genai checkout is
# mounted read-only at /genai, and your genai config (if any) is NOT mounted: set one up inside.
# Usage: tests/slurm/run.sh [command...]
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
# chroot isolation: without a systemd user session, rootless podman can't run build steps otherwise.
podman build -q --isolation chroot -t genai-slurm "$here" >/dev/null
exec podman run --rm -it --hostname slurmbox -v "$repo:/genai:ro,Z" genai-slurm "$@"
