"""Tests for NMEA altitude extraction (hardware/nmea.py)."""

import unittest

from hardware.nmea import (
    NmeaGPSInterface,
    parse_gga,
    parse_gsa_vdop,
    vertical_accuracy_from_vdop,
)

# Real-shaped GGA with valid checksum: quality=1, 8 sats, HDOP 0.9, altitude 545.4 M.
GGA_3D = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
# quality=0 (no fix). Checksum omitted (parser is lenient when absent).
GGA_NOFIX = "$GPGGA,123519,4807.038,N,01131.000,E,0,08,0.9,545.4,M,46.9,M,,"
# quality=1 but only 3 satellites (< 4).
GGA_FEWSATS = "$GPGGA,123519,4807.038,N,01131.000,E,1,03,2.5,545.4,M,46.9,M,,"
# GSA with VDOP 1.8 in field 17.
GSA = "$GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,1.8"


class TestParseGga(unittest.TestCase):
    def test_valid_3d_fix_has_altitude(self):
        gga = parse_gga(GGA_3D)
        self.assertIsNotNone(gga)
        self.assertTrue(gga.has_3d_fix)
        self.assertAlmostEqual(gga.altitude_m, 545.4, places=1)
        self.assertEqual(gga.satellites, 8)
        self.assertAlmostEqual(gga.latitude, 48.1173, places=3)

    def test_altitude_none_when_quality_zero(self):
        gga = parse_gga(GGA_NOFIX)
        self.assertIsNotNone(gga)
        self.assertFalse(gga.has_3d_fix)
        self.assertIsNone(gga.altitude_m)

    def test_altitude_none_when_too_few_satellites(self):
        gga = parse_gga(GGA_FEWSATS)
        self.assertIsNotNone(gga)
        self.assertFalse(gga.has_3d_fix)
        self.assertIsNone(gga.altitude_m)

    def test_non_gga_returns_none(self):
        self.assertIsNone(parse_gga("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,,*1D"))


class TestVdop(unittest.TestCase):
    def test_parse_gsa_vdop(self):
        self.assertAlmostEqual(parse_gsa_vdop(GSA), 1.8, places=1)

    def test_vertical_accuracy_from_vdop(self):
        self.assertEqual(vertical_accuracy_from_vdop(1.8), round(1.8 * 5.0, 2))
        self.assertIsNone(vertical_accuracy_from_vdop(None))


class TestNmeaInterface(unittest.TestCase):
    def test_feed_populates_fix_altitude_and_accuracy(self):
        gps = NmeaGPSInterface()
        gps.feed(GSA)        # establishes VDOP first
        gps.feed(GGA_3D)
        fix = gps.fix
        self.assertTrue(fix.has_fix)
        self.assertAlmostEqual(fix.altitude_m, 545.4, places=1)
        self.assertEqual(fix.altitude_accuracy_m, round(1.8 * 5.0, 2))

    def test_feed_nofix_leaves_altitude_none(self):
        gps = NmeaGPSInterface()
        gps.feed(GGA_NOFIX)
        self.assertIsNone(gps.fix.altitude_m)


if __name__ == "__main__":
    unittest.main()
