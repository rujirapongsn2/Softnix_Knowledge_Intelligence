"""OCR fallback chain for anydoc's ``to_markdown_with_ocr`` callback.

Engine order (default ``softnix,mistral,tesseract``, configurable via
``OCR_CHAIN_ENGINES``):

1. **Softnix OCR** — the company's self-hosted service. Best Thai accuracy;
   job-based v3 API (submit → poll → result). Preferred for every page.
2. **Mistral OCR** — cloud API paid per page, so it sits behind Softnix and
   is only reached when Softnix fails or is not configured.
3. **Tesseract (tha+eng)** — local CLI. Always available when installed and
   the last line of defence when both network engines are unreachable.
   Deliberately last: on Thai scans it usually *succeeds with poor quality*
   (a space between every glyph), so a first-position placement would end
   the chain there and degrade every page it touches.

Contract (matches anydoc's Python callback protocol):

- ``recognize(image: bytes, page: int) -> str`` — PNG bytes of one rendered
  page plus its 1-based number; returns the recognized text.
- Returning ``""`` leaves that page's text layer untouched (anydoc keeps
  whatever the layer had); raising aborts the whole conversion.
- The chain tries engines in order and the first non-empty text wins. When
  every engine fails it raises ``RuntimeError("OCR_CHAIN_FAILED: ...")`` so
  the worker's error mapping can classify the failure.
"""

from __future__ import annotations

from collections.abc import Callable
import base64
import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int], None]

#: Valid engine names, in the default order.
DEFAULT_CHAIN_ENGINES = ("softnix", "mistral", "tesseract")


class OcrChainError(RuntimeError):
    """Every configured engine failed for a page."""

    def __init__(self, attempts: list[str]):
        self.attempts = attempts
        super().__init__("OCR_CHAIN_FAILED: " + "; ".join(attempts))


@dataclass
class _Attempt:
    engine: str
    error: str | None = None
    ok: bool = False


class SoftnixOcrEngine:
    """Softnix OCR v3: submit the rendered page, poll, read the result.

    The page image is sent as ``image/png`` (anydoc already rendered it),
    with structured extraction disabled — only Markdown text is wanted.

    Three guards keep a degraded Softnix queue from stalling the document:

    - **Per-request timeout** — submit/status/result calls fail fast.
    - **Overall page budget** — ``softnix_ocr_timeout_seconds`` caps the
      whole job; exceeding it falls through to the next engine.
    - **Stall detection** — the known failure mode is a job parked in
      ``queueing`` that never reports progress. When the (state, progress)
      signature stops changing for ``softnix_ocr_stall_seconds`` the job is
      abandoned instead of waiting out the full budget, so Mistral takes
      over within seconds of a stuck queue rather than minutes.
    """

    name = "softnix"

    def __init__(self, settings: Settings, client: httpx.Client | None = None,
                 on_progress: ProgressCallback | None = None):
        self.settings = settings
        self.on_progress = on_progress
        self.client = client or httpx.Client(
            base_url=settings.softnix_ocr_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.softnix_ocr_token}"},
            verify=not settings.softnix_ocr_insecure_tls,
            timeout=httpx.Timeout(settings.softnix_ocr_request_timeout_seconds),
        )

    def recognize(self, image: bytes, page: int) -> str:
        try:
            submit = self.client.post(
                "/v3/ai-process-file",
                files={"file": ("page.png", image, "image/png")},
                data={"disable_structure": "true", "use_thinking": "false"},
            )
            submit.raise_for_status()
            job_id = submit.json().get("job_id")
            if not job_id:
                raise ValueError("submit returned no job_id")
            return self._wait(job_id)
        except OcrChainError:
            raise
        except httpx.HTTPError as exc:
            raise OcrChainError([f"softnix: {type(exc).__name__}"]) from exc
        except ValueError as exc:
            # stalled / failed job / timeout — engine-level outcomes the
            # chain treats as "try the next engine".
            raise OcrChainError([f"softnix: {exc}"]) from exc

    def _wait(self, job_id: str) -> str:
        deadline = time.monotonic() + self.settings.softnix_ocr_timeout_seconds
        stall_budget = self.settings.softnix_ocr_stall_seconds
        interval = min(self.settings.softnix_ocr_poll_interval_seconds, max(stall_budget / 4.0, 0.5))
        last_signature: tuple | None = None
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            status_response = self.client.get(f"/v3/ai-process-file/{job_id}/status")
            status_response.raise_for_status()
            payload = status_response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"unexpected status payload: {type(payload).__name__}")
            state = payload.get("status")
            progress = payload.get("progress")
            if not isinstance(progress, dict):
                progress = {}
            if state in {"completed", "partial_success"}:
                return self._result(job_id)
            if state in {"failed", "cancelled"}:
                raise ValueError(f"job {state}")
            signature = (state, progress.get("stage"), progress.get("percent"))
            if signature != last_signature:
                last_signature = signature
                last_change = time.monotonic()
                if self.on_progress:
                    self.on_progress(f"ocr_chain_softnix_{progress.get('stage') or state}", 20)
            elif time.monotonic() - last_change >= stall_budget:
                # Queue stuck with no movement: abandon early so the next
                # engine (Mistral) can take the page within seconds.
                logger.warning(
                    "Softnix job %s stalled in '%s' for %.0fs; abandoning (budget %.0fs)",
                    job_id, state, time.monotonic() - last_change, stall_budget,
                )
                raise ValueError(f"stalled in {state!r} for {stall_budget:.0f}s")
            time.sleep(interval)
        raise ValueError("timeout")

    def _result(self, job_id: str) -> str:
        response = self.client.get(f"/v3/ai-process-file/{job_id}/result")
        response.raise_for_status()
        payload = response.json().get("results") or {}
        for page_row in payload.get("pages") or []:
            ai = page_row.get("ai_processing") or {}
            if ai.get("success") and (ai.get("content") or "").strip():
                return ai["content"]
            if (page_row.get("ocr_text") or "").strip():
                return page_row["ocr_text"]
        return (payload.get("combined_markdown") or "").strip()


class MistralOcrEngine:
    """Mistral OCR: one synchronous request per page, paid per use."""

    name = "mistral"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(
            base_url="https://api.mistral.ai/v1",
            headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
            timeout=httpx.Timeout(settings.mistral_ocr_timeout_seconds),
        )

    def recognize(self, image: bytes, page: int) -> str:
        body = {
            "model": "mistral-ocr-latest",
            "document": {
                "type": "image_url",
                "image_url": f"data:image/png;base64,{base64.b64encode(image).decode('ascii')}",
            },
        }
        try:
            response = self.client.post("/ocr", json=body)
            response.raise_for_status()
            pages = response.json().get("pages") or []
            return (pages[0].get("markdown") or "").strip() if pages else ""
        except httpx.HTTPError as exc:
            raise OcrChainError([f"mistral: {type(exc).__name__}"]) from exc


class TesseractOcrEngine:
    """Local ``tesseract`` CLI, Thai + English, LSTM engine only."""

    name = "tesseract"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _binary(self) -> str:
        binary = shutil.which("tesseract")
        if not binary:
            raise FileNotFoundError("tesseract binary not found on PATH")
        return binary

    def recognize(self, image: bytes, page: int) -> str:
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                handle.write(image)
                path = Path(handle.name)
            try:
                result = subprocess.run(  # noqa: S603 - fixed binary, fixed args
                    [self._binary(), str(path), "stdout", "-l", "tha+eng", "--oem", "1"],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=self.settings.tesseract_timeout_seconds,
                )
            finally:
                path.unlink(missing_ok=True)
        except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError) as exc:
            # TimeoutExpired: page budget blown; OSError: spawn/disk/temp-file
            # failures (FileNotFoundError is a subclass); UnicodeDecodeError:
            # non-UTF8 bytes on stdout despite errors="replace" guards.
            raise OcrChainError([f"tesseract: {type(exc).__name__}"]) from exc
        if result.returncode != 0:
            raise OcrChainError([f"tesseract: exit {result.returncode}"])
        return result.stdout


def _parse_engine_order(raw: str | None) -> tuple[str, ...]:
    names = tuple(name.strip() for name in (raw or "").split(",") if name.strip())
    unknown = [name for name in names if name not in DEFAULT_CHAIN_ENGINES]
    if unknown:
        raise ValueError(f"unknown OCR engine(s) in OCR_CHAIN_ENGINES: {', '.join(unknown)}")
    return names or DEFAULT_CHAIN_ENGINES


# Sentinel distinguishing "not configured" (cached negative) from a cached engine.
_NOT_CONFIGURED = object()


@dataclass
class OcrChain:
    """Engines in configured order; first non-empty text wins per page.

    Engines (and their httpx clients) are built lazily once and cached for
    the chain's lifetime — one recognize() call serves one page, and a
    hundred-page document must not construct a fresh connection pool per
    page. Callers should use ``close()``/the context manager when done,
    typically one chain per document conversion.
    """

    settings: Settings
    on_progress: ProgressCallback | None = None
    engine_order: tuple[str, ...] = field(default_factory=tuple)
    clients: dict[str, httpx.Client] = field(default_factory=dict)
    _engines: dict[str, object] = field(default_factory=dict, repr=False)
    _clients_owned: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if not self.engine_order:
            self.engine_order = _parse_engine_order(self.settings.ocr_chain_engines)

    def _build_engine(self, name: str):
        cached = self._engines.get(name)
        if cached is _NOT_CONFIGURED:
            return None
        if cached is not None:
            return cached
        engine = None
        client = None
        if name == "softnix":
            if self.settings.softnix_ocr_base_url and self.settings.softnix_ocr_token:
                client = self.clients.get("softnix") or httpx.Client(
                    base_url=self.settings.softnix_ocr_base_url.rstrip("/"),
                    headers={"Authorization": f"Bearer {self.settings.softnix_ocr_token}"},
                    verify=not self.settings.softnix_ocr_insecure_tls,
                    timeout=httpx.Timeout(self.settings.softnix_ocr_request_timeout_seconds),
                )
                engine = SoftnixOcrEngine(self.settings, client, self.on_progress)
        elif name == "mistral":
            if self.settings.mistral_api_key:
                client = self.clients.get("mistral") or httpx.Client(
                    base_url="https://api.mistral.ai/v1",
                    headers={"Authorization": f"Bearer {self.settings.mistral_api_key}"},
                    timeout=httpx.Timeout(self.settings.mistral_ocr_timeout_seconds),
                )
                engine = MistralOcrEngine(self.settings, client)
        else:
            engine = TesseractOcrEngine(self.settings)
        # Remember which clients this chain owns so close() releases them;
        # externally supplied clients (tests) stay untouched.
        if engine is not None and client is not None and name not in self.clients:
            self.clients[name] = client
            self._clients_owned.add(name)
        self._engines[name] = engine if engine is not None else _NOT_CONFIGURED
        return engine

    def close(self) -> None:
        """Close httpx clients this chain created (external ones are kept)."""
        for name in list(self._clients_owned):
            client = self.clients.pop(name, None)
            if client is not None:
                client.close()
            self._clients_owned.discard(name)

    def __enter__(self) -> "OcrChain":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def engine_names(self) -> list[str]:
        """Configured engines that are actually usable right now.

        Pure configuration check — never constructs engines or httpx
        clients, so probing readiness cannot leak resources.
        """
        usable = []
        for name in self.engine_order:
            if name == "softnix":
                ok = bool(self.settings.softnix_ocr_base_url and self.settings.softnix_ocr_token)
            elif name == "mistral":
                ok = bool(self.settings.mistral_api_key)
            else:
                ok = True  # tesseract: availability checked at use time
            if ok:
                usable.append(name)
            else:
                logger.info("OCR engine '%s' not configured; skipping", name)
        return usable

    def recognize(self, image: bytes, page: int) -> str:
        attempts: list[str] = []
        for name in self.engine_order:
            engine = self._build_engine(name)
            if engine is None:
                attempts.append(f"{name}: not configured")
                continue
            try:
                text = engine.recognize(image, page)
            except OcrChainError as exc:
                attempts.extend(exc.attempts)
                logger.warning("OCR engine '%s' failed on page %s: %s", name, page, exc.attempts)
                continue
            if text.strip():
                logger.debug("OCR engine '%s' recognized page %s", name, page)
                return text
            attempts.append(f"{name}: empty result")
        raise OcrChainError(attempts)


def build_recognizer(settings: Settings | None = None,
                     on_progress: ProgressCallback | None = None) -> OcrChain:
    """Convenience factory for ``anydoc.to_markdown_with_ocr(data, fmt, ocr)``."""
    return OcrChain(settings or get_settings(), on_progress=on_progress)
