from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.core.errors import PixivNotFoundError, PixivRestrictedError
from app.models.work import WorkType
from app.schemas.pixiv import RemoteAsset, RemoteSnapshot


def to_dict(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: to_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dict(v) for v in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return to_dict(model_dump())
    items = getattr(value, "items", None)
    if callable(items):
        return {k: to_dict(v) for k, v in items()}
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, dict):
        return {k: to_dict(v) for k, v in raw.items() if not k.startswith("_")}
    return value


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def version_token(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def suffix_from_url(url: str) -> str:
    path = url.split("?", 1)[0]
    suffix = "." + path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ".jpg"
    return suffix if len(suffix) <= 6 else ".jpg"


def build_illust_snapshot(payload: Any, pixiv_id: int) -> RemoteSnapshot:
    illust = to_dict(payload)
    if not isinstance(illust, dict) or illust.get("id") is None:
        raise PixivNotFoundError(f"Pixiv illust {pixiv_id} not found")

    assets: list[RemoteAsset] = []
    meta_pages = illust.get("meta_pages") or []
    if meta_pages:
        for idx, page in enumerate(meta_pages):
            page = to_dict(page) or {}
            urls = page.get("image_urls") or {}
            url = urls.get("original") or urls.get("large")
            if url:
                assets.append(RemoteAsset(page_index=idx, url=url, filename=f"{idx}{suffix_from_url(url)}"))
    else:
        urls = illust.get("meta_single_page") or {}
        url = urls.get("original_image_url") or (illust.get("image_urls") or {}).get("large")
        if url:
            assets.append(RemoteAsset(page_index=0, url=url, filename=f"0{suffix_from_url(url)}"))

    if not assets:
        raise PixivRestrictedError(f"Pixiv illust {pixiv_id} has no downloadable image URL")

    user = illust.get("user") or {}
    update_dt = parse_dt(illust.get("updated_at") or illust.get("update_date"))
    fingerprint = {
        "id": illust.get("id"),
        "title": illust.get("title"),
        "caption": illust.get("caption"),
        "tags": illust.get("tags"),
        "page_count": illust.get("page_count"),
        "type": illust.get("type"),
        "assets": [a.model_dump() for a in assets],
        "updated_at": update_dt,
    }
    return RemoteSnapshot(
        pixiv_id=pixiv_id,
        work_type=WorkType.ILLUST,
        title=illust.get("title") or "",
        author_id=user.get("id"),
        author_name=user.get("name"),
        created_at=parse_dt(illust.get("create_date")),
        updated_at=update_dt,
        metadata=illust,
        assets=assets,
        version_token=version_token(fingerprint),
    )


def build_novel_snapshot(detail_payload: Any, text_payload: Any, pixiv_id: int) -> RemoteSnapshot:
    novel = to_dict(detail_payload)
    text_data = to_dict(text_payload)
    if not isinstance(novel, dict) or novel.get("id") is None:
        raise PixivNotFoundError(f"Pixiv novel {pixiv_id} not found")
    if not isinstance(text_data, dict):
        text_data = {}

    text = text_data.get("novel_text") or text_data.get("text") or ""
    user = novel.get("user") or {}
    update_dt = parse_dt(novel.get("updated_at") or novel.get("update_date"))
    fingerprint = {
        "id": novel.get("id"),
        "title": novel.get("title"),
        "caption": novel.get("caption"),
        "tags": novel.get("tags"),
        "series": novel.get("series"),
        "text": text,
        "updated_at": update_dt,
    }
    return RemoteSnapshot(
        pixiv_id=pixiv_id,
        work_type=WorkType.NOVEL,
        title=novel.get("title") or "",
        author_id=user.get("id"),
        author_name=user.get("name"),
        created_at=parse_dt(novel.get("create_date")),
        updated_at=update_dt,
        metadata=novel,
        text_content=text,
        version_token=version_token(fingerprint),
    )
