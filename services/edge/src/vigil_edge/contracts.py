"""Serialization of in-process events to versioned wire contracts.

See ``packages/contracts``. Stream offsets become UTC timestamps using the
wall-clock time at which the stream started (ADR-008).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from vigil_edge.models import IncidentEvent

INCIDENT_EVENT_SCHEMA = "incident-event.v1"
_FEATURE_TYPES = (int, float, str, bool, type(None))


def _iso(base: datetime, offset: float) -> str:
    ts = (base + timedelta(seconds=offset)).astimezone(UTC)
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def incident_event_message(
    event: IncidentEvent, device_id: str, stream_started_at: datetime
) -> dict[str, Any]:
    if stream_started_at.tzinfo is None:
        raise ValueError("stream_started_at must be timezone-aware")
    inc = event.incident
    features = {
        k: (round(v, 6) if isinstance(v, float) else v)
        for k, v in event.features.items()
        if isinstance(v, _FEATURE_TYPES)
    }
    return {
        "schema_version": INCIDENT_EVENT_SCHEMA,
        "event_id": event.event_id,
        "device_id": device_id,
        "incident_id": inc.id,
        "follows_incident_id": inc.follows_incident_id,
        "event_type": event.event_type.value,
        "signal_type": inc.signal_type.value,
        "severity": inc.severity.value,
        "occurred_at": _iso(stream_started_at, event.at),
        "started_at": _iso(stream_started_at, inc.started_at),
        "ended_at": None if inc.ended_at is None else _iso(stream_started_at, inc.ended_at),
        "confidence": round(min(1.0, max(0.0, inc.peak_score)), 4),
        "rule_version": inc.rule_version,
        "features": features,
    }
