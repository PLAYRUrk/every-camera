#!/usr/bin/env bash
#
# Launch every-camera. Works the same by hand and under systemd.
#
#   ./run.sh --type asi
#   ./run.sh --type asi --config /etc/every-camera/asi.json
#   ./run.sh --gui
#
# Everything after the script name is passed straight to main.py, so this is a
# drop-in replacement for `python3 main.py …` and adds nothing to learn.
#
# What it does for you:
#   * runs from the program's own directory, so relative paths behave;
#   * picks the interpreter: $PYTHON, else a venv beside the checkout, else
#     python3 from PATH;
#   * sources env.sh if it exists, which is where machine-specific settings
#     belong (the PICAM SDK paths, a conda activation, a proxy). That file is
#     deliberately untracked: it describes this machine, not the program.
#
# The last line is `exec` on purpose — see the comment there.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

# Machine-local environment, if the operator wrote one. Copy env.sh.example to
# env.sh to get started; it is in .gitignore, so a station's paths never end up
# in a commit.
if [[ -f "$APP_DIR/env.sh" ]]; then
    # shellcheck disable=SC1091
    source "$APP_DIR/env.sh"
fi

# $PYTHON wins, then a venv next to the checkout, then whatever is on PATH.
if [[ -z "${PYTHON:-}" ]]; then
    for candidate in "$APP_DIR/venv/bin/python" "$APP_DIR/.venv/bin/python"; do
        if [[ -x "$candidate" ]]; then
            PYTHON="$candidate"
            break
        fi
    done
fi
PYTHON="${PYTHON:-$(command -v python3 || true)}"

# PYTHON may be a bare command name ("python3.11") rather than a path; resolve
# it against PATH, which env.sh may just have changed by activating a conda
# environment. An absolute path is still the safer thing to write there: a
# service does not inherit the login shell's PATH, so a name that resolves for
# the operator may resolve to something else, or to nothing, under systemd.
if [[ -n "$PYTHON" && ! -x "$PYTHON" ]]; then
    PYTHON="$(command -v "$PYTHON" || echo "$PYTHON")"
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "run.sh: no python3 found. Set PYTHON=/path/to/python3, or put one in" >&2
    echo "        env.sh, or create a venv at $APP_DIR/venv" >&2
    exit 1
fi

# exec, not a plain call: the shell replaces itself with Python instead of
# sitting between it and the terminal. That matters for stopping. A wrapper left
# in the middle would take SIGINT and SIGTERM itself and die, and Python would
# be orphaned or killed without ever running its shutdown — the closing dark
# frames and, on the ASI, the sensor warm-up from its operating temperature.
# With exec there is one process, and the signal lands where it is handled.
exec "$PYTHON" "$APP_DIR/main.py" "$@"
