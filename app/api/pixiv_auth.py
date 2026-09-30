"""Private Pixiv authentication endpoints.

Global API-key middleware protects this router. No credential material is
returned or logged.
"""
from fastapi import APIRouter, Depends

from app.api.routes import get_client
from app.clients.pixivpy_client import PixivPyClient

router = APIRouter(prefix="/api/admin/pixiv", tags=["pixiv-auth"])


@router.get("/auth/status")
async def pixiv_auth_status(client: PixivPyClient = Depends(get_client)):
    return await client.auth_status()


@router.post("/auth/refresh")
async def pixiv_auth_refresh(client: PixivPyClient = Depends(get_client)):
    """Explicitly recover/renew App token and Web Cookie when AUTO_AUTH is enabled."""
    return await client.recover_auth()
