"""Contract schemas must be valid JSON Schema documents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMAS = ["incident-event.v1.schema.json", "heartbeat.v1.schema.json"]


@pytest.mark.parametrize("name", SCHEMAS)
def test_schema_is_valid(repo_root: Path, name: str) -> None:
    schema = json.loads((repo_root / "packages/contracts/schemas" / name).read_text())
    Draft202012Validator.check_schema(schema)


def test_incident_messages_validate_against_schema(repo_root: Path) -> None:
    from datetime import UTC, datetime

    from vigil_edge.config import VigilConfig
    from vigil_edge.contracts import incident_event_message
    from vigil_edge.pipeline import run_pipeline
    from vigil_edge.sources.synthetic import SyntheticSource, get_scenario

    schema = json.loads(
        (repo_root / "packages/contracts/schemas/incident-event.v1.schema.json").read_text()
    )
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    res = run_pipeline(SyntheticSource(get_scenario("convulsion"), fps=15), VigilConfig())
    start = datetime(2026, 11, 14, 20, 0, tzinfo=UTC)
    messages = [incident_event_message(e, "pi-livingroom", start) for e in res.events]
    assert {m["event_type"] for m in messages} == {"incident_opened", "incident_ended"}
    for msg in messages:
        validator.validate(msg)
    assert messages[0]["started_at"].startswith("2026-11-14T20:00:")
    assert all("image" not in k for m in messages for k in m["features"])


def test_naive_start_time_rejected() -> None:
    from datetime import datetime

    from vigil_edge.contracts import incident_event_message

    with pytest.raises(ValueError, match="timezone"):
        incident_event_message(None, "d", datetime(2026, 1, 1))  # type: ignore[arg-type]
