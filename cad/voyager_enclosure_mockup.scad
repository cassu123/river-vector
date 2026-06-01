// =============================================================================
// River Vector — Voyager Electronics Enclosure Mock-up
// =============================================================================
//
// This is a layout sanity-check, NOT a production design. It verifies that
// two Pis + Pico + buck regulators + relay + dual fans fit comfortably in a
// 220 × 220 × 100 mm IP66 enclosure with airflow paths and connector
// penetrations.
//
// Usage:
//   1. Install OpenSCAD (free — openscad.org).
//   2. Open this file. Press F5 to preview, F6 to render.
//   3. To use in TinkerCAD: File → Export → STL → import to TinkerCAD.
//
// All dimensions in millimeters. Origin (0,0,0) is the bottom-front-left
// inside corner of the enclosure exterior. +X = right, +Y = back, +Z = up.
// =============================================================================

// ---- Parameters (tweak these and re-render) ---------------------------------

// Enclosure (Hammond 1554Q is 220×220×90 — using 100 for headroom)
BOX_W = 220;
BOX_D = 220;
BOX_H = 100;
WALL  = 4;

// Internal aluminum mounting plate
PLATE_T = 2;
PLATE_Z = WALL;        // sits on bottom wall

// Standoff height (rubber vibration mount + spacer)
STANDOFF = 8;

// Raspberry Pi 5 — 85 × 56 × 17 (header + cooler bumps it to ~20)
PI5_W = 85;  PI5_D = 56;  PI5_H = 20;

// Raspberry Pi 4 — 85 × 56 × 17
PI4_W = 85;  PI4_D = 56;  PI4_H = 17;

// RP2040 Pico — 51 × 21 × 4
PICO_W = 51; PICO_D = 21; PICO_H = 4;

// 80mm Noctua-style fan
FAN_SIZE = 80;
FAN_T    = 25;

// Buck converter board (12V → 5V/5A typical)
BUCK_W = 50; BUCK_D = 35; BUCK_H = 18;

// Bosch/Tyco 40A automotive relay (cube style)
RELAY_W = 28; RELAY_D = 28; RELAY_H = 28;

// Bulgin Buccaneer 400-series panel mount (thread ~22mm)
PLUG_D = 24;

// RP-SMA bulkhead for external antenna (separate penetration — see notes)
ANTENNA_D = 10;
// External antenna whip (L-com HG2458RD-09NF — 16" tall, 1" mast)
ANTENNA_WHIP_H = 410;
ANTENNA_MAST_H = 200;
ANTENNA_MAST_D = 25;

// Goretex pressure-equalization vent
VENT_D = 12;

// ---- Enclosure (hollow shell with cutouts) ----------------------------------

module enclosure() {
    color("lightgray", 0.25)
    difference() {
        cube([BOX_W, BOX_D, BOX_H]);

        // Interior void
        translate([WALL, WALL, WALL])
            cube([BOX_W - 2*WALL, BOX_D - 2*WALL, BOX_H - WALL + 0.1]);

        // Intake fan cutout — LEFT wall, vertically centered
        translate([-0.1, BOX_D/2 - FAN_SIZE/2, BOX_H/2 - FAN_SIZE/2])
            cube([WALL + 0.2, FAN_SIZE, FAN_SIZE]);

        // Exhaust fan cutout — RIGHT wall, vertically centered
        translate([BOX_W - WALL - 0.1, BOX_D/2 - FAN_SIZE/2, BOX_H/2 - FAN_SIZE/2])
            cube([WALL + 0.2, FAN_SIZE, FAN_SIZE]);

        // Cannon plug penetration — FRONT wall, lower-right
        translate([BOX_W * 0.72, -0.1, 25])
            rotate([-90, 0, 0])
            cylinder(d=PLUG_D, h=WALL + 0.2, $fn=48);

        // WiFi antenna pigtail — FRONT wall, upper-left
        translate([BOX_W * 0.22, -0.1, BOX_H - 22])
            rotate([-90, 0, 0])
            cylinder(d=ANTENNA_D, h=WALL + 0.2, $fn=32);

        // Goretex vent — BACK wall, top-center
        translate([BOX_W/2, BOX_D + 0.1, BOX_H - 18])
            rotate([90, 0, 0])
            cylinder(d=VENT_D, h=WALL + 0.2, $fn=32);
    }
}

// ---- Internal mounting plate ------------------------------------------------

module mounting_plate() {
    color("tan", 0.7)
    translate([WALL, WALL, PLATE_Z])
        cube([BOX_W - 2*WALL, BOX_D - 2*WALL, PLATE_T]);
}

// ---- Components -------------------------------------------------------------

// Pi 4 — LEFT of center, gets cool intake air FIRST.
// Oriented with the 85mm long axis running front-to-back (Y),
// 56mm across the airflow (X). This keeps a clear airflow lane
// against each side wall for the fans.
module pi4() {
    color("darkgreen")
    translate([40, 50, PLATE_Z + PLATE_T + STANDOFF])
        cube([PI4_D, PI4_W, PI4_H]);   // 56 wide × 85 deep × 17 tall
}

// Pi 5 — RIGHT of center, downstream in airflow (gets warm air, vents to exhaust).
module pi5() {
    color("green")
    translate([120, 50, PLATE_Z + PLATE_T + STANDOFF])
        cube([PI5_D, PI5_W, PI5_H]);   // 56 wide × 85 deep × 20 tall
}

// Pico — small board, back-center between the two Pis
module pico() {
    color("royalblue")
    translate([BOX_W/2 - PICO_W/2, 150, PLATE_Z + PLATE_T + STANDOFF])
        cube([PICO_W, PICO_D, PICO_H]);
}

// Buck regulator — front strip, under the cannon plug entry
module buck() {
    color("red")
    translate([140, WALL + 6, PLATE_Z + PLATE_T])
        cube([BUCK_W, BUCK_D, BUCK_H]);
}

// Master relay — front strip, left of buck
module relay() {
    color("orange")
    translate([85, WALL + 6, PLATE_Z + PLATE_T])
        cube([RELAY_W, RELAY_D, RELAY_H]);
}

// Fans
module intake_fan() {
    color("dimgray", 0.85)
    translate([WALL + 1, BOX_D/2 - FAN_SIZE/2, BOX_H/2 - FAN_SIZE/2])
        cube([FAN_T, FAN_SIZE, FAN_SIZE]);
}

module exhaust_fan() {
    color("dimgray", 0.85)
    translate([BOX_W - WALL - FAN_T - 1, BOX_D/2 - FAN_SIZE/2, BOX_H/2 - FAN_SIZE/2])
        cube([FAN_T, FAN_SIZE, FAN_SIZE]);
}

// ---- External antenna assembly ---------------------------------------------

module antenna_mast() {
    // Mast tube (1" OD, alongside the enclosure's front-left corner)
    color("silver", 0.8)
    translate([BOX_W * 0.22, -40, BOX_H])
        cylinder(d=ANTENNA_MAST_D, h=ANTENNA_MAST_H, $fn=24);
}

module antenna_whip() {
    // The actual radiating element on top of the mast
    color("black")
    translate([BOX_W * 0.22, -40, BOX_H + ANTENNA_MAST_H])
        cylinder(d=12, h=ANTENNA_WHIP_H, $fn=24);
}

module antenna_bracket() {
    // L-bracket attaching the mast to the enclosure / cowl
    color("dimgray")
    translate([BOX_W * 0.22 - 10, -10, BOX_H - 30])
        cube([20, 30, 4]);
}

// ---- Assemble ---------------------------------------------------------------

enclosure();
mounting_plate();
pi5();
pi4();
pico();
buck();
relay();
intake_fan();
exhaust_fan();
antenna_mast();
antenna_whip();
antenna_bracket();
