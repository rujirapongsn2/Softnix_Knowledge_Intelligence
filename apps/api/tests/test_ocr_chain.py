"""OCR fallback chain: Softnix → Mistral → Tesseract (tha+eng).

Contract under test mirrors anydoc's callback protocol: first engine with
non-empty text wins; a failed engine is skipped; all-fail raises
OCR_CHAIN_FAILED listing every attempt.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from app.config import Settings
from app.ocr_chain import OcrChain, OcrChainError, _parse_engine_order, build_recognizer


def _settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,  # never read the project .env: tests must not inherit real EXT_OCR_* credentials
        softnix_ocr_base_url="https://softnix.test",
        softnix_ocr_token="tok",
        mistral_api_key="mistral-key",
        tesseract_timeout_seconds=5,
        # Fast guards so stall tests run in milliseconds.
        softnix_ocr_timeout_seconds=2,
        softnix_ocr_stall_seconds=0.3,
        softnix_ocr_poll_interval_seconds=0.05,
    )
    base.update(overrides)
    return Settings(**base)


def _softnix_transport(result_text: str = "หน้ากระดาษ", *, fail: bool = False):
    state = {"job": "job-1", "calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        path = request.url.path
        if fail and path.endswith("/v3/ai-process-file") and request.method == "POST":
            return httpx.Response(500)
        if path.endswith("/v3/ai-process-file") and request.method == "POST":
            return httpx.Response(200, json={"job_id": state["job"]})
        if path.endswith(f"/{state['job']}/status"):
            return httpx.Response(200, json={"status": "completed"})
        if path.endswith(f"/{state['job']}/result"):
            return httpx.Response(200, json={"results": {"pages": [{
                "ai_processing": {"success": True, "content": result_text},
            }]}})
        raise AssertionError(f"unexpected {request.method} {path}")

    return httpx.MockTransport(handler)


def _mistral_transport(result_text: str = "Mistral มาร์กดาวน์", *, fail: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(401)
        body = __import__("json").loads(request.content)
        assert body["model"] == "mistral-ocr-latest"
        assert body["document"]["image_url"].startswith("data:image/png;base64,")
        return httpx.Response(200, json={"pages": [{"markdown": result_text}]})

    return httpx.MockTransport(handler)


def _chain(settings, *, softnix=None, mistral=None) -> OcrChain:
    return OcrChain(
        settings,
        clients={
            # MockTransport resolves relative paths against the client's
            # base_url, so each test client keeps a real-looking origin.
            "softnix": httpx.Client(base_url="https://softnix.test",
                                    transport=softnix or _softnix_transport(fail=True)),
            "mistral": httpx.Client(base_url="https://api.mistral.ai/v1",
                                    transport=mistral or _mistral_transport(fail=True)),
        },
    )


def test_first_engine_success_wins():
    chain = _chain(_settings(), softnix=_softnix_transport("ราชกิจจานุเบกษา"))
    assert chain.recognize(b"png", 1) == "ราชกิจจานุเบกษา"


def test_softnix_failure_falls_through_to_mistral():
    chain = _chain(_settings(), softnix=_softnix_transport(fail=True),
                   mistral=_mistral_transport("จาก Mistral"))
    assert chain.recognize(b"png", 2) == "จาก Mistral"


def test_empty_softnix_result_counts_as_failure():
    chain = _chain(_settings(), softnix=_softnix_transport("   "),
                   mistral=_mistral_transport("จาก Mistral"))
    assert chain.recognize(b"png", 1) == "จาก Mistral"


def test_unconfigured_engines_are_skipped_and_reported(monkeypatch):
    # Ensure no inherited EXT_OCR_* alias resurrects Softnix mid-test.
    for var in ("EXT_OCR_BASE_URL", "EXT_OCR_KEY", "EXT_OCR_VERIFY_SSL",
                "EXT_OCR_POLL_INTERVAL_SECONDS", "EXT_OCR_PROCESSING_TIMEOUT_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    settings = _settings(softnix_ocr_base_url="", softnix_ocr_token="", mistral_api_key="")
    chain = OcrChain(settings, clients={})
    # Only tesseract is left; without the binary it fails -> chain error.
    with pytest.raises(OcrChainError) as excinfo:
        chain.recognize(b"png", 1)
    assert "softnix: not configured" in str(excinfo.value)
    assert "mistral: not configured" in str(excinfo.value)


def test_all_failures_raise_chain_error_with_attempts():
    chain = _chain(_settings(), softnix=_softnix_transport(fail=True), mistral=_mistral_transport(fail=True))
    with pytest.raises(OcrChainError) as excinfo:
        chain.recognize(b"png", 1)
    message = str(excinfo.value)
    assert message.startswith("OCR_CHAIN_FAILED")
    assert "softnix" in message and "mistral" in message


def test_engine_order_is_configurable_and_validated():
    assert _parse_engine_order(None) == ("softnix", "mistral", "tesseract")
    assert _parse_engine_order("tesseract,softnix") == ("tesseract", "softnix")
    assert _parse_engine_order("") == ("softnix", "mistral", "tesseract")
    with pytest.raises(ValueError, match="unknown OCR engine"):
        _parse_engine_order("softnix,gpt")


def test_engine_names_lists_only_usable_engines():
    settings = _settings(mistral_api_key="")
    chain = OcrChain(settings, clients={})
    assert chain.engine_names() == ["softnix", "tesseract"]


def _stuck_queue_transport():
    """Softnix queue parked in 'queueing' with no progress movement."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/v3/ai-process-file") and request.method == "POST":
            return httpx.Response(200, json={"job_id": "job-stuck"})
        if path.endswith("/job-stuck/status"):
            return httpx.Response(200, json={"status": "queueing", "progress": {}})
        raise AssertionError(f"unexpected {request.method} {path}")

    return httpx.MockTransport(handler)


def test_stuck_softnix_queue_abandons_early_and_falls_through(monkeypatch):
    """A job that never reports progress is abandoned at the stall budget,
    not the full timeout, so Mistral takes the page quickly."""
    from app.ocr_chain import SoftnixOcrEngine

    engine = SoftnixOcrEngine(
        _settings(),
        httpx.Client(base_url="https://softnix.test", transport=_stuck_queue_transport()),
    )
    import pytest as _pytest

    with _pytest.raises(OcrChainError):
        engine.recognize(b"png", 1)

    # In the full chain the same stall falls through to Mistral.
    chain = _chain(
        _settings(),
        softnix=_stuck_queue_transport(),
        mistral=_mistral_transport("หลังคิวค้าง"),
    )
    assert chain.recognize(b"png", 1) == "หลังคิวค้าง"


def test_progress_updates_reset_the_stall_clock():
    """A slow-but-moving job is NOT abandoned: changing progress resets the
    stall timer, so only a truly frozen queue trips the guard."""
    from app.ocr_chain import SoftnixOcrEngine

    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/v3/ai-process-file") and request.method == "POST":
            return httpx.Response(200, json={"job_id": "job-slow"})
        if path.endswith("/job-slow/status"):
            state["polls"] += 1
            if state["polls"] >= 3:
                return httpx.Response(200, json={"status": "completed"})
            # percent creeps forward on every poll -> moving, not stalled
            return httpx.Response(200, json={"status": "processing",
                                             "progress": {"stage": "ocr", "percent": state["polls"]}})
        if path.endswith("/job-slow/result"):
            return httpx.Response(200, json={"results": {"pages": [{
                "ai_processing": {"success": True, "content": "ผลลัพธ์ช้าแต่คืบหน้า"},
            }]}})
        raise AssertionError(f"unexpected {request.method} {path}")

    engine = SoftnixOcrEngine(
        _settings(softnix_ocr_timeout_seconds=10, softnix_ocr_stall_seconds=0.3),
        httpx.Client(base_url="https://softnix.test", transport=httpx.MockTransport(handler)),
    )
    assert engine.recognize(b"png", 1) == "ผลลัพธ์ช้าแต่คืบหน้า"


def test_build_recognizer_uses_global_settings():
    recognizer = build_recognizer(_settings())
    assert recognizer.engine_order[0] == "softnix"


def test_engines_and_clients_are_reused_across_pages():
    """Regression: the chain must not build a fresh engine/httpx client per
    page — a hundred-page scan would leak a connection pool per page."""
    chain = _chain(_settings())
    softnix_first = chain._build_engine("softnix")
    mistral_first = chain._build_engine("mistral")
    assert chain._build_engine("softnix") is softnix_first
    assert chain._build_engine("mistral") is mistral_first
    # unconfigured engines stay cached as "not configured" too
    unconfigured = OcrChain(_settings(mistral_api_key=""), clients={})
    assert unconfigured._build_engine("mistral") is None
    assert unconfigured._build_engine("mistral") is None


def test_close_releases_clients_the_chain_created():
    chain = OcrChain(_settings(), clients={})  # no external clients
    softnix = chain._build_engine("softnix")
    mistral = chain._build_engine("mistral")
    external = httpx.Client(base_url="https://softnix.test")
    chain_with_external = OcrChain(_settings(), clients={"softnix": external})
    external_engine = chain_with_external._build_engine("softnix")

    chain.close()
    chain_with_external.close()

    # owned clients are closed and dropped; the external one is untouched
    assert "softnix" not in chain.clients and "mistral" not in chain.clients
    softnix_client = getattr(softnix, "client")
    mistral_client = getattr(mistral, "client")
    assert softnix_client.is_closed and mistral_client.is_closed
    assert not external.is_closed and getattr(external_engine, "client") is external


def test_mistral_payload_is_base64_data_uri():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        seen["image_url"] = body["document"]["image_url"]
        return httpx.Response(200, json={"pages": [{"markdown": "ok"}]})

    from app.ocr_chain import MistralOcrEngine

    engine = MistralOcrEngine(
        _settings(),
        httpx.Client(base_url="https://api.mistral.ai/v1", transport=httpx.MockTransport(handler)),
    )
    engine.recognize(b"PNGDATA", 3)
    prefix, _, payload = seen["image_url"].partition(",")
    assert prefix == "data:image/png;base64"
    assert base64.b64decode(payload) == b"PNGDATA"
