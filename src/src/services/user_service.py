"""TODO (Módulo A - SSO): implementar registro/consulta real de estudiantes."""

from src.repositories.user_repo import UserRepository


class UserService:
    def __init__(self, repo: UserRepository):
        self.repo = repo

    async def get_by_whatsapp_number(self, whatsapp_number: str) -> dict | None:
        return await self.repo.find_by_whatsapp_number(whatsapp_number)
