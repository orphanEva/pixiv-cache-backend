from abc import ABC, abstractmethod
from app.schemas.pixiv import DiscoveryPage, RemoteSnapshot
from app.models.work import WorkType


class PixivClient(ABC):
    @abstractmethod
    async def get_illust_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def get_novel_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        raise NotImplementedError

    async def get_authenticated_user_id(self) -> int:
        raise NotImplementedError

    async def get_authenticated_web_user_id(self, *, force: bool = False) -> int:
        raise NotImplementedError

    async def get_account_identity(self, *, force: bool = True) -> dict:
        raise NotImplementedError

    async def ensure_series_followed(self, series_type: str, series_id: int, *,
                                     expected_user_id: str | int | None = None) -> bool:
        raise NotImplementedError

    async def ensure_bookmarked(self, pixiv_id: int, work_type: WorkType, *,
                                restrict: str = 'private',
                                snapshot: RemoteSnapshot | None = None,
                                expected_user_id: str | int | None = None) -> bool:
        raise NotImplementedError

    async def discover_followed_series(self, series_type: str = 'novel') -> list[dict]:
        raise NotImplementedError

    async def discover_author_illusts(
        self, user_id: int, next_params: dict | None = None
    ) -> DiscoveryPage:
        raise NotImplementedError

    async def discover_author_novels(
        self, user_id: int, next_params: dict | None = None
    ) -> DiscoveryPage:
        raise NotImplementedError

    async def discover_bookmark_illusts(
        self, user_id: int, restrict: str, next_params: dict | None = None
    ) -> DiscoveryPage:
        raise NotImplementedError

    async def discover_bookmark_novels(
        self, user_id: int, restrict: str, next_params: dict | None = None
    ) -> DiscoveryPage:
        raise NotImplementedError
