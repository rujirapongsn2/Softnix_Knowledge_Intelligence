"""/ready when the Softnix OCR service is down: degraded while another OCR engine can read scans, unavailable otherwise."""
import socket

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import get_settings
from app.main import app


@pytest.fixture
def ocr_down(monkeypatch):
    """A Softnix OCR URL nothing listens on, with no other dependency configured."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    for name in ("EMBEDDING_BASE_URL", "REDIS_URL", "NEO4J_HTTP_URL", "LIGHTRAG_BASE_URL", "MISTRAL_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("SOFTNIX_OCR_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SOFTNIX_OCR_TOKEN", "test-token")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _ready():
    main_module._PROBE_CACHE.clear()
    with TestClient(app) as client:
        return client.get("/ready")


def test_a_down_ocr_service_with_tesseract_left_is_degraded_not_unavailable(ocr_down, monkeypatch):
    ocr_down.setenv("OCR_CHAIN_ENGINES", "softnix,tesseract")
    get_settings.cache_clear()
    monkeypatch.setattr("app.ocr_chain.shutil.which", lambda name: "/usr/bin/tesseract")
    response = _ready()
    assert response.status_code == 200
    assert response.json()["dependencies"]["ocr_chain"] == "degraded"


def test_a_down_ocr_service_with_no_other_engine_is_unavailable(ocr_down, monkeypatch):
    ocr_down.setenv("OCR_CHAIN_ENGINES", "softnix")
    get_settings.cache_clear()
    response = _ready()
    assert response.status_code == 503
    assert response.json()["error"]["details"]["ocr_chain"] == "unavailable"


def test_tesseract_missing_from_the_image_does_not_count_as_a_fallback(ocr_down, monkeypatch):
    ocr_down.setenv("OCR_CHAIN_ENGINES", "softnix,tesseract")
    get_settings.cache_clear()
    monkeypatch.setattr("app.ocr_chain.shutil.which", lambda name: None)
    assert _ready().status_code == 503
