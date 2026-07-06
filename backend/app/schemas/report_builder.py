"""Schemas for the custom report builder (Phase 3)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReportDefinitionPayload(BaseModel):
    """Ad-hoc run (preview before save)."""
    data_source: str
    definition: dict[str, Any] = Field(default_factory=dict)


class ReportCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    data_source: str
    definition: dict[str, Any] = Field(default_factory=dict)


class ReportUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    data_source: str | None = None
    definition: dict[str, Any] | None = None


class ReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    data_source: str
    definition: dict[str, Any]
    owner_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class ReportColumn(BaseModel):
    key: str
    label: str
    type: str


class ReportRunResult(BaseModel):
    data_source: str
    columns: list[ReportColumn]
    rows: list[dict[str, Any]]
    row_count: int
    grouped: bool
    truncated: bool
    limit: int
