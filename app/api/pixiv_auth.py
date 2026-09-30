"""Private Pixiv authentication health endpoints.

Global API-key middleware protects this router. No credential material is
returned, logged or persisted.
"""
from fastapi import APIRouter, Depends

from app.api.routes import get_client
from app.clients.pixivpy_client import PixivPyClient

router = APIRouter(prefix="/api/admin/pixiv", tags=["pixiv-auth"])


@router.get("/auth/status")
async def pixiv_auth_status(client: PixivPyClient = Depends(get_client)):
    return await client.auth_status()
