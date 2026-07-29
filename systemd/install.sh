#!/usr/bin/env bash
#
# Install every-camera as a systemd system service.
#
#   sudo systemd/install.sh asi
#   sudo systemd/install.sh asi --user alex --python /usr/bin/python3
#   sudo systemd/install.sh asi --uninstall
#
# What it does, and nothing more:
#   * copies systemd/every-camera@.service to /etc/systemd/system/ with the
#     paths of this checkout filled in;
#   * copies config.json to /etc/every-camera/<type>.json if that file does not
#     exist yet, so the service has a config of its own to edit;
#   * enables and starts every-camera@<type>.
#
# All of it is reversible: --uninstall stops the service, disables the autostart
# and removes the unit. The config in /etc/every-camera and the archive are left
# alone — losing a station's programme to a flag is not a thing this script does.
#
# Running the program by hand is unaffected: `python3 main.py --type asi` keeps
# working against the checkout's own config.json exactly as before.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$APP_DIR/systemd/every-camera@.service"
UNIT_DIR=/etc/systemd/system
CONFIG_DIR=/etc/every-camera
CAMERA=""
PYTHON=""
OWNER=""
DRY_RUN=0
UNINSTALL=0

die() { echo "error: $*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: sudo $0 <camera-type> [--user NAME] [--python PATH] [--config-dir DIR]

  camera-type    asi | japan | sentry | cannon | sptt | infra
  --user NAME    whose home receives the logs and status files
                 (default: the user invoking sudo)
  --python PATH  interpreter to run (default: the first python3 on PATH)
  --config-dir   where the service's config lives (default: $CONFIG_DIR)
  --dry-run      print the unit that would be written and change nothing
  --uninstall    stop and disable the service and remove the unit file
                 (the config and the archive are kept)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --user) OWNER="${2:-}"; shift 2 ;;
        --python) PYTHON="${2:-}"; shift 2 ;;
        --config-dir) CONFIG_DIR="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -*) die "unknown option $1" ;;
        *) [[ -n "$CAMERA" ]] && die "one camera type at a time"; CAMERA="$1"; shift ;;
    esac
done

[[ -n "$CAMERA" ]] || { usage; exit 1; }
case "$CAMERA" in
    asi|japan|sentry|cannon|sptt|infra) ;;
    *) die "unknown camera type '$CAMERA'" ;;
esac
[[ $DRY_RUN -eq 1 || $EUID -eq 0 ]] || die "run me with sudo — this writes to $UNIT_DIR"

if [[ $UNINSTALL -eq 1 ]]; then
    # Stopping goes through the normal path, so the camera still shoots its
    # closing darks and warms the sensor before the unit disappears.
    echo "stopping every-camera@$CAMERA (closing darks and warm-up run first)…"
    systemctl disable --now "every-camera@$CAMERA" || true
    # The unit file is shared by every instance, so it only goes when the last
    # one does.
    remaining="$(systemctl list-units --all --no-legend 'every-camera@*' 2>/dev/null | wc -l)"
    if [[ "$remaining" -eq 0 ]]; then
        rm -f "$UNIT_DIR/every-camera@.service"
        echo "removed $UNIT_DIR/every-camera@.service"
    else
        echo "kept $UNIT_DIR/every-camera@.service — other instances still use it"
    fi
    systemctl daemon-reload
    echo
    echo "Done. Kept, on purpose:"
    echo "  $CONFIG_DIR/$CAMERA.json   (the station's programme)"
    echo "  the frame archive and $HOME/.every_camera/logs/"
    exit 0
fi

[[ -f "$TEMPLATE" ]] || die "template not found: $TEMPLATE"

# SUDO_USER is who called sudo; falling back to root would silently put the
# logs somewhere the operator does not look.
OWNER="${OWNER:-${SUDO_USER:-}}"
[[ -n "$OWNER" ]] || die "cannot tell whose home to use — pass --user NAME"
USER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
[[ -n "$USER_HOME" ]] || die "no such user: $OWNER"

PYTHON="${PYTHON:-$(command -v python3 || true)}"
[[ -x "$PYTHON" ]] || die "python3 not found — pass --python /path/to/python3"

# The service gets its own config so that editing the station's programme does
# not mean editing a file inside a git checkout.
TARGET_CONFIG="$CONFIG_DIR/$CAMERA.json"
if [[ $DRY_RUN -eq 1 ]]; then
    echo "--- would write $UNIT_DIR/every-camera@.service ---"
    sed -e "s|@APP_DIR@|$APP_DIR|g" \
        -e "s|@PYTHON@|$PYTHON|g" \
        -e "s|@CONFIG_DIR@|$CONFIG_DIR|g" \
        -e "s|@USER_HOME@|$USER_HOME|g" \
        "$TEMPLATE"
    echo "--- would use config $TARGET_CONFIG (copied from $APP_DIR/config.json if absent) ---"
    echo "--- would run: systemctl enable --now every-camera@$CAMERA ---"
    exit 0
fi

mkdir -p "$CONFIG_DIR"
if [[ -f "$TARGET_CONFIG" ]]; then
    echo "keeping the existing $TARGET_CONFIG"
elif [[ -f "$APP_DIR/config.json" ]]; then
    cp "$APP_DIR/config.json" "$TARGET_CONFIG"
    chown "$OWNER" "$TARGET_CONFIG"
    echo "copied config.json -> $TARGET_CONFIG"
else
    die "no $APP_DIR/config.json to copy — run the setup wizard first: \
python3 main.py --type $CAMERA --setup"
fi

UNIT="$UNIT_DIR/every-camera@.service"
sed -e "s|@APP_DIR@|$APP_DIR|g" \
    -e "s|@PYTHON@|$PYTHON|g" \
    -e "s|@CONFIG_DIR@|$CONFIG_DIR|g" \
    -e "s|@USER_HOME@|$USER_HOME|g" \
    "$TEMPLATE" > "$UNIT"
chmod 644 "$UNIT"
chmod +x "$APP_DIR/run.sh"
echo "installed $UNIT"

systemctl daemon-reload
systemctl enable --now "every-camera@$CAMERA"

cat <<EOF

every-camera@$CAMERA is enabled and running; it will come back after a reboot.

  systemctl status every-camera@$CAMERA
  journalctl -u every-camera@$CAMERA -f
  systemctl stop every-camera@$CAMERA     # closing darks and warm-up run first

Config:  $TARGET_CONFIG
Logs:    $USER_HOME/.every_camera/logs/

The service runs $APP_DIR/run.sh — the same launcher you can use by hand:
  $APP_DIR/run.sh --type $CAMERA

If the camera needs the PICAM SDK, put its environment in $APP_DIR/env.sh
(see env.sh.example), then: systemctl restart every-camera@$CAMERA

To undo all of this:  sudo $0 $CAMERA --uninstall
EOF
