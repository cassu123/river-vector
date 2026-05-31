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

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "Must run as root. Try: sudo $0 $*" >&2
        exit 1
    fi
}

usage() {
    cat <<EOF
Usage: $0 <command>

Commands:
  install                      Full provisioning: dirs, user, service, bootstrap template
  uninstall                    Remove the service (preserves data)
  reset                        Reset the device to UNCLAIMED (wipes unit_token)
  add-wifi <ssid> <password>   Add a WiFi network to bootstrap (encrypts PSK)
  status                       Print bootstrap state and service status

After install, edit ${BOOTSTRAP} to add real WiFi networks.
EOF
}

cmd_install() {
    require_root

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

    echo "→ Writing systemd unit..."
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
    systemctl daemon-reload
    systemctl enable river-vector.service

    echo "✓ Install complete."
    echo
    echo "Next steps:"
    echo "  1. Add WiFi networks: $0 add-wifi <ssid> <password>"
    echo "  2. Start the service:  systemctl start river-vector"
    echo "  3. Watch logs:         journalctl -u river-vector -f"
    echo "  4. On boot, claim the device from riversongai.com → Fleet → Discovered"
}

cmd_uninstall() {
    require_root
    echo "→ Removing service unit..."
    systemctl stop river-vector.service 2>/dev/null || true
    systemctl disable river-vector.service 2>/dev/null || true
    rm -f "${SERVICE_UNIT}"
    systemctl daemon-reload
    echo "✓ Service removed. Data under ${ETC_DIR}, ${VAR_DIR}, ${LOG_DIR} preserved."
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
"
    echo
    echo "Service status:"
    systemctl is-active river-vector.service 2>/dev/null || true
    systemctl is-enabled river-vector.service 2>/dev/null || true
}

# ── Dispatch ────────────────────────────────────────────────────────────

cmd="${1:-}"
case "${cmd}" in
    install)       cmd_install ;;
    uninstall)     cmd_uninstall ;;
    reset)         cmd_reset ;;
    add-wifi)      shift; cmd_add_wifi "$@" ;;
    status)        cmd_status ;;
    -h|--help|"")  usage ;;
    *)             echo "Unknown command: ${cmd}" >&2; usage; exit 1 ;;
esac
