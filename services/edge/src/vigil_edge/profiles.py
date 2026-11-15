"""Subject profiles: per-subject severity mapping and alert wording (DESIGN §1.1).

Profiles are data. Adding a subject type (e.g. ``person`` with a fall detector)
means adding an entry here plus a detector, not branching pipeline code.
Wording must describe *observations*, never diagnoses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vigil_edge.models import Severity, SignalType


@dataclass(frozen=True)
class Profile:
    key: str
    description: str
    severities: dict[SignalType, Severity]
    # Short alert text templates; ``{name}`` is the subject's display name.
    alert_text: dict[SignalType, str] = field(default_factory=dict)

    def severity_for(self, signal: SignalType) -> Severity:
        try:
            return self.severities[signal]
        except KeyError:
            raise KeyError(f"profile {self.key!r} has no severity for {signal.value!r}") from None

    def describe(self, signal: SignalType, name: str) -> str:
        template = self.alert_text.get(signal, "Unusual activity observed for {name}")
        return template.format(name=name)


PROFILES: dict[str, Profile] = {
    "dog": Profile(
        key="dog",
        description="Companion dog in a fixed indoor camera view.",
        severities={
            SignalType.CONVULSION: Severity.URGENT,
            SignalType.VOMITING: Severity.WARNING,
        },
        alert_text={
            SignalType.CONVULSION: "Possible seizure-like movement observed for {name}",
            SignalType.VOMITING: "Possible vomiting observed for {name}",
        },
    ),
}


def get_profile(key: str) -> Profile:
    try:
        return PROFILES[key]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise KeyError(f"unknown subject profile {key!r} (known: {known})") from None
