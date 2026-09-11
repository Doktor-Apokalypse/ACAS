"""Validated request and response contracts for the HTTP API."""

from typing import Literal

from pydantic import BaseModel, Field

from app_config import MAX_MESSAGE_CHARS
from authentication import MAX_PASSWORD_LENGTH, MIN_NEW_PASSWORD_LENGTH


class ChatRequest(BaseModel):
    chat_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    mode: Literal["chat", "analyse"] = "chat"


class ChatResponse(BaseModel):
    chat_id: str
    title: str
    reply: str
    reply_message_id: int | None = None


class ProjectAnalysisRequest(BaseModel):
    retry_failed: bool = False


class RenameProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class RenameChatRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class RegistrationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class CompleteRegistrationRequest(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    username: str = Field(min_length=3, max_length=30)
    password: str = Field(
        min_length=MIN_NEW_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH
    )


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class PasswordResetRequest(BaseModel):
    identity: str = Field(min_length=1, max_length=254)


class CompletePasswordResetRequest(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(
        min_length=MIN_NEW_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH
    )


class AdminUserAction(BaseModel):
    action: Literal[
        "make_admin",
        "remove_admin",
        "ban",
        "unban",
        "revoke_sessions",
        "unlock",
    ]
    reason: str | None = Field(default=None, max_length=500)
    expires_in_hours: int | None = Field(default=None, ge=1, le=8_760)


class AdminUserLimits(BaseModel):
    storage_limit_bytes: int | None = Field(
        default=None, ge=1_048_576, le=1_099_511_627_776
    )
    active_job_limit: int | None = Field(default=None, ge=1, le=100)
    pending_input_char_limit: int | None = Field(
        default=None, ge=1, le=100_000_000
    )


class AdminAccountDisposition(BaseModel):
    mode: Literal["delete", "anonymize"]
    confirmation: str = Field(min_length=1, max_length=30)


class AdminRegistrationSetting(BaseModel):
    enabled: bool


class AdminAiWorkSetting(BaseModel):
    enabled: bool


class ProjectEntryDelete(BaseModel):
    kind: Literal["file", "folder", "upload"]
    file_id: int | None = Field(default=None, ge=1)
    batch_id: int | None = Field(default=None, ge=1)
    path: str | None = Field(default=None, max_length=500)


class ProjectMainFile(BaseModel):
    file_id: int | None = Field(default=None, ge=1)


class AdminAnnouncementSetting(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    level: Literal["info", "warning", "critical"] = "info"
    expires_in_hours: int | None = Field(default=None, ge=1, le=720)
