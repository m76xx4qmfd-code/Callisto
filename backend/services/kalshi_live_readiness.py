"""Evidence-defined, permanently blocked Kalshi live-readiness v1 assessment.

This module is deliberately static. It has no database, venue, credential,
executor, control-plane, or environment dependency and can never promote live
execution.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


BLOCKERS: tuple[Mapping[str, object], ...] = tuple(
    MappingProxyType(
        {
            "id": blocker_id,
            "claim": claim,
            "status": status,
            "evidence": evidence,
        }
    )
    for blocker_id, claim, status, evidence in (
        (
            "LR-01",
            "No approved operator policy exists for live Kalshi execution.",
            "absent",
            ("docs/architecture/KALSHI_LIVE_READINESS.md",),
        ),
        (
            "LR-02",
            "No authorization distinct from paper operation authorizes live Kalshi writes.",
            "not_implemented",
            ("docs/architecture/KALSHI_LIVE_READINESS.md",),
        ),
        (
            "LR-03",
            "No live arm is bound to both the current runtime instance and operator session.",
            "not_implemented",
            ("docs/architecture/SAFE_LOCAL_RUNTIME_PLAN.md",),
        ),
        (
            "LR-04",
            "The final venue-write boundary does not enforce a current runtime write lease.",
            "not_implemented",
            ("backend/services/venues/kalshi_v2.py",),
        ),
        (
            "LR-05",
            "Portfolio readiness is not assessed by this route and is not connected to activation.",
            "not_assessed",
            ("docs/architecture/KALSHI_AUTHORITATIVE_PORTFOLIO_PROJECTION_PLAN.md",),
        ),
        (
            "LR-06",
            "No complete reviewed Kalshi live submit, acknowledgement, fill, cancel, reconciliation, and position lifecycle exists.",
            "not_implemented",
            ("backend/services/venues/kalshi_v2.py",),
        ),
        (
            "LR-07",
            "Mounted legacy account routes can initialize stored Kalshi credentials on read requests "
            "and are not a live-readiness or authorization boundary.",
            "unsafe",
            ("backend/api/routes_kalshi.py",),
        ),
        (
            "LR-08",
            "Dormant legacy/write-capable paths would violate this contract if enabled.",
            "disabled",
            ("backend/services/venues/kalshi_v2.py",),
        ),
        (
            "LR-09",
            "The mandatory future-live safety regression matrix is absent.",
            "absent",
            ("docs/architecture/KALSHI_LIVE_READINESS.md",),
        ),
    )
)


def build_live_readiness() -> dict[str, object]:
    """Return the immutable blocked assessment without performing any I/O."""
    return {
        "schema_version": "live-readiness/v1",
        "assessment": "permanently_blocked",
        "effective_execution": "disabled",
        "live_ready": False,
        "operator_policy": "absent",
        "risk_limits": "absent",
        "separate_live_authorization": "not_implemented",
        "runtime_session_arm": "not_implemented",
        "final_boundary_write_lease": "not_implemented",
        "portfolio_readiness": "not_assessed",
        "complete_live_lifecycle": "not_implemented",
        "dormant_write_primitive": {
            "exists": True,
            "callable": "KalshiV2Client.create_order(..., allow_writes=True)",
            "status": "disabled",
            "production_route_wiring": "absent",
            "production_runtime_wiring": "absent",
        },
        "blockers": [
            {
                "id": blocker["id"],
                "claim": blocker["claim"],
                "status": blocker["status"],
                "evidence": list(blocker["evidence"]),
            }
            for blocker in BLOCKERS
        ],
    }
