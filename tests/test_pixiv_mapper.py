from app.clients.pixiv_mapper import apply_ugoira_metadata, build_illust_snapshot, build_novel_snapshot, to_dict
from app.models.work import WorkType


class PydanticLike:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self):
        return self.payload


def test_to_dict_supports_pydantic_models():
    value = PydanticLike({"user": PydanticLike({"id": 7, "name": "alice"})})
    assert to_dict(value) == {"user": {"id": 7, "name": "alice"}}


def test_illust_mapper_supports_multi_page_original_urls():
    payload = PydanticLike({
        "id": 10,
        "title": "multi",
        "caption": "demo",
        "page_count": 2,
        "type": "illust",
        "create_date": "2026-01-01T00:00:00+00:00",
        "user": {"id": 99, "name": "artist"},
        "meta_pages": [
            {"image_urls": {"original": "https://i.pximg.net/a_p0.jpg"}},
            {"image_urls": {"original": "https://i.pximg.net/a_p1.png"}},
        ],
    })
    snapshot = build_illust_snapshot(payload, 10)
    assert snapshot.work_type == WorkType.ILLUST
    assert [asset.filename for asset in snapshot.assets] == ["0.jpg", "1.png"]
    assert snapshot.author_id == 99
    assert len(snapshot.version_token) == 64


def test_illust_mapper_supports_single_page_fallback():
    payload = {
        "id": 11,
        "title": "single",
        "page_count": 1,
        "user": {},
        "meta_pages": [],
        "meta_single_page": {"original_image_url": "https://i.pximg.net/single.webp"},
    }
    snapshot = build_illust_snapshot(payload, 11)
    assert snapshot.assets[0].filename == "0.webp"


def test_novel_mapper_uses_novel_text_and_changes_token_with_body():
    detail = {
        "id": 20,
        "title": "story",
        "caption": "x",
        "user": {"id": 1, "name": "writer"},
        "series": {"id": 2, "title": "series"},
    }
    first = build_novel_snapshot(detail, {"novel_text": "hello"}, 20)
    second = build_novel_snapshot(detail, {"novel_text": "hello2"}, 20)
    assert first.work_type == WorkType.NOVEL
    assert first.text_content == "hello"
    assert first.version_token != second.version_token


def test_ugoira_metadata_adds_zip_frames_and_changes_fingerprint():
    snapshot = build_illust_snapshot({
        "id": 30,
        "title": "animated",
        "type": "ugoira",
        "user": {"id": 9, "name": "animator"},
        "meta_single_page": {"original_image_url": "https://i.pximg.net/cover.jpg"},
    }, 30)
    before = snapshot.version_token
    enriched = apply_ugoira_metadata(snapshot, {
        "ugoira_metadata": {
            "zip_urls": {"medium": "https://i.pximg.net/ugoira.zip"},
            "frames": [
                {"file": "000000.jpg", "delay": 100},
                {"file": "000001.jpg", "delay": 200},
            ],
        }
    })
    assert enriched.work_type == WorkType.UGOIRA
    assert enriched.ugoira_zip_url.endswith("ugoira.zip")
    assert [f.delay for f in enriched.ugoira_frames] == [100, 200]
    assert enriched.version_token != before
