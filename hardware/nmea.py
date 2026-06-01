"""
River Vector - NMEA 0183 parsing (GGA + GSA)

Pure-function parsers for the two sentences River Vector needs in order to
report 3-D position with altitude:

  GGA — fix data: lat/lng, fix quality, satellites-in-use, HDOP, altitude (MSL).
  GSA — DOP + active satellites: PDOP/HDOP/VDOP (VDOP feeds vertical accuracy).

A valid **3-D** fix (the gate for reporting altitude) requires the GGA quality
indicator >= 1 AND satellites-in-use >= 4. Below that, altitude_m is None — the
device reports None and the server handles the rest (it never guesses altitude).

These functions do not touch hardware; a serial/UART reader (or gpsd shim) feeds
raw sentences in. `NmeaGPSInterface` wires them onto a GPSFix for consumers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from hardware.gps import FixQuality, GPSFix, GPSInterface

logger = logging.getLogger(__name__)

# Minimum 3-D fix gate.
MIN_FIX_QUALITY = 1
MIN_SATELLITES_FOR_3D = 4

# Nominal user-equivalent range error (m) used to turn VDOP into a vertical
# accuracy estimate: vertical_accuracy ≈ VDOP * UERE. 5 m is a conservative
# consumer-GPS figure; RTK fixes report far better but we only have DOP here.
NOMINAL_UERE_M = 5.0

_QUALITY_TO_FIXQUALITY = {
    0: FixQuality.NO_FIX,
    1: FixQuality.GPS,
    2: FixQuality.DGPS,
    4: FixQuality.RTK_FIXED,
    5: FixQuality.RTK_FLOAT,
}


@dataclass
class GgaData:
    """Parsed GGA sentence. altitude_m is None unless a valid 3-D fix."""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude_m: Optional[float] = None
    quality: int = 0
    satellites: int = 0
    hdop: Optional[float] = None

    @property
    def has_3d_fix(self) -> bool:
        return self.quality >= MIN_FIX_QUALITY and self.satellites >= MIN_SATELLITES_FOR_3D


def _checksum_ok(sentence: str) -> bool:
    """Validates the NMEA *HH checksum if present; True if absent (lenient)."""
    s = sentence.strip()
    if "*" not in s:
        return True
    body, _, cks = s.partition("*")
    body = body[1:] if body.startswith("$") else body
    try:
        want = int(cks[:2], 16)
    except ValueError:
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


def _dm_to_deg(value: str, hemi: str) -> Optional[float]:
    """Converts NMEA ddmm.mmmm + hemisphere to signed decimal degrees."""
    if not value:
        return None
    try:
        v = float(value)
    except ValueError:
        return None
    deg = int(v // 100)
    minutes = v - deg * 100
    dec = deg + minutes / 60.0
    if hemi in ("S", "W"):
        dec = -dec
    return dec


def _to_float(s: str) -> Optional[float]:
    try:
        return float(s) if s != "" else None
    except ValueError:
        return None


def parse_gga(sentence: str) -> Optional[GgaData]:
    """
    Parses a GGA sentence. Returns GgaData, or None if it is not a GGA
    sentence or is malformed. altitude_m is populated ONLY for a valid 3-D
    fix (quality >= 1 and satellites >= 4); otherwise it is None.
    """
    if not sentence:
        return None
    s = sentence.strip()
    if "GGA" not in s.split(",", 1)[0]:
        return None
    if not _checksum_ok(s):
        logger.debug("GGA checksum failed: %s", s)
        return None

    fields = s.split("*", 1)[0].split(",")
    # 0:type 1:time 2:lat 3:N/S 4:lon 5:E/W 6:quality 7:numSV 8:HDOP 9:alt 10:unit ...
    if len(fields) < 11:
        return None

    quality = int(_to_float(fields[6]) or 0)
    satellites = int(_to_float(fields[7]) or 0)
    hdop = _to_float(fields[8])
    lat = _dm_to_deg(fields[2], fields[3])
    lng = _dm_to_deg(fields[4], fields[5])

    data = GgaData(
        latitude=lat,
        longitude=lng,
        quality=quality,
        satellites=satellites,
        hdop=hdop,
    )
    if data.has_3d_fix:
        data.altitude_m = _to_float(fields[9])  # MSL metres (unit in fields[10], "M")
    return data


def parse_gsa_vdop(sentence: str) -> Optional[float]:
    """Returns VDOP from a GSA sentence (field 17), or None."""
    if not sentence:
        return None
    s = sentence.strip()
    if "GSA" not in s.split(",", 1)[0]:
        return None
    if not _checksum_ok(s):
        return None
    fields = s.split("*", 1)[0].split(",")
    # ...15:PDOP 16:HDOP 17:VDOP
    if len(fields) < 18:
        return None
    return _to_float(fields[17])


def vertical_accuracy_from_vdop(vdop: Optional[float]) -> Optional[float]:
    """Estimates vertical accuracy (m) from VDOP, or None if VDOP unknown."""
    if vdop is None:
        return None
    return round(vdop * NOMINAL_UERE_M, 2)


class NmeaGPSInterface(GPSInterface):
    """
    GPSInterface driven by raw NMEA sentences fed in via feed().

    A serial/UART reader (real hardware) calls feed() with each line; this
    keeps the latest GGA (position + altitude) and GSA (VDOP → vertical
    accuracy) and exposes them as a GPSFix. Used in place of the no-op base
    GPSInterface when a real NMEA receiver is wired.
    """

    def __init__(self) -> None:
        super().__init__()
        self._last_vdop: Optional[float] = None

    def feed(self, sentence: str) -> None:
        """Processes one NMEA sentence, updating the current fix."""
        gsa_vdop = parse_gsa_vdop(sentence)
        if gsa_vdop is not None:
            self._last_vdop = gsa_vdop
            # Refresh accuracy on the existing fix if we already have one.
            self._fix.altitude_accuracy_m = vertical_accuracy_from_vdop(gsa_vdop)
            return

        gga = parse_gga(sentence)
        if gga is None:
            return

        fix = GPSFix(
            has_fix=gga.quality >= MIN_FIX_QUALITY,
            latitude=gga.latitude,
            longitude=gga.longitude,
            altitude_m=gga.altitude_m,
            altitude_accuracy_m=vertical_accuracy_from_vdop(self._last_vdop),
            fix_quality=_QUALITY_TO_FIXQUALITY.get(gga.quality, FixQuality.NO_FIX),
            satellites=gga.satellites,
        )
        # Preserve heading/speed/accuracy populated by other sources, if any.
        fix.heading_deg = self._fix.heading_deg
        fix.speed_ms = self._fix.speed_ms
        fix.accuracy_m = self._fix.accuracy_m
        self._fix = fix
