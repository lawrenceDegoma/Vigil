from __future__ import annotations

from pathlib import Path

import pytest

from vigil_edge.config import (
    ConfigError,
    VigilConfig,
    config_from_dict,
    config_to_dict,
    load_config,
)
from vigil_edge.models import DetectorMode


def test_defaults_are_valid() -> None:
    cfg = VigilConfig()
    assert cfg.detectors.convulsion.mode is DetectorMode.ALERT
    assert cfg.detectors.vomiting.mode is DetectorMode.SHADOW  # experimental by default


def test_example_config_matches_defaults(repo_root: Path) -> None:
    example = load_config(repo_root / "config" / "vigil.example.toml")
    assert config_to_dict(example) == config_to_dict(VigilConfig())


def test_handheld_overrides_load(repo_root: Path) -> None:
    cfg = load_config(repo_root / "config" / "handheld.toml")
    assert cfg.motion.stabilize is True
    assert cfg.window == VigilConfig().window


def test_unknown_key_rejected() -> None:
    with pytest.raises(ConfigError, match="treshold"):
        config_from_dict({"detectors": {"convulsion": {"treshold": 0.3}}})


def test_bad_enum_rejected() -> None:
    with pytest.raises(ConfigError, match="not one of"):
        config_from_dict({"detectors": {"vomiting": {"mode": "loud"}}})


def test_type_errors_rejected() -> None:
    with pytest.raises(ConfigError, match="expected a number"):
        config_from_dict({"source": {"target_fps": "fast"}})
    with pytest.raises(ConfigError, match="expected true/false"):
        config_from_dict({"motion": {"stabilize": 1}})


def test_band_must_respect_nyquist() -> None:
    with pytest.raises(ConfigError, match="Nyquist"):
        config_from_dict({"source": {"target_fps": 8}, "window": {"band_hz": [1.0, 6.0]}})


def test_inverted_range_rejected() -> None:
    with pytest.raises(ConfigError, match="lower bound"):
        config_from_dict({"detectors": {"convulsion": {"activity_ramp": [0.5, 0.1]}}})


def test_rule_version_tracks_detection_settings() -> None:
    a = VigilConfig()
    b = config_from_dict({"detectors": {"convulsion": {"threshold": 0.6}}})
    c = config_from_dict({"subject": {"name": "Biscuit"}})
    assert a.rule_version != b.rule_version
    assert a.rule_version == c.rule_version  # display name doesn't change detection
    assert a.rule_version.startswith("motion-rules-")


def test_missing_file_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")
