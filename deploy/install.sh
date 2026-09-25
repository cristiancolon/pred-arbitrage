#!/bin/sh
# Install arbscan as a systemd user service. Works from wherever the repo is cloned;
# re-run it after moving the repo.
set -eu

cd "$(dirname "$0")/.."
repo="$(pwd -P)"
case "$repo" in
    *[\#\&]*) echo "error: the repo path must not contain '#' or '&': $repo" >&2; exit 1 ;;
esac

if [ ! -x .venv/bin/arbscan ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .

mkdir -p "$HOME/.config/systemd/user"
# Older versions used a separate refresh timer; `arbscan serve` schedules it now.
if systemctl --user list-unit-files arbscan-refresh.timer >/dev/null 2>&1; then
    systemctl --user disable --now arbscan-refresh.timer 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/arbscan-refresh.timer" "$HOME/.config/systemd/user/arbscan-refresh.service"
fi
sed "s#@REPO_DIR@#$repo#g" deploy/arbscan.service > "$HOME/.config/systemd/user/arbscan.service"
systemctl --user daemon-reload
systemctl --user enable arbscan.service
systemctl --user restart arbscan.service
# Keep user services running after you log out. Some systems need sudo for this.
if ! loginctl enable-linger "$USER" 2>/dev/null; then
    echo "note: couldn't enable linger; run 'sudo loginctl enable-linger $USER' so the service survives logout"
fi

port=$(sed -n 's/^web_port *= *\([0-9]*\).*/\1/p' config.toml 2>/dev/null)
echo "installed. dashboard: http://$(hostname -I 2>/dev/null | cut -d' ' -f1):${port:-8787}/"
echo "logs: journalctl --user -u arbscan -f"
