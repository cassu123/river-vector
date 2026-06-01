"""
River Vector - Compute Topology

Defines how the subsystems of ONE logical mower ("unit") are distributed
across one or more physical computers ("nodes").

The same River Vector codebase runs in two shapes, selected by config only —
no code fork:

  * SOLO  — one node owns every role. A single capable machine (a Chromebox,
            or just the Pi 5 by itself) runs the whole stack. This is the
            historical default and requires zero topology configuration.

  * SPLIT — roles are spread across nodes that talk over the internal LAN.
            For Voyager: the Pi 5 owns `control` (real-time autonomy + safety)
            and the Pi 4 owns `vision` (cameras + CV), reachable as a peer.

Roles
-----
  control  Autonomy, safety, mode manager, command/telemetry channels, drive,
           GPS/IMU, the Pico bridge, e-stop and watchdog. SAFETY-CRITICAL and
           hard real-time. **Never delegated over the network** — the control
           node is the authority and must own this role locally.
  vision   Cameras, undistortion, ArUco detection, MJPEG streaming. Not in the
           hard real-time loop, so it is safe to run on a separate node and
           reach over the LAN. If the vision peer is unreachable, the control
           node degrades cameras to sim mode (same path as missing hardware).

The topology lives in the per-NODE bootstrap (it is a physical fact of THIS
box — where the cameras are wired), NOT in the per-UNIT config pulled from
River Song. River Song stays topology-agnostic: it knows the unit has N
cameras, not which board they hang off. You can re-wire boxes without touching
the server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Roles & topologies
# ──────────────────────────────────────────────────────────────────────────

ROLE_CONTROL = "control"
ROLE_VISION = "vision"
ALL_ROLES = (ROLE_CONTROL, ROLE_VISION)

# Roles that own safety-critical, hard real-time subsystems. These may never
# be reached over the network — the control node must own them locally.
SAFETY_CRITICAL_ROLES = frozenset({ROLE_CONTROL})

TOPOLOGY_SOLO = "solo"
TOPOLOGY_SPLIT = "split"
ALL_TOPOLOGIES = (TOPOLOGY_SOLO, TOPOLOGY_SPLIT)

# Default port the vision node serves its snapshot/ArUco HTTP API on.
DEFAULT_VISION_PORT = 8090


class ComputeTopologyError(Exception):
    """Raised when a compute profile is structurally invalid or unsafe."""


# ──────────────────────────────────────────────────────────────────────────
# Profile
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class ComputeProfile:
    """
    This node's place in the unit's compute topology.

    Attributes:
        node_id:  Stable identifier for this physical box (e.g. "voyager-ctrl").
        topology: TOPOLOGY_SOLO or TOPOLOGY_SPLIT.
        roles:    Roles this node owns and runs locally.
        peers:    Map of {role -> base_url} for roles this node delegates to
                  another node. e.g. {"vision": "http://10.55.0.2:8090"}.
    """

    node_id: str = ""
    topology: str = TOPOLOGY_SOLO
    roles: List[str] = field(default_factory=lambda: list(ALL_ROLES))
    peers: Dict[str, str] = field(default_factory=dict)

    # ── Construction ────────────────────────────────────────────────────

    @classmethod
    def default_solo(cls, node_id: str = "") -> "ComputeProfile":
        """A single node that owns every role. The zero-config default."""
        return cls(
            node_id=node_id,
            topology=TOPOLOGY_SOLO,
            roles=list(ALL_ROLES),
            peers={},
        )

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ComputeProfile":
        """
        Builds a profile from a parsed dict. A missing or empty block yields
        the solo default — this is what makes old bootstraps (written before
        compute topology existed) keep working unchanged.
        """
        if not d:
            return cls.default_solo()
        roles = d.get("roles")
        return cls(
            node_id=d.get("node_id", ""),
            topology=d.get("topology", TOPOLOGY_SOLO),
            roles=list(roles) if roles else list(ALL_ROLES),
            peers=dict(d.get("peers", {}) or {}),
        )

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "topology": self.topology,
            "roles": list(self.roles),
            "peers": dict(self.peers),
        }

    # ── Queries ─────────────────────────────────────────────────────────

    def owns_role(self, role: str) -> bool:
        """True if this node runs `role` locally."""
        return role in self.roles

    def peer_url(self, role: str) -> Optional[str]:
        """Base URL of the node that owns `role`, or None if local/unknown."""
        return self.peers.get(role)

    def location_of(self, role: str) -> str:
        """Human-readable location: 'local', a peer URL, or 'unavailable'."""
        if self.owns_role(role):
            return "local"
        return self.peers.get(role, "unavailable")

    # ── Validation ──────────────────────────────────────────────────────

    def problems(self) -> List[str]:
        """
        Returns a list of structural/safety problems. Empty list == valid.

        Rules:
          * topology must be known.
          * roles must be a non-empty, unique subset of ALL_ROLES.
          * peers may only reference known roles.
          * a role is either owned locally OR delegated to a peer — never both.
          * SAFETY: a safety-critical role may never be delegated to a peer.
          * SOLO: one node owns everything and has no peers.
        """
        issues: List[str] = []

        if self.topology not in ALL_TOPOLOGIES:
            issues.append(
                f"unknown topology '{self.topology}' (expected one of {ALL_TOPOLOGIES})"
            )

        if not self.roles:
            issues.append("node owns no roles")
        unknown_roles = [r for r in self.roles if r not in ALL_ROLES]
        if unknown_roles:
            issues.append(f"unknown role(s) {unknown_roles} (expected {list(ALL_ROLES)})")
        if len(set(self.roles)) != len(self.roles):
            issues.append("duplicate roles declared")

        unknown_peers = [r for r in self.peers if r not in ALL_ROLES]
        if unknown_peers:
            issues.append(f"peer declared for unknown role(s) {unknown_peers}")

        both = [r for r in self.peers if r in self.roles]
        if both:
            issues.append(
                f"role(s) {both} declared both locally-owned and as a peer — pick one"
            )

        unsafe = [r for r in self.peers if r in SAFETY_CRITICAL_ROLES]
        if unsafe:
            issues.append(
                f"safety-critical role(s) {unsafe} may NEVER be delegated to a peer; "
                "they must run on the control node locally"
            )

        if self.topology == TOPOLOGY_SOLO:
            missing = [r for r in ALL_ROLES if r not in self.roles]
            if missing:
                issues.append(f"solo node must own every role; missing {missing}")
            if self.peers:
                issues.append("solo node must not declare peers")

        if self.topology == TOPOLOGY_SPLIT:
            # Every role this node neither owns nor delegates is a gap.
            unresolved = [
                r for r in ALL_ROLES
                if r not in self.roles and r not in self.peers
            ]
            # Unresolved roles are only a problem for roles this process needs;
            # a vision-only node legitimately neither owns nor peers `control`.
            # Callers that require a role assert it explicitly (see main.py).
            if unresolved:
                logger.debug("split node leaves role(s) unresolved: %s", unresolved)

        return issues

    def is_valid(self) -> bool:
        return not self.problems()

    def validated(self) -> "ComputeProfile":
        """Returns self if valid; raises ComputeTopologyError otherwise."""
        issues = self.problems()
        if issues:
            raise ComputeTopologyError(
                "Invalid compute profile:\n  - " + "\n  - ".join(issues)
            )
        return self

    def describe(self) -> str:
        """One-line human summary for logs."""
        owned = ",".join(self.roles) or "(none)"
        if self.peers:
            peered = ", ".join(f"{r}→{u}" for r, u in self.peers.items())
            return f"node={self.node_id or '(unnamed)'} topology={self.topology} owns=[{owned}] peers=[{peered}]"
        return f"node={self.node_id or '(unnamed)'} topology={self.topology} owns=[{owned}]"
