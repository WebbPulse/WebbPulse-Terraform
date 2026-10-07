"""Request and response models for projects."""

from __future__ import annotations

from typing import Any, Final, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ....common.workspaces.projects import PROJECT_ID_PATTERN

PROJECT_NAME_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9 _-]*$"
"""Letters, digits, spaces, hyphens and underscores, starting with a letter or digit."""

PROJECT_NAME_MAX_LENGTH: Final = 40
PROJECT_DESCRIPTION_MAX_LENGTH: Final = 256


def _clean_name(value: str) -> str:
    """The name without surrounding whitespace, refused when nothing is left."""
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("A project name cannot be blank.")
    return cleaned


class ProjectCreate(BaseModel):
    """A new project. Its name is unique across the environment, ignoring case."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=PROJECT_NAME_MAX_LENGTH, pattern=PROJECT_NAME_PATTERN)
    description: str = Field(default="", max_length=PROJECT_DESCRIPTION_MAX_LENGTH)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        """Collapse runs of whitespace so two names that read the same compare the same."""
        return _clean_name(value)


class ProjectUpdate(BaseModel):
    """A partial project edit. A null description clears it; the name cannot be cleared."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=PROJECT_NAME_MAX_LENGTH,
        pattern=PROJECT_NAME_PATTERN,
    )
    description: Optional[str] = Field(default=None, max_length=PROJECT_DESCRIPTION_MAX_LENGTH)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: Optional[str]) -> Optional[str]:
        """Collapse runs of whitespace, as a create does."""
        return None if value is None else _clean_name(value)

    @model_validator(mode="before")
    @classmethod
    def _refuse_a_null_name(cls, data: Any) -> Any:
        """A project always has a name, so a null on it is refused rather than read as a clear."""
        if isinstance(data, dict) and "name" in data and data["name"] is None:
            raise ValueError("name cannot be cleared, so null is not an accepted value")
        return data


class Project(BaseModel):
    """A project and how many workspaces it holds."""

    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    name: str
    description: str = ""
    is_default: bool = False
    """True for the default project, which holds every workspace not moved elsewhere
    and can be neither renamed nor deleted."""
    workspace_count: int = 0
    created_at: Optional[str] = None
    """Absent on the default project, which is never stored."""
    updated_at: Optional[str] = None


class ProjectList(BaseModel):
    """Every project, the default first and the rest by name."""

    items: list[Project]
