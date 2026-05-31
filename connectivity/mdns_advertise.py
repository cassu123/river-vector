"""
River Vector - mDNS Advertise

During the CLAIMING phase, the device broadcasts its presence on the
LAN so River Song can discover it. Once claimed, the broadcast stops.

Service type: _rivervector._tcp.local.
TXT record:   unit_id=<id>, proto_version=<n>

River Song's mDNS listener picks these up and exposes them at
GET /api/vector/units/discovered for the operator to claim from the
fleet UI.

Uses zeroconf (pure-Python mDNS). If zeroconf is not installed, the
module logs a warning and the device degrades gracefully — claiming
still works via manual unit_id entry in the UI.
"""

from __future__ import annotations

import logging
import socket
from typing import Optional

from core.constants import MDNS_PORT, MDNS_SERVICE_TYPE, PROTOCOL_VERSION

logger = logging.getLogger(__name__)

try:
    from zeroconf import IPVersion, ServiceInfo, Zeroconf
    _ZEROCONF_AVAILABLE = True
except ImportError:
    _ZEROCONF_AVAILABLE = False
    logger.warning(
        "zeroconf not installed — mDNS advertising disabled. "
        "Claim flow requires manual unit_id entry."
    )


class MdnsAdvertiser:
    """
    Advertises the device on the LAN during CLAIMING.

    Args:
        unit_id:  This device's unit_id.
        port:     The claim_server port (default MDNS_PORT).
    """

    def __init__(self, unit_id: str, port: int = MDNS_PORT) -> None:
        self._unit_id = unit_id
        self._port = port
        self._zc: Optional[object] = None
        self._info: Optional[object] = None

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not _ZEROCONF_AVAILABLE:
            return
        if self._zc is not None:
            return

        ip = self._get_lan_ip()
        if ip is None:
            logger.warning("Could not determine LAN IP — mDNS advertise skipped.")
            return

        service_name = f"{self._unit_id}.{MDNS_SERVICE_TYPE}"
        properties = {
            "unit_id": self._unit_id,
            "proto_version": str(PROTOCOL_VERSION),
        }
        self._info = ServiceInfo(
            type_=MDNS_SERVICE_TYPE,
            name=service_name,
            addresses=[socket.inet_aton(ip)],
            port=self._port,
            properties=properties,
            server=f"{self._unit_id.lower()}.local.",
        )
        self._zc = Zeroconf(ip_version=IPVersion.V4Only)
        self._zc.register_service(self._info)
        logger.info(
            "mDNS advertising: %s at %s:%d", service_name, ip, self._port
        )

    def stop(self) -> None:
        if self._zc is None:
            return
        try:
            if self._info is not None:
                self._zc.unregister_service(self._info)
            self._zc.close()
        except Exception as exc:
            logger.warning("mDNS stop error: %s", exc)
        finally:
            self._zc = None
            self._info = None
            logger.info("mDNS advertising stopped.")

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_lan_ip() -> Optional[str]:
        """
        Determines the LAN IP by opening a UDP socket toward a public host.
        No data is actually sent.
        """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except OSError:
            return None
