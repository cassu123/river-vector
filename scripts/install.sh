#!/usr/bin/env bash
#
# River Vector — install / provisioning script
#
# Run as root on a freshly imaged Raspberry Pi. Sets up:
#   /etc/river-vector/                  (bootstrap config)
#   /var/lib/river-vector/              (config cache, claim code)
#   /var/log/river-vector/              (logs)
#   river-vector user                   (service account)
#   systemd unit                        (autostart)
#
# After running this, edit /etc/river-vector/bootstrap.json:
#   - Add WiFi networks (use --encrypt-psk to add encrypted PSKs)
#   - Confirm river_song URLs
# Then `systemctl start river-vector`.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_USER="river-vector"
ETC_DIR="/etc/river-vector"
VAR_DIR="/var/lib/river-vector"
LOG_DIR="/var/log/river-vector"
BOOTSTRAP="${ETC_DIR}/bootstrap.json"
SERVICE_UNIT="/etc/systemd/system/river-vector.service"
VISION_SERVICE_UNIT="/etc/systemd/system/river-vector-vision.service"
VISION_PORT="8090"

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "Must run as root. Try: sudo $0 $*" >&2
        exit 1
    fi
}

usage() {
    cat <<EOF
Usage: $0 <command> [options]

Commands:
  install [compute-opts]       Full provisioning: dirs, user, service, bootstrap template
  set-compute [compute-opts]   Reconfigure this node's compute topology in place
  uninstall                    Remove the service(s) (preserves data)
  reset                        Reset the device to UNCLAIMED (wipes unit_token)
  add-wifi <ssid> <password>   Add a WiFi network to bootstrap (encrypts PSK)
  status                       Print bootstrap state and service status

Compute options (topology of THIS physical box within the unit):
  --topology solo|split        solo (default): this node runs everything.
                               split: roles spread across nodes on the LAN.
  --role control|vision        Role this node owns (repeatable). Defaults to
                               both for solo; required for split.
  --peer ROLE=URL              Where to reach a role this node delegates,
                               e.g. --peer vision=http://10.55.0.2:8090 (repeatable).
  --node-id NAME               Stable id for this box (e.g. voyager-ctrl).

Examples:
  $0 install                                         # solo (Chromebox / lone Pi 5)
  $0 install --topology split --role control \\
             --peer vision=http://10.55.0.2:8090 --node-id voyager-ctrl
  $0 install --topology split --role vision --node-id voyager-vision

The control node runs river-vector.service (python -m core.main).
A vision-only node runs river-vector-vision.service (python -m vision.node).
After install, add WiFi: $0 add-wifi <ssid> <password>.
EOF
}

# ── Compute-option parsing (shared by install / set-compute) ──────────────
CO_TOPOLOGY=""
CO_ROLES=()
CO_PEERS=()
CO_NODE_ID=""

parse_compute_opts() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --topology) CO_TOPOLOGY="${2:-}"; shift 2 ;;
            --role)     CO_ROLES+=("${2:-}"); shift 2 ;;
            --peer)     CO_PEERS+=("${2:-}"); shift 2 ;;
            --node-id)  CO_NODE_ID="${2:-}"; shift 2 ;;
            *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
        esac
    done
}

# Writes the compute block into BOOTSTRAP from parsed options, validating it
# through the canonical ComputeProfile. Sets COMPUTE_PRIMARY_ROLE so the
# caller knows which systemd unit to install. With no compute flags it leaves
# the existing bootstrap untouched and just reports its current role.
COMPUTE_PRIMARY_ROLE="control"
apply_compute() {
    if [[ -z "${CO_TOPOLOGY}" && ${#CO_ROLES[@]} -eq 0 && ${#CO_PEERS[@]} -eq 0 && -z "${CO_NODE_ID}" ]]; then
        # No compute flags → keep whatever the template/bootstrap already has.
        COMPUTE_PRIMARY_ROLE="$(BOOTSTRAP="${BOOTSTRAP}" python3 - <<'EOF'
import json, os
try:
    d = json.load(open(os.environ["BOOTSTRAP"]))
    roles = (d.get("compute") or {}).get("roles", ["control", "vision"])
except Exception:
    roles = ["control", "vision"]
print("vision" if roles == ["vision"] else "control")
EOF
)"
        return
    fi
    cd "${REPO_DIR}"
    # Pass parsed options to python via distinct env names (CSV) so we never
    # clobber the bash arrays themselves.
    local roles_csv peers_csv
    roles_csv="$(IFS=,; echo "${CO_ROLES[*]:-}")"
    peers_csv="$(IFS=,; echo "${CO_PEERS[*]:-}")"
    COMPUTE_PRIMARY_ROLE="$(
        BOOTSTRAP="${BOOTSTRAP}" \
        CO_TOPOLOGY="${CO_TOPOLOGY}" \
        CO_NODE_ID="${CO_NODE_ID}" \
        CO_ROLES_CSV="${roles_csv}" \
        CO_PEERS_CSV="${peers_csv}" \
        python3 - <<'PYEOF'
import json, os, sys
sys.path.insert(0, ".")
from core.compute_topology import ComputeProfile, ALL_ROLES

bootstrap = os.environ["BOOTSTRAP"]
topology = os.environ.get("CO_TOPOLOGY") or ""
node_id  = os.environ.get("CO_NODE_ID") or ""
roles    = [r for r in os.environ.get("CO_ROLES_CSV", "").split(",") if r]
peers_in = [p for p in os.environ.get("CO_PEERS_CSV", "").split(",") if p]

if not topology:
    topology = "solo"
if not roles:
    roles = ["vision"] if topology == "split" else list(ALL_ROLES)
peers = {}
for item in peers_in:
    if "=" not in item:
        sys.exit(f"--peer must be ROLE=URL, got '{item}'")
    role, url = item.split("=", 1)
    peers[role.strip()] = url.strip()

profile = ComputeProfile(node_id=node_id, topology=topology, roles=roles, peers=peers)
profile.validated()  # raises ComputeTopologyError on bad/unsafe config

with open(bootstrap) as f:
    d = json.load(f)
d["compute"] = profile.to_dict()
with open(bootstrap, "w") as f:
    json.dump(d, f, indent=2)

print("vision" if roles == ["vision"] else "control")
PYEOF
)" || { echo "Compute configuration rejected." >&2; exit 1; }
    echo "→ Compute topology written: topology=${CO_TOPOLOGY:-solo}, roles=[${roles_csv:-default}]."
}

write_control_unit() {
    cat > "${SERVICE_UNIT}" <<EOF
[Unit]
Description=River Vector autonomous mower control
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m core.main
WorkingDirectory=${REPO_DIR}
Restart=always
RestartSec=5
User=root
Group=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
}

write_vision_unit() {
    cat > "${VISION_SERVICE_UNIT}" <<EOF
[Unit]
Description=River Vector vision node (cameras + CV) for split compute topology
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m vision.node --port ${VISION_PORT}
WorkingDirectory=${REPO_DIR}
Restart=always
RestartSec=5
User=root
Group=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
}

cmd_install() {
    require_root
    parse_compute_opts "$@"

    echo "→ Creating directories..."
    mkdir -p "${ETC_DIR}" "${VAR_DIR}" "${LOG_DIR}"

    echo "→ Creating service user '${INSTALL_USER}'..."
    if ! id -u "${INSTALL_USER}" >/dev/null 2>&1; then
        useradd --system --no-create-home --shell /usr/sbin/nologin "${INSTALL_USER}"
    fi
    chown -R "${INSTALL_USER}:${INSTALL_USER}" "${VAR_DIR}" "${LOG_DIR}"
    chmod 750 "${VAR_DIR}" "${LOG_DIR}"

    echo "→ Writing bootstrap template (if absent)..."
    if [[ ! -f "${BOOTSTRAP}" ]]; then
        cp "${REPO_DIR}/units/example.json" "${BOOTSTRAP}"
        chown root:root "${BOOTSTRAP}"
        chmod 600 "${BOOTSTRAP}"
        echo "  Wrote ${BOOTSTRAP}. Edit it before starting the service."
    else
        echo "  ${BOOTSTRAP} already exists; leaving untouched."
    fi

    echo "→ Configuring compute topology..."
    apply_compute

    echo "→ Writing systemd unit(s)..."
    if [[ "${COMPUTE_PRIMARY_ROLE}" == "vision" ]]; then
        write_vision_unit
        systemctl daemon-reload
        systemctl enable river-vector-vision.service
        echo "✓ Install complete (VISION node)."
        echo
        echo "Next steps:"
        echo "  1. Start the vision service: systemctl start river-vector-vision"
        echo "  2. Watch logs:               journalctl -u river-vector-vision -f"
        echo "  3. On the control node, point --peer vision=http://<this-ip>:${VISION_PORT}"
    else
        write_control_unit
        systemctl daemon-reload
        systemctl enable river-vector.service
        echo "✓ Install complete (CONTROL node)."
        echo
        echo "Next steps:"
        echo "  1. Add WiFi networks: $0 add-wifi <ssid> <password>"
        echo "  2. Start the service:  systemctl start river-vector"
        echo "  3. Watch logs:         journalctl -u river-vector -f"
        echo "  4. On boot, claim the device from riversongai.com → Fleet → Discovered"
    fi
}

cmd_set_compute() {
    require_root
    if [[ ! -f "${BOOTSTRAP}" ]]; then
        echo "No bootstrap at ${BOOTSTRAP} — run '$0 install' first." >&2
        exit 1
    fi
    parse_compute_opts "$@"
    if [[ -z "${CO_TOPOLOGY}" && ${#CO_ROLES[@]} -eq 0 && ${#CO_PEERS[@]} -eq 0 && -z "${CO_NODE_ID}" ]]; then
        echo "Nothing to do — pass --topology / --role / --peer / --node-id." >&2
        exit 1
    fi
    apply_compute
    echo "→ Re-aligning systemd units to new role (${COMPUTE_PRIMARY_ROLE})..."
    if [[ "${COMPUTE_PRIMARY_ROLE}" == "vision" ]]; then
        systemctl disable --now river-vector.service 2>/dev/null || true
        rm -f "${SERVICE_UNIT}"
        write_vision_unit
        systemctl daemon-reload
        systemctl enable river-vector-vision.service
    else
        systemctl disable --now river-vector-vision.service 2>/dev/null || true
        rm -f "${VISION_SERVICE_UNIT}"
        write_control_unit
        systemctl daemon-reload
        systemctl enable river-vector.service
    fi
    echo "✓ Compute topology updated. Restart the active service to apply."
}

cmd_uninstall() {
    require_root
    echo "→ Removing service unit(s)..."
    systemctl stop river-vector.service river-vector-vision.service 2>/dev/null || true
    systemctl disable river-vector.service river-vector-vision.service 2>/dev/null || true
    rm -f "${SERVICE_UNIT}" "${VISION_SERVICE_UNIT}"
    systemctl daemon-reload
    echo "✓ Service(s) removed. Data under ${ETC_DIR}, ${VAR_DIR}, ${LOG_DIR} preserved."
}

cmd_reset() {
    require_root
    if [[ ! -f "${BOOTSTRAP}" ]]; then
        echo "No bootstrap at ${BOOTSTRAP} — nothing to reset." >&2
        exit 1
    fi
    echo "→ Resetting device to UNCLAIMED..."
    python3 - <<EOF
import json
with open("${BOOTSTRAP}", "r") as f:
    d = json.load(f)
d["claim_state"] = "UNCLAIMED"
d["unit_token"] = ""
with open("${BOOTSTRAP}", "w") as f:
    json.dump(d, f, indent=2)
EOF
    rm -f "${VAR_DIR}/claim_code.txt" "${VAR_DIR}/config_cache.json"
    systemctl restart river-vector.service 2>/dev/null || true
    echo "✓ Device reset. Re-claim from riversongai.com."
}

cmd_add_wifi() {
    require_root
    local ssid="${1:-}"
    local pw="${2:-}"
    if [[ -z "${ssid}" ]]; then
        echo "Usage: $0 add-wifi <ssid> <password>" >&2
        exit 1
    fi
    cd "${REPO_DIR}"
    python3 - <<EOF
import json, sys
sys.path.insert(0, ".")
from core.bootstrap import load_bootstrap, save_bootstrap, encrypt_psk, WifiNetwork

bc = load_bootstrap("${BOOTSTRAP}")
psk = encrypt_psk("${pw}") if "${pw}" else ""

# Replace if SSID already present.
existing = [n for n in bc.wifi_networks if n.ssid == "${ssid}"]
if existing:
    existing[0].psk_encrypted = psk
    print(f"Updated WiFi network ${ssid}.")
else:
    next_priority = max((n.priority for n in bc.wifi_networks), default=0) + 1
    bc.wifi_networks.append(WifiNetwork(ssid="${ssid}", psk_encrypted=psk, priority=next_priority))
    print(f"Added WiFi network ${ssid} (priority={next_priority}).")
save_bootstrap(bc, "${BOOTSTRAP}")
EOF
}

cmd_status() {
    require_root
    if [[ ! -f "${BOOTSTRAP}" ]]; then
        echo "Not installed: ${BOOTSTRAP} not found."
        exit 1
    fi
    echo "Bootstrap state:"
    python3 -c "
import json
with open('${BOOTSTRAP}') as f:
    d = json.load(f)
print(f\"  unit_id:        {d.get('unit_id','(unassigned)')}\")
print(f\"  claim_state:    {d.get('claim_state')}\")
print(f\"  claimed:        {bool(d.get('unit_token'))}\")
print(f\"  wifi_networks:  {len(d.get('wifi_networks',[]))}\")
print(f\"  url_primary:    {d.get('server',{}).get('url_primary')}\")
print(f\"  url_fallback:   {d.get('server',{}).get('url_fallback')}\")
c = d.get('compute',{}) or {}
print(f\"  compute:        topology={c.get('topology','solo')} roles={c.get('roles',['control','vision'])} peers={c.get('peers',{})}\")
"
    echo
    echo "Service status:"
    systemctl is-active river-vector.service 2>/dev/null || true
    systemctl is-enabled river-vector.service 2>/dev/null || true
}

# ── Dispatch ────────────────────────────────────────────────────────────

cmd="${1:-}"
case "${cmd}" in
    install)       shift; cmd_install "$@" ;;
    set-compute)   shift; cmd_set_compute "$@" ;;
    uninstall)     cmd_uninstall ;;
    reset)         cmd_reset ;;
    add-wifi)      shift; cmd_add_wifi "$@" ;;
    status)        cmd_status ;;
    -h|--help|"")  usage ;;
    *)             echo "Unknown command: ${cmd}" >&2; usage; exit 1 ;;
esac
