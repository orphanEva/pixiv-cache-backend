from app.main import app


def test_library_openapi_contract_is_api_first_and_non_destructive():
    spec = app.openapi()
    paths = spec["paths"]

    expected = {
        "/api/library/works",
        "/api/library/works/{pixiv_id}",
        "/api/library/works/{pixiv_id}/history",
        "/api/library/works/{pixiv_id}/versions/{version_no}",
        "/api/library/works/{pixiv_id}/refresh",
        "/api/library/works/{pixiv_id}/integrity",
        "/api/library/authors",
        "/api/library/authors/{author_id}",
        "/api/library/tags",
        "/api/library/series",
        "/api/library/series/{series_id}",
        "/api/library/stats",
    }
    assert expected.issubset(paths)

    work_params = {
        item["name"]
        for item in paths["/api/library/works"]["get"]["parameters"]
    }
    assert {
        "page",
        "page_size",
        "q",
        "type",
        "status",
        "tag",
        "tag_mode",
        "source_id",
        "cached_from",
        "cached_to",
        "remote_created_from",
        "remote_created_to",
    }.issubset(work_params)

    # Management is deliberately conservative in v1.5.
    assert "delete" not in paths["/api/library/works/{pixiv_id}"]
    assert "post" in paths["/api/library/works/{pixiv_id}/refresh"]
