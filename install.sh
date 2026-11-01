#!/usr/bin/env bash
#
# Install the pump dashboard systemd unit for this checkout and machine.
#
# Creates a dedicated system account for the service (pumpdash by default),
# rewrites User=, Group=, WorkingDirectory= and ExecStart= in
# pump-dashboard.service, installs it, then enables and (re)starts the service.
# Safe to re-run.
#
set -euo pipefail

SERVICE_NAME="pump-dashboard"
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
UNIT_SOURCE="$REPO_DIR/pump-dashboard.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

DEFAULT_SYSTEM_USER="pumpdash"
TARGET_USER=""
TARGET_GROUP="dialout"
PYTHON_BIN=""
CREATE_USER=0

usage() {
    cat <<EOF
Usage: sudo $0 [options]

  --system-user NAME  create (if needed) and run as a system account
                      (default: pumpdash)
  --user NAME         run as an existing account instead
  --group NAME        group that can open the serial device (default: dialout)
  --python PATH       python3 interpreter to run (default: python3 on PATH)
  -h, --help          show this help

Environment:
  PYTHON=/path/to/python3   same as --python
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --system-user)
            TARGET_USER="${2:?--system-user needs a value}"
            CREATE_USER=1
            shift 2
            ;;
        --user)
            TARGET_USER="${2:?--user needs a value}"
            CREATE_USER=0
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
    if [[ -z "$TARGET_USER" ]]; then
        TARGET_USER="$DEFAULT_SYSTEM_USER"
        CREATE_USER=1
    fi
    PYTHON_BIN="${PYTHON_BIN:-${PYTHON:-$(command -v python3)}}"
    if [[ $CREATE_USER -eq 1 ]]; then
        user_flag="--system-user"
    else
        user_flag="--user"
    fi
    exec sudo bash "$SCRIPT_PATH" \
        "$user_flag" "$TARGET_USER" \
        --group "$TARGET_GROUP" \
        --python "$PYTHON_BIN"
fi

if [[ -z "$TARGET_USER" ]]; then
    TARGET_USER="$DEFAULT_SYSTEM_USER"
    CREATE_USER=1
fi
PYTHON_BIN="${PYTHON_BIN:-${PYTHON:-$(command -v python3)}}"

getent group "$TARGET_GROUP" >/dev/null || {
    echo "group does not exist: $TARGET_GROUP" >&2
    exit 1
}
[[ -x "$PYTHON_BIN" ]] || {
    echo "not an executable python: $PYTHON_BIN" >&2
    exit 1
}

# logind's RemoveIPC cleanup unlinks /dev/shm objects owned by the UID whose
# last session just ended.  The database therefore has to belong to an account
# that never logs in.  Using a system account also lets RemoveIPC stay at its
# default (yes), so /dev/shm still gets cleaned up after a normal user logs out.
if [[ $CREATE_USER -eq 1 ]] && ! id -u "$TARGET_USER" >/dev/null 2>&1; then
    echo "creating system user $TARGET_USER"
    useradd --system --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin --groups "$TARGET_GROUP" "$TARGET_USER"
fi

id -u "$TARGET_USER" >/dev/null 2>&1 || {
    echo "user does not exist: $TARGET_USER" >&2
    exit 1
}

if [[ "$TARGET_USER" != "root" ]] && (( $(id -u "$TARGET_USER") >= 1000 )); then
    if [[ $CREATE_USER -eq 1 ]]; then
        echo "error: --system-user $TARGET_USER exists but is a login account (uid $(id -u "$TARGET_USER"))" >&2
        echo "       choose another name, or pass --user $TARGET_USER to accept it" >&2
        exit 1
    fi
    echo "warning: $TARGET_USER is a login account (uid $(id -u "$TARGET_USER"));" >&2
    echo "         logind may delete /dev/shm/pump.db when that user logs out" >&2
fi

# Group= in the unit is enough for the service, but membership also lets the
# account run the sampler by hand.
if [[ "$TARGET_USER" != "root" ]] &&
   ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx "$TARGET_GROUP"; then
    echo "adding $TARGET_USER to group $TARGET_GROUP (log out/in to use it interactively)"
    usermod -aG "$TARGET_GROUP" "$TARGET_USER"
fi

# The checkout usually lives under the installing user's home directory, which
# is not traversable by a system account.  Grant execute-only (not read) access
# on the path, and only where it is missing, so re-runs are no-ops.
if [[ "$TARGET_USER" != "root" ]] &&
   ! runuser -u "$TARGET_USER" -- test -r "$REPO_DIR/pump_dashboard.py"; then
    d="$REPO_DIR"
    while [[ "$d" != "/" ]]; do
        mode="$(stat -c %a "$d")"
        if (( (8#$mode & 1) == 0 )); then
            echo "granting $TARGET_USER traverse permission on $d"
            chmod o+x "$d"
        fi
        d="$(dirname "$d")"
    done
    runuser -u "$TARGET_USER" -- test -r "$REPO_DIR/pump_dashboard.py" || {
        echo "$TARGET_USER still cannot read $REPO_DIR" >&2
        exit 1
    }
fi

# Older versions disabled logind's RemoveIPC globally.  That is no longer
# needed now that the database belongs to a system account, so drop the
# override and put /dev/shm cleanup back to its default.
LOGIND_DROPIN=/etc/systemd/logind.conf.d/99-pump-dashboard.conf
if [[ -e "$LOGIND_DROPIN" ]]; then
    rm -f "$LOGIND_DROPIN"
    rmdir --ignore-fail-on-non-empty "$(dirname "$LOGIND_DROPIN")" 2>/dev/null || true
    systemctl reload systemd-logind 2>/dev/null || true
    echo "removed $LOGIND_DROPIN (logind cleanup restored)"
fi
logind_ipc="$(busctl get-property org.freedesktop.login1 \
    /org/freedesktop/login1 org.freedesktop.login1.Manager RemoveIPC \
    2>/dev/null | awk '{print $2}' || true)"
if [[ -n "$logind_ipc" ]]; then
    echo "logind RemoveIPC=$logind_ipc"
fi

# The database files belong to the service account.  Stop the old instance
# first, then hand over any files created by a previous owner.
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
DB_PATH="$(sed -n 's/^Environment=PUMP_DB=//p' "$UNIT_SOURCE" | head -n 1)"
DB_PATH="${DB_PATH:-/dev/shm/pump.db}"
for db_file in "$DB_PATH"*; do
    [[ -e "$db_file" ]] || continue
    chown "$TARGET_USER:$TARGET_GROUP" "$db_file"
done

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
