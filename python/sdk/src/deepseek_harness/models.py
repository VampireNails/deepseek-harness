from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from pydantic import BaseModel

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


@dataclass(slots=True)
class Notification:
    method: str
    payload: JsonObject


@dataclass(slots=True)
class IncomingRequest:
    id: str | int
    method: str
    payload: JsonObject


class ServerInfo(BaseModel):
    name: str | None = None
    version: str | None = None


class InitializeResponse(BaseModel):
    serverInfo: ServerInfo | None = None


class SessionDeletionCapability(BaseModel):
    """Irreversible deletion available for unowned cold ordinary Sessions."""

    available: bool
    scope: Literal["cold-ordinary-session"]


class SessionDeletionResult(BaseModel):
    """Physical outcome; incomplete and unknown never acknowledge success."""

    sessionId: str
    status: Literal["deleted", "not-found", "refused", "incomplete", "unknown"]
    reason: Literal["unavailable", "writer-held", "live-owner", "active-schedule", "unsupported-object", "unsafe-storage"] | None = None
