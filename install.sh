#!/usr/bin/env bash
#
# Installer for the Aperture Terminal on Raspberry Pi OS (64-bit).
#
# What it does, in order:
#   1. installs system packages
#   2. enables I2C and raises the bus to 400 kHz
#   3. puts you in the groups needed to read the panel and the keyboards
#   4. creates a virtualenv and installs the one Python dependency
#   5. optionally builds llama.cpp from source
#   6. optionally installs a systemd unit so it starts at boot
#
# Every step is idempotent: running this again after a failure is safe, and
# running it after an upgrade is the supported way to re-apply changes.
#
# Usage:
#   ./install.sh                    interactive
#   ./install.sh --yes              accept every default, no prompts
#   ./install.sh --with-llama       build llama.cpp (takes 10-25 min on a Pi 5)
#   ./install.sh --no-llama         skip building llama.cpp
#   ./install.sh --service          install and enable the systemd unit
#   ./install.sh --no-apt           skip apt entirely (offline installs)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV="$HERE/.venv"
LLAMA_DIR="$HERE/vendor/llama.cpp"
MODELS_DIR="$HERE/models"

ASSUME_YES=0
BUILD_LLAMA=""
INSTALL_SERVICE=""
RUN_APT=1

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'

step()  { printf '\n%s==>%s %s%s\n' "$BOLD$GREEN" "$RESET" "$BOLD" "$*$RESET"; }
info()  { printf '    %s\n' "$*"; }
warn()  { printf '%s !  %s%s\n' "$YELLOW" "$*" "$RESET" >&2; }
die()   { printf '%s !! %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }

ask() {
    # ask <question> <default y|n>
    local question="$1" default="${2:-y}" reply
    if [[ $ASSUME_YES -eq 1 ]]; then return 0; fi
    if [[ ! -t 0 ]]; then
        # Non-interactive with no explicit flag: take the default rather than
        # hanging on a prompt nobody can answer.
        [[ "$default" == "y" ]]
        return
    fi
    local hint="[Y/n]"; [[ "$default" == "n" ]] && hint="[y/N]"
    read -r -p "    $question $hint " reply || reply=""
    reply="${reply:-$default}"
    [[ "${reply,,}" == y* ]]
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)       ASSUME_YES=1 ;;
        --with-llama)   BUILD_LLAMA=1 ;;
        --no-llama)     BUILD_LLAMA=0 ;;
        --service)      INSTALL_SERVICE=1 ;;
        --no-service)   INSTALL_SERVICE=0 ;;
        --no-apt)       RUN_APT=0 ;;
        -h|--help)      awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
        *)              die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

if [[ "$(id -u)" -eq 0 && -z "${SUDO_USER:-}" ]]; then
    warn "Running as root. The terminal is meant to run as an ordinary user;"
    warn "group changes below will apply to root only."
fi
TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
TARGET_HOME="${TARGET_HOME:-$HOME}"

SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
    command -v sudo >/dev/null 2>&1 || die "sudo is required but not installed"
    SUDO="sudo"
fi

printf '%s\n' "$BOLD"
cat <<'BANNER'
  ###   ####  #### ###  ###  #  # ###   ####
  #  #  #  #  #    #  #  #   #  # #  #  #
  ###   ####  ###  ###   #   #  # ###   ###
  #  #  #     #    # #   #   #  # #  #  #
  #  #  #     #### #  #  #   ####  #  #  ####
                              T E R M I N A L
BANNER
printf '%s' "$RESET"
info "installing into $HERE for user $TARGET_USER"

# --------------------------------------------------------------------------
step "Checking the host"
# --------------------------------------------------------------------------
BOARD="unknown"
if [[ -r /proc/device-tree/model ]]; then
    BOARD="$(tr -d '\0' < /proc/device-tree/model)"
fi
info "board:  $BOARD"
info "kernel: $(uname -srm)"

case "$BOARD" in
    *"Raspberry Pi 5"*) ;;
    *"Raspberry Pi"*)   warn "This targets the Pi 5; other models work but are slower." ;;
    *)                  warn "Not a Raspberry Pi. Hardware steps will be skipped where they do not apply." ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
    die "python3 not found"
fi
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
info "python: $PYVER"
python3 - <<'PY' || die "python 3.9 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY

# --------------------------------------------------------------------------
step "Installing system packages"
# --------------------------------------------------------------------------
PACKAGES=(
    python3 python3-venv python3-pip
    i2c-tools                 # i2cdetect, for diagnosing the panel
    bluez                     # bluetoothctl, for pairing a keyboard
    network-manager           # nmcli, for the Wi-Fi menu
)
BUILD_PACKAGES=(git build-essential cmake ccache libcurl4-openssl-dev)

if [[ $RUN_APT -eq 1 ]] && command -v apt-get >/dev/null 2>&1; then
    MISSING=()
    for package in "${PACKAGES[@]}"; do
        dpkg -s "$package" >/dev/null 2>&1 || MISSING+=("$package")
    done
    if [[ ${#MISSING[@]} -gt 0 ]]; then
        info "installing: ${MISSING[*]}"
        $SUDO apt-get update -qq
        $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MISSING[@]}"
    else
        info "all present"
    fi
else
    info "skipped"
fi

# --------------------------------------------------------------------------
step "Enabling I2C"
# --------------------------------------------------------------------------
CONFIG_TXT=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f "$candidate" ]] && { CONFIG_TXT="$candidate"; break; }
done

NEEDS_REBOOT=0
if [[ -n "$CONFIG_TXT" ]]; then
    info "firmware config: $CONFIG_TXT"

    if command -v raspi-config >/dev/null 2>&1; then
        $SUDO raspi-config nonint do_i2c 0 || warn "raspi-config could not enable I2C"
    fi

    if ! grep -qE '^\s*dtparam=i2c_arm=on' "$CONFIG_TXT"; then
        info "enabling dtparam=i2c_arm"
        echo 'dtparam=i2c_arm=on' | $SUDO tee -a "$CONFIG_TXT" >/dev/null
        NEEDS_REBOOT=1
    fi

    # 400 kHz rather than the 100 kHz default. A full repaint of the panel is
    # ~45 ms at 100 kHz and ~11 ms at 400 kHz; every animation in the program
    # is budgeted against the faster figure. Every PCF8574 backpack supports
    # it -- the part is rated to 400 kHz.
    if grep -qE '^\s*dtparam=i2c_arm_baudrate=' "$CONFIG_TXT"; then
        CURRENT="$(grep -oE 'i2c_arm_baudrate=[0-9]+' "$CONFIG_TXT" | tail -1 | cut -d= -f2)"
        info "bus speed already set to ${CURRENT} Hz"
    else
        info "raising the bus to 400 kHz"
        echo 'dtparam=i2c_arm_baudrate=400000' | $SUDO tee -a "$CONFIG_TXT" >/dev/null
        NEEDS_REBOOT=1
    fi

    if ! grep -qE '^\s*i2c[-_]dev' /etc/modules 2>/dev/null; then
        echo 'i2c-dev' | $SUDO tee -a /etc/modules >/dev/null
    fi
    $SUDO modprobe i2c-dev 2>/dev/null || true
else
    info "no Raspberry Pi firmware config found; skipping"
fi

# --------------------------------------------------------------------------
step "Setting group membership"
# --------------------------------------------------------------------------
# i2c  -> /dev/i2c-1, the panel
# input-> /dev/input/event*, every keyboard
# bluetooth -> pairing over D-Bus without root
GROUPS_NEEDED=(i2c input bluetooth)
CHANGED_GROUPS=()
for group in "${GROUPS_NEEDED[@]}"; do
    if ! getent group "$group" >/dev/null 2>&1; then
        info "group '$group' does not exist on this system; skipping"
        continue
    fi
    if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx "$group"; then
        info "$TARGET_USER is already in '$group'"
    else
        $SUDO usermod -aG "$group" "$TARGET_USER"
        CHANGED_GROUPS+=("$group")
        info "added $TARGET_USER to '$group'"
    fi
done

# --------------------------------------------------------------------------
step "Creating the Python environment"
# --------------------------------------------------------------------------
if [[ ! -d "$VENV" ]]; then
    python3 -m venv --system-site-packages "$VENV"
    info "created $VENV"
else
    info "reusing $VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip setuptools wheel 2>/dev/null || true
if "$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"; then
    info "installed: $(sed -n 's/^smbus2.*/smbus2/p' "$HERE/requirements.txt" | head -1)"
else
    warn "pip could not install smbus2."
    warn "With no network, install the distribution package instead:"
    warn "    sudo apt-get install python3-smbus2"
fi

mkdir -p "$MODELS_DIR"
if [[ ! -f "$MODELS_DIR/README.md" ]]; then
    cat > "$MODELS_DIR/README.md" <<'MODELS'
Put GGUF model files in this directory.

They are found automatically, including in subdirectories, and appear in the
model picker (F3). For a multi-part model, place every shard here; only the
first (`-00001-of-*.gguf`) is listed, and llama.cpp loads the rest itself.

Sizing, for a Raspberry Pi 5 with 8 GB:

    1-2 B parameters, Q4_K_M   fast, roughly 8-15 tokens/second
    3-4 B parameters, Q4_K_M   comfortable, roughly 4-7 tokens/second
    7-8 B parameters, Q4_K_M   usable but slow, roughly 2-3 tokens/second

Leave at least 1.5 GB free beyond the file size for the KV cache and the rest
of the system. The context setting is what spends that: 4096 tokens is a
reasonable default, and the terminal will refuse to exceed what the model was
actually trained for.
MODELS
fi
info "models directory: $MODELS_DIR"

# --------------------------------------------------------------------------
step "llama.cpp"
# --------------------------------------------------------------------------
EXISTING_LLAMA="$(command -v llama-server || true)"
if [[ -x "$LLAMA_DIR/build/bin/llama-server" ]]; then
    EXISTING_LLAMA="$LLAMA_DIR/build/bin/llama-server"
fi

if [[ -n "$EXISTING_LLAMA" ]]; then
    info "found: $EXISTING_LLAMA"
    if [[ "$BUILD_LLAMA" == "1" ]]; then
        info "rebuilding anyway, as requested"
    else
        BUILD_LLAMA=0
    fi
fi

if [[ -z "$BUILD_LLAMA" ]]; then
    info "llama.cpp is the inference engine. Building it from source takes"
    info "roughly 10 to 25 minutes on a Pi 5."
    if ask "Build llama.cpp now?" y; then BUILD_LLAMA=1; else BUILD_LLAMA=0; fi
fi

if [[ "$BUILD_LLAMA" == "1" ]]; then
    if [[ $RUN_APT -eq 1 ]] && command -v apt-get >/dev/null 2>&1; then
        MISSING=()
        for package in "${BUILD_PACKAGES[@]}"; do
            dpkg -s "$package" >/dev/null 2>&1 || MISSING+=("$package")
        done
        if [[ ${#MISSING[@]} -gt 0 ]]; then
            info "installing build tools: ${MISSING[*]}"
            $SUDO apt-get update -qq
            $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MISSING[@]}"
        fi
    fi

    mkdir -p "$(dirname "$LLAMA_DIR")"
    if [[ -d "$LLAMA_DIR/.git" ]]; then
        info "updating existing checkout"
        git -C "$LLAMA_DIR" fetch --depth 1 origin master && \
            git -C "$LLAMA_DIR" reset --hard origin/master
    else
        info "cloning ggml-org/llama.cpp"
        git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
    fi

    info "configuring"
    # Server on, tests and extra tools off: nothing here needs them, and they
    # are a large share of the build time on a four-core machine.
    cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DLLAMA_BUILD_SERVER=ON \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_CURL=ON \
        -DGGML_NATIVE=ON

    JOBS="$(nproc)"
    info "building with $JOBS jobs (this is the slow part)"
    cmake --build "$LLAMA_DIR/build" --config Release -j "$JOBS" --target llama-server

    if [[ -x "$LLAMA_DIR/build/bin/llama-server" ]]; then
        info "built: $LLAMA_DIR/build/bin/llama-server"
        "$LLAMA_DIR/build/bin/llama-server" --version 2>&1 | head -1 | sed 's/^/    /'
    else
        die "the build finished but llama-server is missing"
    fi
else
    if [[ -z "$EXISTING_LLAMA" ]]; then
        warn "No llama-server. The terminal will start, report the engine as"
        warn "offline, and let you fix it from the settings menu. To build it"
        warn "later:  ./install.sh --with-llama"
    fi
fi

# --------------------------------------------------------------------------
step "Start at boot"
# --------------------------------------------------------------------------
UNIT_PATH=/etc/systemd/system/aperture-terminal.service
if [[ -z "$INSTALL_SERVICE" ]]; then
    if ask "Start the terminal automatically at boot?" n; then
        INSTALL_SERVICE=1
    else
        INSTALL_SERVICE=0
    fi
fi

if [[ "$INSTALL_SERVICE" == "1" ]]; then
    $SUDO tee "$UNIT_PATH" >/dev/null <<UNIT
[Unit]
Description=Aperture Terminal (local chat on a 20x4 character display)
After=multi-user.target bluetooth.service NetworkManager.service
Wants=bluetooth.service

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$HERE
ExecStart=$HERE/run.sh
Environment=APERTURE_SKIP_CHECKS=1
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5

# The panel keeps its last image until it loses power, so give the program
# time to write its closing frame rather than killing it mid-refresh.
KillSignal=SIGTERM
TimeoutStopSec=20

# Reading /dev/input/event* and /dev/i2c-1 is what this needs; nothing else.
SupplementaryGroups=input i2c bluetooth
NoNewPrivileges=yes
ProtectSystem=full
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable aperture-terminal.service >/dev/null
    info "installed and enabled $UNIT_PATH"
    info "control it with: sudo systemctl {start,stop,status} aperture-terminal"
else
    info "skipped"
fi

# --------------------------------------------------------------------------
step "Checking the panel"
# --------------------------------------------------------------------------
if [[ -e /dev/i2c-1 ]]; then
    if command -v i2cdetect >/dev/null 2>&1; then
        FOUND="$(i2cdetect -y 1 2>/dev/null | awk 'NR>1 {for (i=2;i<=NF;i++) if ($i ~ /^[0-9a-f]{2}$/) print $i}' | tr '\n' ' ')"
        if [[ -n "$FOUND" ]]; then
            info "devices responding on bus 1: $FOUND"
            case "$FOUND" in
                *27*|*3f*) info "that looks like a PCF8574 display backpack" ;;
                *) warn "no address typical of a display backpack (0x27 or 0x3F)" ;;
            esac
        else
            warn "nothing responded on I2C bus 1."
            warn "Check the wiring against the table in README.md -- the two"
            warn "most common faults are SDA and SCL swapped, and a missing"
            warn "ground between the panel and the Pi."
        fi
    fi
else
    warn "/dev/i2c-1 does not exist yet; a reboot is needed."
    NEEDS_REBOOT=1
fi

# --------------------------------------------------------------------------
step "Done"
# --------------------------------------------------------------------------
echo
if [[ $NEEDS_REBOOT -eq 1 ]]; then
    printf '%s    Reboot before first use:  sudo reboot%s\n\n' "$BOLD$YELLOW" "$RESET"
fi
if [[ ${#CHANGED_GROUPS[@]} -gt 0 ]]; then
    printf '%s    Group membership changed (%s).%s\n' "$YELLOW" "${CHANGED_GROUPS[*]}" "$RESET"
    printf '%s    Log out and back in, or reboot, before the keyboard will work.%s\n\n' "$YELLOW" "$RESET"
fi

cat <<NEXT
    Next steps:

      1. Put a GGUF model in ${DIM}$MODELS_DIR${RESET}
      2. Check the wiring:   ${BOLD}./run.sh --probe${RESET}
      3. Test the panel:     ${BOLD}./run.sh --self-test${RESET}
      4. Start the terminal: ${BOLD}./run.sh${RESET}

    No hardware to hand? ${BOLD}./run.sh --sim${RESET} mirrors the panel in this terminal.

    Once running: ${BOLD}F7${RESET} settings, ${BOLD}F1${RESET} keys, ${BOLD}F3${RESET} models, ${BOLD}F6${RESET} diagnostics.
NEXT
