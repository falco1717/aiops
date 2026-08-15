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
PROXY=""
CA_BUNDLE=""

usage() {
    cat <<'EOF'
Usage: install.sh --url <AIOps URL> --token <enrolment token> [options]

  --url URL         Where AIOps is, e.g. https://aiops.example.com
  --token TOKEN     One-time enrolment token, from Nodes -> Register in AIOps
  --name NAME       systemd unit name (default aiops-relay-node)
  --user USER       Account to run as (default aiops-relay, created if absent)
  --proxy URL       HTTP CONNECT proxy to reach AIOps through, e.g.
                    http://proxy.corp.example:8080, with credentials in the URL
                    if it wants them. Takes precedence over HTTPS_PROXY and
                    ALL_PROXY, and is persisted so restarts keep it.
  --ca-bundle PATH  PEM file of extra certificate authorities to trust. This is
                    the answer to a gateway that re-signs TLS, and the one to
                    prefer over --insecure.
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
        --proxy) PROXY="$2"; shift 2 ;;
        --ca-bundle) CA_BUNDLE="$2"; shift 2 ;;
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
# Resolved through any symlink before it is judged or written down: /usr/bin/
# python3 is usually a link, and where it points is what the service will run.
PYTHON="$(readlink -f "$PYTHON" 2>/dev/null || echo "$PYTHON")"

# The same exposure Windows has, in the shape Linux takes it. The unit sets
# ProtectHome=yes, so an interpreter under /home or /root is invisible to the
# service however readable it looks from here - a virtualenv in somebody's home
# directory is the usual way to arrive at this.
case "$PYTHON" in
    /home/*|/root/*)
        echo "python3 here is $PYTHON, which is inside a home directory." >&2
        echo "The unit sets ProtectHome=yes, so the service cannot see it at all." >&2
        echo "Install a system python3 (apt install python3 / dnf install python3)," >&2
        echo "or pass one with PATH set to it, and run this again." >&2
        exit 2 ;;
esac

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

# --- can the account this will run as actually use that interpreter? ----
# Asked by running it as that account, which is the question rather than a
# proxy for it. The equivalent check on Windows can only read permissions,
# because there the service account does not exist until the service does.
if ! su -s /bin/sh "$RUN_USER" -c "$PYTHON -c 'import sys' " >/dev/null 2>&1; then
    echo "$RUN_USER cannot run $PYTHON." >&2
    echo "The service runs as $RUN_USER, so it would fail the same way with no" >&2
    echo "output at all. Install a system-wide python3 and run this again." >&2
    exit 2
fi

# Copied in beside the config rather than referenced where it was found: the
# path somebody passes is usually in their home directory, and the unit sets
# ProtectHome=yes, so the service would never be able to read it there.
INSTALLED_CA=""
if [ -n "$CA_BUNDLE" ]; then
    [ -f "$CA_BUNDLE" ] || { echo "--ca-bundle $CA_BUNDLE is not a file" >&2; exit 2; }
    install -m 0644 "$CA_BUNDLE" "$CONF_DIR/ca-bundle.pem"
    INSTALLED_CA="$CONF_DIR/ca-bundle.pem"
fi

cat > "$CONF_DIR/node.env" <<EOF
# Written by install.sh. The enrolment token is deliberately not here: it is
# single-use and was spent during installation.
AIOPS_RELAY_URL=$URL
AIOPS_RELAY_STATE_DIR=$STATE_DIR
AIOPS_RELAY_INSECURE=${INSECURE:-0}
AIOPS_RELAY_PROXY=$PROXY
AIOPS_RELAY_CA_BUNDLE=$INSTALLED_CA
EOF
# 0640, not 0644: a proxy URL routinely carries a password. systemd reads an
# EnvironmentFile as root before it drops to the service account, and the
# account itself is given group read so nothing needs relaxing later.
chown "root:$RUN_USER" "$CONF_DIR/node.env"
chmod 0640 "$CONF_DIR/node.env"

# --- can this machine reach AIOps at all? ------------------------------
# Before a service exists, before a token is spent. The agent's own --diagnose
# is what runs, as the account that will be doing it, so a network this node
# cannot get out of is a refusal here with a printed reason rather than a
# service quietly flapping afterwards.
echo "Checking that $RUN_USER can reach AIOps..."
if ! su -s /bin/sh "$RUN_USER" -c \
        "AIOPS_RELAY_INSECURE=${INSECURE:-0} \
         AIOPS_RELAY_PROXY='$PROXY' AIOPS_RELAY_CA_BUNDLE='$INSTALLED_CA' \
         $PYTHON $PREFIX/aiops_relay_node.py --url '$URL' --state-dir '$STATE_DIR' --diagnose"; then
    echo "This machine cannot reach AIOps. No service has been created and no" >&2
    echo "enrolment token has been spent; the reason is above." >&2
    exit 1
fi

# --- enrol before starting, so a bad token fails here and visibly ------
if [ -n "$TOKEN" ] && [ ! -f "$STATE_DIR/credential" ]; then
    echo "Enrolling with AIOps..."
    # Enrolment dials out too, so it is given exactly the egress the running
    # service will have. An enrolment that ignored the proxy failed at install
    # time on precisely the machines the proxy was there for.
    su -s /bin/sh "$RUN_USER" -c \
        "AIOPS_RELAY_INSECURE=${INSECURE:-0} \
         AIOPS_RELAY_PROXY='$PROXY' AIOPS_RELAY_CA_BUNDLE='$INSTALLED_CA' \
         $PYTHON $PREFIX/aiops_relay_node.py \
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

# The status file is the node's own answer to "did you get there". Removed
# first so what is read below is this start's answer and not the last one's.
rm -f "$STATE_DIR/status"

systemctl daemon-reload
systemctl enable --now "$NAME.service"

# --- did it actually connect? ------------------------------------------
# The whole reason this section exists: an installer that exits 0 over a node
# that will never connect is worse than one that fails, because it sends
# somebody to look at the network. "pending" counts - a node that reached
# AIOps and was told it is not approved yet has done its part.
printf 'Waiting for the node to reach AIOps'
CONNECTED=""
WAITED=0
while [ "$WAITED" -lt 60 ]; do
    if [ -f "$STATE_DIR/status" ]; then
        STATE="$(cut -d' ' -f1 < "$STATE_DIR/status" 2>/dev/null || true)"
        case "$STATE" in
            connected|pending) CONNECTED="$STATE"; break ;;
            revoked|unauthenticated) CONNECTED="$STATE"; break ;;
        esac
    fi
    if ! systemctl is-active --quiet "$NAME.service"; then break; fi
    printf '.'
    sleep 2
    WAITED=$((WAITED + 2))
done
printf '\n'

case "$CONNECTED" in
    connected)
        echo "The node is connected to AIOps." ;;
    pending)
        echo "The node reached AIOps and is waiting to be approved (Nodes -> Approve)." ;;
    *)
        echo >&2
        echo "The service is installed and the node has NOT reached AIOps." >&2
        echo >&2
        if [ -n "$CONNECTED" ]; then
            echo "AIOps answered, and its answer was: $CONNECTED" >&2
        else
            echo "It said nothing in 60 seconds. What the service has to say:" >&2
            journalctl -u "$NAME" -n 20 --no-pager >&2 2>/dev/null || true
        fi
        echo >&2
        echo "Ask the node itself why:" >&2
        echo >&2
        echo "  sudo -u $RUN_USER sh -c 'set -a; . $CONF_DIR/node.env; exec $PYTHON $PREFIX/aiops_relay_node.py --diagnose'" >&2
        echo >&2
        exit 1 ;;
esac

echo
echo "Installed. The node carries no traffic until an AIOps administrator"
echo "approves it (Nodes -> Approve)."
echo
echo "  status:    systemctl status $NAME"
echo "  logs:      journalctl -u $NAME -f"
# Run with the unit's own EnvironmentFile sourced, so the check is made with
# the configuration the service has and not with an empty environment.
echo "  check:     sudo -u $RUN_USER sh -c 'set -a; . $CONF_DIR/node.env; exec $PYTHON $PREFIX/aiops_relay_node.py --diagnose'"
echo "  remove:    sudo aiops-relay-uninstall"
