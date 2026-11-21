"""TODO (Módulo A - SSO): expandir cuando se implemente el flujo de autenticación completo."""

from pydantic import BaseModel


class UserBase(BaseModel):
    student_id: str
    full_name: str
    whatsapp_number: str
    email: str | None = None


class UserCreate(UserBase):
    pass


class UserResponse(UserBase):
    id: str
