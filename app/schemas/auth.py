"""Schemas Pydantic de autenticación."""

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1)


class AdminOut(BaseModel):
    id: int
    username: str
