from __future__ import annotations

import pytest

from vigil_edge.models import Severity, SignalType
from vigil_edge.profiles import get_profile


def test_dog_profile_severities() -> None:
    dog = get_profile("dog")
    assert dog.severity_for(SignalType.CONVULSION) is Severity.URGENT
    assert dog.severity_for(SignalType.VOMITING) is Severity.WARNING


def test_alert_wording_is_non_diagnostic() -> None:
    dog = get_profile("dog")
    for signal in (SignalType.CONVULSION, SignalType.VOMITING):
        text = dog.describe(signal, "Biscuit").lower()
        assert "possible" in text
        assert "biscuit" in text
        assert "confirmed" not in text


def test_unknown_profile() -> None:
    with pytest.raises(KeyError, match="unknown subject profile"):
        get_profile("cat")
