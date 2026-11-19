"""Central, validated configuration for the LinkedIn Learning automation tool.

Values are resolved in this order (later wins):

1. Model defaults
2. A ``.env`` file in the project root
3. An optional JSON config file (``CONFIG_FILE`` env var or ``--config``)
4. OS environment variables named after the fields (e.g. ``COURSE_URL``)
5. Explicit overrides passed by the CLI

No LinkedIn credentials are ever read, stored or transmitted by this module.
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
URL_SCHEMES = ("http://", "https://", "file://")


def is_supported_url(value: str) -> bool:
    """Return True for URL formats the course loader and browser accept."""

    return (value or "").strip().lower().startswith(URL_SCHEMES)


def _read_dotenv(path: Path) -> dict[str, str]:
    """Minimal ``.env`` reader (``KEY=VALUE``, ``#`` comments, optional quotes)."""

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")
    return {str(k): v for k, v in data.items()}


class Settings(BaseModel):
    """Runtime settings. All fields can be set through environment variables."""

    model_config = ConfigDict(validate_assignment=True, extra="ignore")

    # --- Course / browser -------------------------------------------------
    course_url: str = Field(default="", description="LinkedIn Learning course URL")
    courses_file: Path | None = Field(
        default=None,
        description="CSV or text file containing one course URL per row/line",
    )
    browser_profile: Path = Field(default=PROJECT_ROOT / "browser_profile")
    headless: bool = False
    slow_mo_ms: int = Field(default=0, ge=0, le=5000)
    viewport_width: int = Field(default=1440, ge=640)
    viewport_height: int = Field(default=900, ge=480)

    # --- Playback ---------------------------------------------------------
    playback_speed: float = Field(default=2.0, ge=0.25, le=4.0)
    max_playback_speed: float = Field(default=2.0, ge=0.25, le=4.0)
    poll_interval: float = Field(default=1.0, ge=0.1, le=30.0)
    stall_timeout: float = Field(default=25.0, ge=5.0)
    playback_timeout: float = Field(default=4 * 60 * 60, ge=60.0)
    completion_timeout: float = Field(default=120.0, ge=5.0)

    # --- Behaviour --------------------------------------------------------
    auto_navigate: bool = True
    auto_resume: bool = True
    auto_start: bool = True
    verify_via_course_page: bool = True
    assessment_enabled: bool = True
    assessment_wait_timeout: float = Field(default=300.0, ge=10.0)
    auth_timeout: float = Field(default=300.0, ge=10.0)
    max_retries: int = Field(default=3, ge=0, le=10)
    max_consecutive_failures: int = Field(default=3, ge=1, le=20)

    # --- Persistence / logging -------------------------------------------
    database_path: Path = Field(default=PROJECT_ROOT / "data" / "progress.db")
    log_dir: Path = Field(default=PROJECT_ROOT / "logs")
    log_level: str = "INFO"
    log_json: bool = False
    selectors_path: Path | None = None

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #
    @field_validator("course_url")
    @classmethod
    def _validate_course_url(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        lowered = value.lower()
        if not is_supported_url(lowered):
            raise ValueError("COURSE_URL must start with http://, https:// or file://")
        host = lowered.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
        is_local = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".test")
        if not host.endswith("linkedin.com") and not is_local:
            warnings.warn(
                f"COURSE_URL host '{host}' is not linkedin.com; "
                "continuing because local fixtures are supported, but selectors "
                "and authentication checks are tuned for LinkedIn Learning.",
                stacklevel=2,
            )
        return value

    @field_validator(
        "browser_profile",
        "courses_file",
        "database_path",
        "log_dir",
        "selectors_path",
        mode="before",
    )
    @classmethod
    def _expand_path(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        return Path(str(value)).expanduser()

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = str(value).upper().strip()
        if level not in LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {LOG_LEVELS}")
        return level

    @field_validator("playback_speed")
    @classmethod
    def _validate_speed(cls, value: float) -> float:
        if not 0.25 <= float(value) <= 4.0:
            raise ValueError("PLAYBACK_SPEED must be between 0.25 and 4.0")
        return float(value)

    @model_validator(mode="after")
    def _validate_consistency(self) -> "Settings":
        if self.max_playback_speed < self.playback_speed:
            # The runtime clamps to the player maximum; keep the requested speed
            # as the ceiling so the user's intent is preserved.
            self.max_playback_speed = self.playback_speed
        if self.selectors_path is not None and not self.selectors_path.is_file():
            raise ValueError(f"selectors_path does not exist: {self.selectors_path}")
        return self

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def ensure_directories(self) -> None:
        """Create runtime directories (never contains credentials)."""

        self.browser_profile.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_linkedin(self) -> bool:
        return "linkedin.com" in self.course_url.lower()

    def public_dict(self) -> dict[str, Any]:
        """Safe representation for logs / ``--show-config``."""

        data = self.model_dump()
        return {key: (str(value) if isinstance(value, Path) else value) for key, value in data.items()}

    @classmethod
    def load(
        cls,
        *,
        config_file: Path | str | None = None,
        overrides: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        dotenv_path: Path | None = None,
    ) -> "Settings":
        """Build settings from files, environment variables and overrides.

        ``env`` defaults to ``os.environ`` and exists so tests can pass an
        isolated mapping without mutating the real environment.
        """

        env_map = dict(os.environ if env is None else env)
        values: dict[str, Any] = {}

        # Files may use either UPPER_CASE (environment style) or snake_case.
        canonical = {name.upper(): name for name in cls.model_fields}

        def normalize(data: dict[str, Any]) -> dict[str, Any]:
            return {canonical.get(str(key).upper(), str(key)): value for key, value in data.items()}

        dotenv_file = dotenv_path or (PROJECT_ROOT / ".env")
        values.update(normalize(_read_dotenv(dotenv_file)))

        chosen_config = config_file or env_map.get("CONFIG_FILE") or None
        if chosen_config:
            values.update(normalize(_read_json(Path(chosen_config).expanduser())))

        for name in cls.model_fields:
            env_name = name.upper()
            if env_name in env_map and env_map[env_name] != "":
                values[name] = env_map[env_name]

        if overrides:
            values.update({key: value for key, value in normalize(overrides).items() if value is not None})

        return cls(**values)


def load_settings(
    *,
    config_file: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Convenience wrapper used by ``main.py``."""

    settings = Settings.load(config_file=config_file, overrides=overrides)
    settings.ensure_directories()
    return settings
