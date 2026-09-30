from abc import ABC, abstractmethod
from app.schemas.pixiv import RemoteSnapshot


class PixivClient(ABC):
    @abstractmethod
    async def get_illust_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def get_novel_snapshot(self, pixiv_id: int) -> RemoteSnapshot:
        raise NotImplementedError
