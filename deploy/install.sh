#!/bin/sh
# Install arbscan as a systemd user service on the Pi. Run from the repo root,
# which must live at ~/pred-arbitrage (or edit WorkingDirectory in the unit).
set -eu

cd "$(dirname "$0")/.."
[ "$(pwd)" = "$HOME/pred-arbitrage" ] || echo "warning: the unit expects the repo at ~/pred-arbitrage (it is at $(pwd))"

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
cp deploy/arbscan.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable arbscan.service
systemctl --user restart arbscan.service
# Keep user services running after you log out.
loginctl enable-linger "$USER"

port=$(sed -n 's/^web_port *= *\([0-9]*\).*/\1/p' config.toml 2>/dev/null)
echo "installed. dashboard: http://$(hostname -I 2>/dev/null | cut -d' ' -f1):${port:-8787}/"
echo "logs: journalctl --user -u arbscan -f"
