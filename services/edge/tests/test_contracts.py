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
