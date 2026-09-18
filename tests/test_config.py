from __future__ import annotations

from pathlib import Path

import pytest

from cxxprobe_worker.config import ConfigError, WorkerConfig, load_config


def write_config(dir_: Path, name: str, body: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{name}.yaml"
    path.write_text(body)
    return path


def test_loads_minimal_config_with_defaults(tmp_path: Path):
    write_config(tmp_path, "test", "worker_id: w1\n")
    cfg = load_config("test", tmp_path, environ={})
    assert cfg.worker_id == "w1"
    assert cfg.environment == "test"
    assert cfg.concurrency == 1
    assert cfg.judge.binary == "cxxprobe"


def test_missing_config_file_raises_config_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="no config for environment"):
        load_config("nope", tmp_path, environ={})


def test_malformed_yaml_raises_config_error(tmp_path: Path):
    write_config(tmp_path, "test", "worker_id: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config("test", tmp_path, environ={})


def test_non_mapping_top_level_raises_config_error(tmp_path: Path):
    write_config(tmp_path, "test", "- a\n- b\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config("test", tmp_path, environ={})


def test_unknown_key_is_rejected(tmp_path: Path):
    # extra=forbid: a typo'd key must fail loudly rather than being ignored,
    # which is the difference between "my setting didn't apply" and a crash.
    write_config(tmp_path, "test", "worker_id: w1\nconcurency: 4\n")
    with pytest.raises(ConfigError):
        load_config("test", tmp_path, environ={})


def test_invalid_value_is_rejected(tmp_path: Path):
    write_config(tmp_path, "test", "worker_id: w1\nconcurrency: 0\n")
    with pytest.raises(ConfigError):
        load_config("test", tmp_path, environ={})


def test_blank_worker_id_is_rejected(tmp_path: Path):
    write_config(tmp_path, "test", 'worker_id: "   "\n')
    with pytest.raises(ConfigError):
        load_config("test", tmp_path, environ={})


def test_env_override_applies_to_top_level_scalar(tmp_path: Path):
    write_config(tmp_path, "test", "worker_id: w1\nconcurrency: 1\n")
    cfg = load_config("test", tmp_path, environ={"CXXPROBE_WORKER_CONCURRENCY": "8"})
    assert cfg.concurrency == 8


def test_env_override_applies_to_nested_field(tmp_path: Path):
    write_config(tmp_path, "test", "worker_id: w1\njudge:\n  timeout_seconds: 30\n")
    cfg = load_config("test", tmp_path, environ={"CXXPROBE_WORKER_JUDGE__TIMEOUT_SECONDS": "120"})
    assert cfg.judge.timeout_seconds == 120.0
    # The sibling key survives the override rather than being replaced wholesale.
    assert cfg.judge.binary == "cxxprobe"


def test_env_override_coerces_booleans(tmp_path: Path):
    write_config(tmp_path, "test", "worker_id: w1\n")
    cfg = load_config(
        "test", tmp_path, environ={"CXXPROBE_WORKER_WORKSPACE__KEEP_ON_FAILURE": "true"}
    )
    assert cfg.workspace.keep_on_failure is True


def test_unrelated_env_vars_are_ignored(tmp_path: Path):
    write_config(tmp_path, "test", "worker_id: w1\n")
    cfg = load_config("test", tmp_path, environ={"PATH": "/usr/bin", "HOME": "/root"})
    assert cfg.worker_id == "w1"


@pytest.mark.parametrize("environment", ["test", "staging", "prod"])
def test_shipped_config_files_are_valid(environment: str):
    """The configs in config/ must actually load — they're the deploy artifact."""
    cfg = load_config(environment, Path("config"), environ={})
    assert isinstance(cfg, WorkerConfig)
    assert cfg.environment == environment


def test_an_empty_override_clears_a_setting_rather_than_nulling_it(tmp_path):
    """`FOO=` in an environment means empty, not null.

    Every field this is plausibly used on is a `str` whose empty value means
    something: an empty `control_plane.base_url` is standalone mode, an empty
    `secondary_queue_url` is "don't poll a second queue". Parsing them as
    `None` failed validation and took the worker down at startup — the worst
    possible time to learn a setting cannot be turned off.
    """
    (tmp_path / "prod.yaml").write_text(
        "worker_id: w\n"
        "control_plane:\n"
        "  base_url: https://api.example.test\n"
        "queue:\n"
        "  backend: local\n"
    )
    config = load_config(
        "prod",
        config_dir=tmp_path,
        environ={"CXXPROBE_WORKER_CONTROL_PLANE__BASE_URL": ""},
    )
    assert config.control_plane.base_url == ""


def test_typed_overrides_still_parse_as_yaml(tmp_path):
    """Only the empty string is special-cased — everything else is unchanged."""
    (tmp_path / "prod.yaml").write_text("worker_id: w\n")
    config = load_config(
        "prod",
        config_dir=tmp_path,
        environ={
            "CXXPROBE_WORKER_CONCURRENCY": "6",
            "CXXPROBE_WORKER_WORKSPACE__KEEP_ON_FAILURE": "true",
        },
    )
    assert config.concurrency == 6
    assert config.workspace.keep_on_failure is True
