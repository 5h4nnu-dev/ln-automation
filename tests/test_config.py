"""Configuration loading and validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from config import Settings


def test_defaults_are_safe(tmp_path: Path) -> None:
    settings = Settings.load(env={}, dotenv_path=tmp_path / "missing.env")
    assert settings.course_url == ""
    assert settings.headless is False
    assert settings.playback_speed == 2.0
    assert settings.auto_navigate is True
    assert settings.auto_resume is True
    assert settings.database_path.name == "progress.db"


def test_environment_overrides(tmp_path: Path) -> None:
    settings = Settings.load(
        env={
            "COURSE_URL": "https://www.linkedin.com/learning/python-essential-training",
            "HEADLESS": "true",
            "PLAYBACK_SPEED": "1.5",
            "AUTO_NAVIGATE": "false",
            "DATABASE_PATH": str(tmp_path / "custom.db"),
            "LOG_LEVEL": "debug",
        },
        dotenv_path=tmp_path / "missing.env",
    )
    assert settings.headless is True
    assert settings.playback_speed == 1.5
    assert settings.auto_navigate is False
    assert settings.database_path == tmp_path / "custom.db"
    assert settings.log_level == "DEBUG"


def test_dotenv_then_env_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nexport COURSE_URL='https://www.linkedin.com/learning/from-dotenv'\nPLAYBACK_SPEED=1.25\n",
        encoding="utf-8",
    )
    settings = Settings.load(
        env={"COURSE_URL": "https://www.linkedin.com/learning/from-env"},
        dotenv_path=env_file,
    )
    assert settings.course_url.endswith("from-env")
    assert settings.playback_speed == 1.25


def test_json_config_file(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text('{"playback_speed": 1.75, "auto_resume": false}', encoding="utf-8")
    settings = Settings.load(config_file=config_file, env={}, dotenv_path=tmp_path / "missing")
    assert settings.playback_speed == 1.75
    assert settings.auto_resume is False


@pytest.mark.parametrize("speed", ["0.1", "20", "-2"])
def test_invalid_playback_speed_rejected(tmp_path: Path, speed: str) -> None:
    with pytest.raises(ValidationError):
        Settings.load(env={"PLAYBACK_SPEED": speed}, dotenv_path=tmp_path / "missing")


def test_sixteen_x_playback_speed_accepted(tmp_path: Path) -> None:
    settings = Settings.load(
        env={"PLAYBACK_SPEED": "16", "MAX_PLAYBACK_SPEED": "16"},
        dotenv_path=tmp_path / "missing",
    )
    assert settings.playback_speed == 16.0
    assert settings.max_playback_speed == 16.0


def test_invalid_course_url_scheme_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings.load(
            env={"COURSE_URL": "www.linkedin.com/learning/foo"},
            dotenv_path=tmp_path / "missing",
        )


def test_local_urls_accepted(tmp_path: Path) -> None:
    settings = Settings.load(
        env={"COURSE_URL": "http://127.0.0.1:8765/course.html"},
        dotenv_path=tmp_path / "missing",
    )
    assert settings.course_url.startswith("http://127.0.0.1")


def test_max_speed_raised_to_requested_speed(tmp_path: Path) -> None:
    settings = Settings(
        playback_speed=3.0,
        max_playback_speed=2.0,
        database_path=tmp_path / "db.sqlite",
    )
    assert settings.max_playback_speed == 3.0


def test_paths_are_expanded(tmp_path: Path) -> None:
    settings = Settings(database_path="~/progress.db", log_dir="~/logs")
    assert not str(settings.database_path).startswith("~")
    assert not str(settings.log_dir).startswith("~")


def test_ensure_directories(tmp_path: Path) -> None:
    settings = Settings(
        browser_profile=tmp_path / "profile",
        database_path=tmp_path / "data" / "progress.db",
        log_dir=tmp_path / "logs",
    )
    settings.ensure_directories()
    assert settings.browser_profile.is_dir()
    assert settings.database_path.parent.is_dir()
    assert settings.log_dir.is_dir()
