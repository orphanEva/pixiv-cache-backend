"""Pixiv Token/Cookie validation endpoints.

Global API-key middleware protects this router. No credential material is
returned or logged.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.api.routes import get_client
from app.clients.pixivpy_client import PixivPyClient
from app.core.errors import PixivRemoteError

router = APIRouter(prefix="/api/admin/pixiv", tags=["pixiv-auth"])


@router.get("/auth/status")
async def pixiv_auth_status(client: PixivPyClient = Depends(get_client)):
    return await client.auth_status()


@router.post("/auth/refresh")
async def pixiv_auth_refresh(client: PixivPyClient = Depends(get_client)):
    """Revalidate configured App token and Web Cookie without account login."""
    try:
        return await client.recover_auth()
    except PixivRemoteError as exc:
        raise HTTPException(exc.status_code, type(exc).__name__) from exc
