import psycopg
from fastapi.testclient import TestClient

from map_api.app import PreloadRequest, create_app


class FakePool:
    def __init__(self):
        self.opened = False

    async def open(self):
        self.opened = True

    async def close(self):
        self.opened = False

    def connection(self):
        return self

    async def __aenter__(self):
        raise psycopg.OperationalError("test database unavailable")

    async def __aexit__(self, *_):
        return False


def test_http_live_and_preload_validation():
    pool = FakePool()
    app = create_app(pool=pool)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/health/live").json() == {"status": "live"}
        unavailable = client.get("/health/ready")
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
        response = client.post(
            "/api/map/objects/preload",
            json={"mapId": 3, "bbox": [0, 0, 1, 1], "zoom": 10, "objectTypes": ["alien"]},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"
        response = client.post(
            "/api/map/objects/preload",
            json={"mapId": 3, "bbox": [0, 91, 1, 92], "zoom": 10, "objectTypes": []},
        )
        assert response.status_code == 422


def test_empty_types_are_valid_but_unknown_type_is_rejected():
    request = PreloadRequest(mapId=3, bbox=[170, -10, -170, 10], zoom=5, objectTypes=[])
    assert request.objectTypes == []
    assert request.bbox[0] > request.bbox[2]


def test_local_map_origins_are_allowed_without_allowing_other_origins(monkeypatch):
    monkeypatch.delenv("MAP_API_CORS_ORIGINS", raising=False)
    app = create_app(pool=FakePool())
    with TestClient(app) as client:
        for origin in ("http://localhost:3000", "http://127.0.0.1:3000"):
            response = client.get("/health/live", headers={"Origin": origin})
            assert response.headers["access-control-allow-origin"] == origin
        response = client.get("/health/live", headers={"Origin": "https://other.example"})
        assert "access-control-allow-origin" not in response.headers


def test_pool_lifespan_opens_and_closes(monkeypatch):
    instances = []

    class StubPool:
        def __init__(self, *args, **kwargs):
            self.opened = False
            instances.append(self)

        async def open(self):
            self.opened = True

        async def close(self):
            self.opened = False

    monkeypatch.setattr("map_api.app.AsyncConnectionPool", StubPool)
    app = create_app(settings={"database_url": "postgresql://test"})
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert instances[0].opened
    assert not instances[0].opened
