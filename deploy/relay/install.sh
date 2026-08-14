#!/bin/sh
# Install the AIOps relay node agent as a systemd service.
#
# Nothing here hides. It creates one named account, one unit, one state
# directory and one uninstall command, and prints each of them. If you want it
# gone, run aiops-relay-uninstall.
#
#   sudo ./install.sh --url https://aiops.example.com --token <enrolment token>
set -eu

URL=""
TOKEN=""
NAME="aiops-relay-node"
RUN_USER="aiops-relay"
PREFIX="/opt/aiops-relay"
STATE_DIR="/var/lib/aiops-relay"
CONF_DIR="/etc/aiops-relay"
INSECURE=""

usage() {
    cat <<'EOF'
Usage: install.sh --url <AIOps URL> --token <enrolment token> [options]

  --url URL         Where AIOps is, e.g. https://aiops.example.com
  --token TOKEN     One-time enrolment token, from Nodes -> Register in AIOps
  --name NAME       systemd unit name (default aiops-relay-node)
  --user USER       Account to run as (default aiops-relay, created if absent)
  --insecure        Skip TLS verification. Only for an AIOps with a self-signed
                    certificate, and it is printed in the service file so
                    nobody discovers it by accident later.
  --help            This.

Removing it: sudo aiops-relay-uninstall
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --url) URL="$2"; shift 2 ;;
        --token) TOKEN="$2"; shift 2 ;;
        --name) NAME="$2"; shift 2 ;;
        --user) RUN_USER="$2"; shift 2 ;;
        --insecure) INSECURE="1"; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

[ -n "$URL" ] || { echo "--url is required" >&2; exit 2; }
[ "$(id -u)" = "0" ] || { echo "run this with sudo: it creates a service account and a unit" >&2; exit 2; }
command -v systemctl >/dev/null 2>&1 || { echo "no systemd here; use the Docker installer instead" >&2; exit 2; }

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || { echo "python3 is required and was not found on PATH" >&2; exit 2; }

SOURCE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
AGENT_SRC="$SOURCE_DIR/aiops_relay_node.py"
[ -f "$AGENT_SRC" ] || { echo "aiops_relay_node.py is not next to this script" >&2; exit 2; }

echo "Installing the AIOps relay node agent."
echo "  AIOps:        $URL"
echo "  Service:      $NAME.service"
echo "  Runs as:      $RUN_USER"
echo "  Agent:        $PREFIX/aiops_relay_node.py"
echo "  State:        $STATE_DIR"
echo

# --- the account it runs as -------------------------------------------
if ! id "$RUN_USER" >/dev/null 2>&1; then
    # No login shell and no home: this account exists to hold one socket open.
    useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_USER"
    echo "Created system account $RUN_USER."
fi

install -d -m 0755 "$PREFIX"
install -d -m 0755 "$CONF_DIR"
install -d -m 0700 -o "$RUN_USER" -g "$RUN_USER" "$STATE_DIR"
install -m 0755 "$AGENT_SRC" "$PREFIX/aiops_relay_node.py"

cat > "$CONF_DIR/node.env" <<EOF
# Written by install.sh. The enrolment token is deliberately not here: it is
# single-use and was spent during installation.
AIOPS_RELAY_URL=$URL
AIOPS_RELAY_STATE_DIR=$STATE_DIR
AIOPS_RELAY_INSECURE=${INSECURE:-0}
EOF
chmod 0644 "$CONF_DIR/node.env"

# --- enrol before starting, so a bad token fails here and visibly ------
if [ -n "$TOKEN" ] && [ ! -f "$STATE_DIR/credential" ]; then
    echo "Enrolling with AIOps..."
    su -s /bin/sh "$RUN_USER" -c \
        "AIOPS_RELAY_INSECURE=${INSECURE:-0} $PYTHON $PREFIX/aiops_relay_node.py \
         --url '$URL' --token '$TOKEN' --state-dir '$STATE_DIR' --enrol-only"
elif [ ! -f "$STATE_DIR/credential" ]; then
    echo "No --token given and no credential stored yet." >&2
    echo "The service will start but stay idle until you re-run with a token." >&2
fi

# --- the unit ----------------------------------------------------------
# Substituted from the template beside this script rather than written out
# here, so the unit that is reviewed in the repo is the unit that is installed.
UNIT_SRC="$SOURCE_DIR/aiops-relay-node.service"
[ -f "$UNIT_SRC" ] || { echo "aiops-relay-node.service is not next to this script" >&2; exit 2; }
sed -e "s|@RUN_USER@|$RUN_USER|g" \
    -e "s|@CONF_DIR@|$CONF_DIR|g" \
    -e "s|@PYTHON@|$PYTHON|g" \
    -e "s|@PREFIX@|$PREFIX|g" \
    -e "s|@STATE_DIR@|$STATE_DIR|g" \
    "$UNIT_SRC" > "/etc/systemd/system/$NAME.service"
chmod 0644 "/etc/systemd/system/$NAME.service"

# --- one command to undo all of it -------------------------------------
cat > /usr/local/sbin/aiops-relay-uninstall <<EOF
#!/bin/sh
# Remove the AIOps relay node agent installed by install.sh.
set -eu
[ "\$(id -u)" = "0" ] || { echo "run with sudo" >&2; exit 2; }
echo "Removing the AIOps relay node."
systemctl disable --now "$NAME.service" 2>/dev/null || true
rm -f "/etc/systemd/system/$NAME.service"
systemctl daemon-reload
rm -rf "$PREFIX" "$STATE_DIR" "$CONF_DIR"
if id "$RUN_USER" >/dev/null 2>&1; then
    userdel "$RUN_USER" 2>/dev/null || true
fi
rm -f /usr/local/sbin/aiops-relay-uninstall
echo "Done. Revoke the node in AIOps as well — this machine no longer answers,"
echo "but the node record is still there until you do."
EOF
chmod 0755 /usr/local/sbin/aiops-relay-uninstall

systemctl daemon-reload
systemctl enable --now "$NAME.service"

echo
echo "Installed. The node is enrolled but carries no traffic until an AIOps"
echo "administrator approves it (Nodes -> Approve)."
echo
echo "  status:    systemctl status $NAME"
echo "  logs:      journalctl -u $NAME -f"
echo "  remove:    sudo aiops-relay-uninstall"
