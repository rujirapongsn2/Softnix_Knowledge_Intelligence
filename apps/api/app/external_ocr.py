from collections.abc import Callable
from pathlib import Path
import time

import httpx

from .config import Settings, get_settings


class ExternalOcrClient:
    """Client for the configured Softnix OCR v3 service.

    The service receives only already-uploaded PDF files. Structured extraction is
    explicitly disabled: its Markdown result continues through this application's
    chunking, retrieval, and legal-extraction pipeline.
    """

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(
            base_url=self.settings.ext_ocr_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {self.settings.ext_ocr_key}"},
            verify=self.settings.ext_ocr_verify_ssl,
            timeout=httpx.Timeout(self.settings.ext_ocr_request_timeout_seconds),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.settings.ext_ocr_key)

    def check(self) -> bool:
        if not self.enabled:
            return False
        try:
            response = self.client.get("/v3/queue-info")
            response.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            raise RuntimeError("EXTERNAL_OCR_UNAVAILABLE") from exc

    def extract_markdown(self, path: Path, on_progress: Callable[[str, int], None] | None = None) -> str:
        if not self.enabled:
            raise RuntimeError("EXTERNAL_OCR_NOT_CONFIGURED")
        try:
            with path.open("rb") as source:
                response = self.client.post(
                    "/v3/ai-process-file",
                    files={"file": (path.name, source, "application/pdf")},
                    data={
                        "ocr_engine": self.settings.ext_ocr_engine,
                        "image_size": str(self.settings.ext_ocr_image_size),
                        "disable_structure": "true",
                        "use_thinking": "false",
                    },
                )
            response.raise_for_status()
            job_id = response.json().get("job_id")
            if not job_id:
                raise RuntimeError("EXTERNAL_OCR_INVALID_RESPONSE")
            if on_progress:
                on_progress("external_ocr_queued", 15)
            return self._wait_for_result(str(job_id), on_progress)
        except RuntimeError:
            raise
        except httpx.HTTPStatusError as exc:
            code = "EXTERNAL_OCR_UNAVAILABLE" if exc.response.status_code >= 500 else "EXTERNAL_OCR_REJECTED"
            raise RuntimeError(code) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("EXTERNAL_OCR_UNAVAILABLE") from exc

    def _wait_for_result(self, job_id: str, on_progress: Callable[[str, int], None] | None) -> str:
        deadline = time.monotonic() + self.settings.ext_ocr_processing_timeout_seconds
        while time.monotonic() < deadline:
            try:
                status_response = self.client.get(f"/v3/ai-process-file/{job_id}/status")
                status_response.raise_for_status()
                status = status_response.json()
            except httpx.HTTPStatusError as exc:
                code = "EXTERNAL_OCR_UNAVAILABLE" if exc.response.status_code >= 500 else "EXTERNAL_OCR_REJECTED"
                raise RuntimeError(code) from exc
            except httpx.HTTPError as exc:
                raise RuntimeError("EXTERNAL_OCR_UNAVAILABLE") from exc
            state = status.get("status")
            progress = status.get("progress") or {}
            if on_progress and state in {"queueing", "processing"}:
                percent = int(progress.get("percent") or 0)
                on_progress(f"external_ocr_{progress.get('stage') or state}", min(55, 15 + round(percent * 0.4)))
            if state in {"completed", "partial_success"}:
                return self._result(job_id)
            if state in {"failed", "cancelled"}:
                raise RuntimeError("EXTERNAL_OCR_REJECTED")
            time.sleep(self.settings.ext_ocr_poll_interval_seconds)
        raise RuntimeError("EXTERNAL_OCR_TIMEOUT")

    def _result(self, job_id: str) -> str:
        try:
            response = self.client.get(f"/v3/ai-process-file/{job_id}/result")
            response.raise_for_status()
            text = ((response.json().get("results") or {}).get("combined_markdown") or "").strip()
        except httpx.HTTPStatusError as exc:
            code = "EXTERNAL_OCR_UNAVAILABLE" if exc.response.status_code >= 500 else "EXTERNAL_OCR_REJECTED"
            raise RuntimeError(code) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("EXTERNAL_OCR_UNAVAILABLE") from exc
        if not text:
            raise RuntimeError("EXTERNAL_OCR_EMPTY_RESULT")
        return text
