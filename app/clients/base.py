from abc import ABC, abstractmethod
from app.schemas.pixiv import DiscoveryPage, RemoteSnapshot


class PixivClient(ABC):
    @abstractmethod
    async def get_illust_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def get_novel_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def get_authenticated_user_id(self) -> int:
        raise NotImplementedError

    @abstractmethod
    async def discover_author_illusts(
        self, user_id: int, next_params: dict | None = None
    ) -> DiscoveryPage:
        raise NotImplementedError

    @abstractmethod
    async def discover_author_novels(
        self, user_id: int, next_params: dict | None = None
    ) -> DiscoveryPage:
        raise NotImplementedError

    @abstractmethod
    async def discover_bookmark_illusts(
        self, user_id: int, restrict: str, next_params: dict | None = None
    ) -> DiscoveryPage:
        raise NotImplementedError

    @abstractmethod
    async def discover_bookmark_novels(
        self, user_id: int, restrict: str, next_params: dict | None = None
    ) -> DiscoveryPage:
        raise NotImplementedError
