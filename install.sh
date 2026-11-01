#!/usr/bin/env bash
#
# Install the pump dashboard systemd unit for this checkout and machine.
#
# Rewrites User=, Group=, WorkingDirectory= and ExecStart= in
# pump-dashboard.service, installs it, then enables and (re)starts the service.
# Safe to re-run.
#
set -euo pipefail

SERVICE_NAME="pump-dashboard"
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
UNIT_SOURCE="$REPO_DIR/pump-dashboard.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

TARGET_USER=""
TARGET_GROUP="dialout"
PYTHON_BIN=""

usage() {
    cat <<EOF
Usage: sudo $0 [options]

  --user NAME     account to run the service as
                  (default: \$SUDO_USER, or the owner of this checkout)
  --group NAME    group that can open the serial device (default: dialout)
  --python PATH   python3 interpreter to run (default: python3 on PATH)
  -h, --help      show this help

Environment:
  PYTHON=/path/to/python3   same as --python
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            TARGET_USER="${2:?--user needs a value}"
            shift 2
            ;;
        --group)
            TARGET_GROUP="${2:?--group needs a value}"
            shift 2
            ;;
        --python)
            PYTHON_BIN="${2:?--python needs a value}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

[[ -f "$UNIT_SOURCE" ]] || { echo "missing $UNIT_SOURCE" >&2; exit 1; }
[[ -f "$REPO_DIR/pump_dashboard.py" ]] || {
    echo "missing $REPO_DIR/pump_dashboard.py" >&2
    exit 1
}

# Resolve defaults before escalating, so the invoking account and PATH are
# used rather than root's.
if [[ $EUID -ne 0 ]]; then
    TARGET_USER="${TARGET_USER:-${SUDO_USER:-$(id -un)}}"
    PYTHON_BIN="${PYTHON_BIN:-${PYTHON:-$(command -v python3)}}"
    exec sudo bash "$SCRIPT_PATH" \
        --user "$TARGET_USER" \
        --group "$TARGET_GROUP" \
        --python "$PYTHON_BIN"
fi

TARGET_USER="${TARGET_USER:-${SUDO_USER:-$(stat -c %U "$REPO_DIR")}}"
PYTHON_BIN="${PYTHON_BIN:-${PYTHON:-$(command -v python3)}}"

id -u "$TARGET_USER" >/dev/null 2>&1 || {
    echo "user does not exist: $TARGET_USER" >&2
    exit 1
}
getent group "$TARGET_GROUP" >/dev/null || {
    echo "group does not exist: $TARGET_GROUP" >&2
    exit 1
}
[[ -x "$PYTHON_BIN" ]] || {
    echo "not an executable python: $PYTHON_BIN" >&2
    exit 1
}

# Group= in the unit is enough for the service, but membership also lets the
# account run the sampler by hand.
if [[ "$TARGET_USER" != "root" ]] &&
   ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx "$TARGET_GROUP"; then
    echo "adding $TARGET_USER to group $TARGET_GROUP (log out/in to use it interactively)"
    usermod -aG "$TARGET_GROUP" "$TARGET_USER"
fi

if ! compgen -G "/dev/ttyUSB*" >/dev/null; then
    echo "warning: no /dev/ttyUSB* found; the sampler will report read errors until the VFD is connected" >&2
fi

TMP_UNIT="$(mktemp)"
trap 'rm -f "$TMP_UNIT"' EXIT

USER_NAME="$TARGET_USER" \
GROUP_NAME="$TARGET_GROUP" \
REPO_PATH="$REPO_DIR" \
PYTHON_PATH="$PYTHON_BIN" \
awk '
    /^User=/             { print "User=" ENVIRON["USER_NAME"]; next }
    /^Group=/            { print "Group=" ENVIRON["GROUP_NAME"]; next }
    /^WorkingDirectory=/ { print "WorkingDirectory=" ENVIRON["REPO_PATH"]; next }
    /^ExecStart=/        { print "ExecStart=" ENVIRON["PYTHON_PATH"] " -B " ENVIRON["REPO_PATH"] "/pump_dashboard.py"; next }
    { print }
' "$UNIT_SOURCE" > "$TMP_UNIT"

install -m 0644 -o root -g root "$TMP_UNIT" "$UNIT_PATH"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
systemctl restart "$SERVICE_NAME"

sleep 2
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "$SERVICE_NAME failed to start; last log lines:" >&2
    tail -n 20 /dev/shm/pump-dashboard.log 2>/dev/null || true
    exit 1
fi

echo "$SERVICE_NAME is running as $TARGET_USER:$TARGET_GROUP from $REPO_DIR"
ip="$(hostname -I | awk '{for (i = 1; i <= NF; i++) if ($i ~ /^[0-9.]+$/) { print $i; exit }}')"
echo "open http://${ip:-localhost}:8080/"
