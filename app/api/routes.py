from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.pixivpy_client import PixivPyClient
from app.core.errors import PixivRemoteError
from app.db.session import get_session
from app.models.work import WorkType
from app.schemas.pixiv import HistoryResponse, WorkResponse
from app.services.cache_service import CacheService

router = APIRouter(prefix="/api")
_client: PixivPyClient | None = None


def get_client() -> PixivPyClient:
    global _client
    if _client is None:
        _client = PixivPyClient()
    return _client


def service(session: AsyncSession) -> CacheService:
    return CacheService(session, get_client())


def raise_http(exc: PixivRemoteError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/illust/{pixiv_id}", response_model=WorkResponse)
async def get_illust(
    pixiv_id: int,
    refresh: bool = Query(True, description="Check Pixiv for a newer version before returning local cache."),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await service(session).get_illust(pixiv_id, refresh)
    except PixivRemoteError as exc:
        raise_http(exc)


@router.get("/novel/{pixiv_id}", response_model=WorkResponse)
async def get_novel(
    pixiv_id: int,
    refresh: bool = Query(True, description="Check Pixiv for a newer version before returning local cache."),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await service(session).get_novel(pixiv_id, refresh)
    except PixivRemoteError as exc:
        raise_http(exc)


@router.get("/illust/{pixiv_id}/history", response_model=HistoryResponse)
async def get_illust_history(pixiv_id: int, session: AsyncSession = Depends(get_session)):
    try:
        return await service(session).get_history(pixiv_id, WorkType.ILLUST)
    except PixivRemoteError as exc:
        raise_http(exc)


@router.get("/novel/{pixiv_id}/history", response_model=HistoryResponse)
async def get_novel_history(pixiv_id: int, session: AsyncSession = Depends(get_session)):
    try:
        return await service(session).get_history(pixiv_id, WorkType.NOVEL)
    except PixivRemoteError as exc:
        raise_http(exc)


@router.get("/illust/{pixiv_id}/versions/{version_no}", response_model=WorkResponse)
async def get_illust_version(pixiv_id: int, version_no: int, session: AsyncSession = Depends(get_session)):
    try:
        return await service(session).get_version(pixiv_id, WorkType.ILLUST, version_no)
    except PixivRemoteError as exc:
        raise_http(exc)


@router.get("/novel/{pixiv_id}/versions/{version_no}", response_model=WorkResponse)
async def get_novel_version(pixiv_id: int, version_no: int, session: AsyncSession = Depends(get_session)):
    try:
        return await service(session).get_version(pixiv_id, WorkType.NOVEL, version_no)
    except PixivRemoteError as exc:
        raise_http(exc)
