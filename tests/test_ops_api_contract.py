from app.main import app


def test_ops_and_job_openapi_contract():
    spec = app.openapi()
    paths = spec["paths"]

    assert "/api/jobs" in paths
    assert "get" in paths["/api/jobs"]
    assert "/api/jobs/{job_id}/replay" in paths
    assert "post" in paths["/api/jobs/{job_id}/replay"]

    assert "/api/ops/status" in paths
    assert "/api/ops/jobs" in paths
    assert "/api/ops/jobs/{job_id}/replay" in paths

    params = {
        item["name"]
        for item in paths["/api/jobs"]["get"]["parameters"]
    }
    assert {"status", "page", "page_size"}.issubset(params)
