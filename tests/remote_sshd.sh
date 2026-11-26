#!/usr/bin/env bash
# A SLES 15 SP6 sshd in a container, for the real-ssh test of hoplon-tui's remote mode (tests/test_remote.py,
# TestRealSsh). Users in it: `me`, who logs in with a key made here, and `svc`, whom `me` may become with
# `sudo -n -u svc` without a password. It has python3.11 only (SLES 15's python3 is 3.6), like the real hosts.
#
#   tests/remote_sshd.sh up      build the image (localhost/hoplon-sshd-test) if needed, start it on 127.0.0.1:2222
#   tests/remote_sshd.sh down    stop it
#   tests/remote_sshd.sh test    up, run the real-ssh test against it, down
#
# Nothing is built by the normal test suite: TestRealSsh is skipped unless podman, the image and the running
# container are all there. State (the login key) is in $HOPLON_TEST_SSH_DIR, default $XDG_RUNTIME_DIR/hoplon-sshd-test.
# Usage for a manual try: hoplon-tui --remote me@127.0.0.1 --as svc --dir /tmp -- -p 2222 -i KEY -o StrictHostKeyChecking=no
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
dir=${HOPLON_TEST_SSH_DIR:-${XDG_RUNTIME_DIR:-/tmp}/hoplon-sshd-test}
port=${HOPLON_TEST_SSH_PORT:-2222}
image=localhost/hoplon-sshd-test
name=hoplon-sshd-test

up() {
  mkdir -p "$dir"
  chmod 700 "$dir"
  [ -f "$dir/key" ] || ssh-keygen -q -t ed25519 -N '' -C hoplon-sshd-test -f "$dir/key"
  if ! podman image exists "$image"; then
    cp "$dir/key.pub" "$dir/authorized_keys"
    cat > "$dir/Containerfile" <<'EOF'
FROM registry.suse.com/bci/bci-base:15.6
RUN zypper -n install --no-recommends openssh-server python311 sudo shadow && zypper -n clean -a \
 && ssh-keygen -A \
 && useradd -m -s /bin/bash me && useradd -m -s /bin/bash svc \
 && usermod -p '*' me && usermod -p '*' svc \
 && echo 'me ALL=(svc) NOPASSWD: ALL' > /etc/sudoers.d/me && chmod 440 /etc/sudoers.d/me \
 && mkdir -p /home/me/.ssh && chmod 700 /home/me/.ssh
COPY authorized_keys /home/me/.ssh/authorized_keys
RUN chown -R me:users /home/me/.ssh && chmod 600 /home/me/.ssh/authorized_keys \
 && printf 'PasswordAuthentication no\nAllowTcpForwarding yes\n' > /etc/ssh/sshd_config.d/hoplon.conf
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
EOF
    # chroot isolation: without a systemd user session, rootless podman can't run build steps otherwise.
    podman build -q --isolation chroot -t "$image" -f "$dir/Containerfile" "$dir" >/dev/null
  fi
  podman rm -f "$name" >/dev/null 2>&1 || true
  podman run -d --rm --name "$name" -p "127.0.0.1:$port:22" "$image" >/dev/null
  for _ in $(seq 50); do
    ssh -p "$port" -i "$dir/key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR me@127.0.0.1 true 2>/dev/null && { echo "up: ssh -p $port -i $dir/key me@127.0.0.1"; return; }
    sleep 0.2
  done
  podman logs "$name" >&2 || true
  echo "remote_sshd.sh: the container did not accept a login" >&2
  exit 1
}

down() { podman rm -f "$name" >/dev/null 2>&1 || true; }

case ${1:-} in
  up) up ;;
  down) down ;;
  test)
    up
    trap down EXIT
    HOPLON_TEST_SSH_PORT=$port HOPLON_TEST_SSH_KEY=$dir/key python3 -m unittest -v tests.test_remote.TestRealSsh
    ;;
  *) echo "usage: $0 up|down|test" >&2; exit 2 ;;
esac
