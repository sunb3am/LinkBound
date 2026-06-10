"""LinkBound LinkedIn Outbound - Python client SDK.

A tiny, dependency-free client for driving the outbound tool from other internal
products (for example, the candidate-account engine that creates customer
accounts and loads candidate profiles). Uses only the standard library.

Example
-------
    from linkbound_client import OutboundClient

    client = OutboundClient("http://127.0.0.1:8000", api_key="cru_local_dev_key_change_me")

    batch = client.enqueue(
        operator="me",
        profiles=[
            {"linkedin_url": "https://linkedin.com/in/jane-doe", "first_name": "Jane", "company": "Acme"},
            {"linkedin_url": "https://linkedin.com/in/john-smith"},  # name resolved automatically
        ],
        action="connect_note",
        message_template="Hi {first_name}, {sender} here. Saw your work at {company}. Open to a quick chat?",
        ai_personalize=True,
        ai_voice="founder",
        dry_run=True,
        webhook_url="https://my-internal-tool.example.com/hooks/outbound",
    )
    print(batch["batch_public_id"])

    # Poll the batch until it finishes (or rely on the webhook).
    detail = client.wait_for_batch(batch["batch_id"])
    print(detail["batch"]["status"], detail["batch"]["sent"])
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


class OutboundError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message


class OutboundClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", api_key: str = "", timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    # ---- low-level ----
    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except json.JSONDecodeError:
                pass
            raise OutboundError(exc.code, detail) from exc

    # ---- API ----
    def health(self) -> dict:
        return self._request("GET", "/api/v1/health")

    def templates(self) -> list[dict]:
        return self._request("GET", "/api/v1/templates").get("templates", [])

    def enqueue(
        self,
        *,
        operator: str,
        profiles: list[dict],
        action: str = "auto",
        message_template: str = "",
        template_id: int | None = None,
        batch_name: str = "",
        dry_run: bool = False,
        send_on_mismatch: bool = False,
        ai_personalize: bool = False,
        ai_voice: str = "auto",
        webhook_url: str = "",
    ) -> dict:
        """Start an outbound batch. Returns the batch summary."""
        payload = {
            "operator": operator,
            "profiles": profiles,
            "action": action,
            "message_template": message_template,
            "template_id": template_id,
            "batch_name": batch_name,
            "dry_run": dry_run,
            "send_on_mismatch": send_on_mismatch,
            "ai_personalize": ai_personalize,
            "ai_voice": ai_voice,
            "webhook_url": webhook_url,
        }
        return self._request("POST", "/api/v1/enqueue", payload)

    def status(self) -> dict:
        return self._request("GET", "/api/v1/status")

    def batch(self, batch_id: int) -> dict:
        return self._request("GET", f"/api/v1/batches/{batch_id}")

    def wait_for_batch(self, batch_id: int, *, poll_seconds: float = 5.0, timeout_seconds: float = 3600) -> dict:
        """Block until the batch reaches a terminal state, then return its detail."""
        deadline = time.time() + timeout_seconds
        terminal = {"finished", "stopped", "error"}
        while time.time() < deadline:
            detail = self.batch(batch_id)
            if (detail.get("batch") or {}).get("status") in terminal:
                return detail
            time.sleep(poll_seconds)
        return self.batch(batch_id)
