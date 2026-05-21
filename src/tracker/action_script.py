from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


class Locator(BaseModel):
    role: str | None = None
    name: str | None = None
    name_contains: str | None = None
    text: str | None = None
    css: str | None = None
    first: bool = False  # use .first when the locator matches multiple elements


class Settle(BaseModel):
    network_idle_ms: int | None = None
    wait_ms: int | None = None
    wait_for_selector: str | None = None
    wait_for_request: str | None = None


class Step(BaseModel):
    id: str
    action: Literal["goto", "click", "fill", "select", "press", "wait"]
    url: str | None = None
    locator: Locator | None = None
    value: str | None = None
    key: str | None = None
    wait_for: Literal["load", "domcontentloaded", "networkidle"] | None = None
    settle: Settle | None = None


class Viewport(BaseModel):
    width: int = 1280
    height: int = 800


class Consent(BaseModel):
    strategy: Literal["accept_all", "pre_seed_cookies", "ignore", "custom"] = "ignore"


class ActionScript(BaseModel):
    session: str
    start_url: str
    viewport: Viewport = Field(default_factory=Viewport)
    consent: Consent = Field(default_factory=Consent)
    steps: list[Step]


def load_script(path: Path) -> ActionScript:
    with open(path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return ActionScript.model_validate(data)
