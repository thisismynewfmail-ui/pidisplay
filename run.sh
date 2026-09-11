#!/usr/bin/env bash
#
# Start the terminal.
#
# Usage:
#   ./run.sh                 run against the attached panel
#   ./run.sh --sim           mirror the panel in this terminal instead
#   ./run.sh --probe         report I2C addresses, keyboards and models
#   ./run.sh --self-test     draw a wiring test pattern and exit
#
# Any other arguments are passed through to the program; see --help.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python3}"
if [[ -x "$HERE/.venv/bin/python" ]]; then
    PYTHON="$HERE/.venv/bin/python"
fi

if ! command -v "$PYTHON" >/dev/null 2>&1 && [[ ! -x "$PYTHON" ]]; then
    echo "error: no python3 found. Run ./install.sh first." >&2
    exit 1
fi

# Put a locally built llama.cpp on the path without requiring installation.
for candidate in \
    "$HERE/vendor/llama.cpp/build/bin" \
    "$HERE/llama.cpp/build/bin" \
    "$HOME/llama.cpp/build/bin"
do
    if [[ -x "$candidate/llama-server" ]]; then
        export PATH="$candidate:$PATH"
        break
    fi
done

# Reading /dev/input/event* requires membership of the 'input' group. If the
# group was granted in this session's absence the membership is not yet active,
# so say so rather than letting the program report "no keyboard".
if [[ -z "${APERTURE_SKIP_CHECKS:-}" && " $* " != *" --sim "* && " $* " != *" --probe "* ]]; then
    if [[ -d /dev/input ]] && ! id -nG "$USER" 2>/dev/null | grep -qw input; then
        if [[ "$(id -u)" -ne 0 ]]; then
            echo "warning: $USER is not in the 'input' group, so keyboards may" >&2
            echo "         not be readable. Fix with:" >&2
            echo "           sudo usermod -aG input $USER   # then log out and back in" >&2
            echo "" >&2
        fi
    fi
fi

exec "$PYTHON" -m aperture "$@"
