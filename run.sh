#!/usr/bin/env bash
# Arguments go straight to main.py; --sentinel starts the watchdog instead.
#
# The watchdog starts here too because `python3` on CentOS 7 is 3.6, which has no
# dataclasses, so a unit naming the interpreter itself would fail.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

if [[ -f "$APP_DIR/env.sh" ]]; then
    # shellcheck disable=SC1091
    source "$APP_DIR/env.sh"
fi

if [[ -z "${PYTHON:-}" ]]; then
    for candidate in "$APP_DIR/venv/bin/python" "$APP_DIR/.venv/bin/python"; do
        if [[ -x "$candidate" ]]; then
            PYTHON="$candidate"
            break
        fi
    done
fi
PYTHON="${PYTHON:-$(command -v python3 || true)}"

# PYTHON may be a bare name, and env.sh may just have changed PATH. Under systemd
# PATH differs from the login shell, so an absolute path is safer.
if [[ -n "$PYTHON" && ! -x "$PYTHON" ]]; then
    PYTHON="$(command -v "$PYTHON" || echo "$PYTHON")"
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "run.sh: no python3 found. Set PYTHON=/path/to/python3, or put one in" >&2
    echo "        env.sh, or create a venv at $APP_DIR/venv" >&2
    exit 1
fi

TARGET="$APP_DIR/main.py"
if [[ "${1:-}" == "--sentinel" ]]; then
    TARGET="$APP_DIR/sentinel.py"
    shift
fi

# exec keeps one process: a wrapper in the middle would take SIGINT and SIGTERM
# itself, and Python would miss its shutdown, closing darks and sensor warm-up.
exec "$PYTHON" "$TARGET" "$@"
