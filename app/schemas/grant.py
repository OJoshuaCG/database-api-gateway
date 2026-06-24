"""Schemas Pydantic para los endpoints de grants granulares."""

from pydantic import BaseModel, Field

from app.services.db_admin.dtos import GrantInfo, GrantLevel, ObjectRef


class GrantRequest(BaseModel):
    level: GrantLevel
    object_ref: ObjectRef
    privileges: list[str] = Field(min_length=1)
    with_grant_option: bool = False


class RevokeRequest(BaseModel):
    level: GrantLevel
    object_ref: ObjectRef
    privileges: list[str] = Field(min_length=1)


class GrantableRequest(BaseModel):
    level: GrantLevel
    object_ref: ObjectRef
    privileges: list[str] = Field(min_length=1)


class GrantableResult(BaseModel):
    can_grant: bool
    level: GrantLevel
    privileges: list[str]


# Re-export GrantInfo as the list-grants output type.
__all__ = ["GrantRequest", "RevokeRequest", "GrantableRequest", "GrantableResult", "GrantInfo"]
