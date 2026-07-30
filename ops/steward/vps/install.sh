#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
UNIT_DIR="${HOME}/.config/systemd/user"
BIN_DIR="${HOME}/.local/bin"

install -d -m 700 "$UNIT_DIR" "$BIN_DIR"
install -m 644 "$SCRIPT_DIR/steward-heartbeat.service" "$UNIT_DIR/steward-heartbeat.service"
install -m 644 "$SCRIPT_DIR/steward-heartbeat.timer" "$UNIT_DIR/steward-heartbeat.timer"
install -m 755 "$SCRIPT_DIR/send-heartbeat.sh" "$BIN_DIR/steward-send-heartbeat"

systemctl --user daemon-reload
systemctl --user enable --now steward-heartbeat.timer
systemctl --user status steward-heartbeat.timer --no-pager
