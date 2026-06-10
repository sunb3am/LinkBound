"""MCP server exposing LinkBound as tools for AI workflows.

Lets an AI agent (Claude, Gemini, Cursor, etc.) drive outbound: list templates,
enqueue a batch of profiles, check status, and read a batch's audit trail. It is
a thin wrapper over the HTTP API via the OutboundClient, so the running dashboard
server is the single source of truth.

Run it:
    pip install "mcp[cli]"
    set OUTBOUND_BASE_URL=http://127.0.0.1:8000      (Windows)  / export ... (mac/linux)
    set OUTBOUND_API_KEY=cru_local_dev_key_change_me
    python client/mcp_server.py

Then register it in your MCP client config as a stdio server pointing at this file.
"""

from __future__ import annotations

import os

from linkbound_client import OutboundClient

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # noqa: BLE001
    raise SystemExit(
        "The 'mcp' package is required. Install it with:  pip install \"mcp[cli]\""
    ) from exc

BASE_URL = os.environ.get("OUTBOUND_BASE_URL", "http://127.0.0.1:8000")
API_KEY = os.environ.get("OUTBOUND_API_KEY", "")

client = OutboundClient(BASE_URL, api_key=API_KEY)
mcp = FastMCP("linkbound")


@mcp.tool()
def list_templates() -> list[dict]:
    """List the saved message templates (name, body, intended action)."""
    return client.templates()


@mcp.tool()
def get_status() -> dict:
    """Get the current run state and live tallies of the outbound engine."""
    return client.status()


@mcp.tool()
def enqueue_outbound(
    operator: str,
    profiles: list[dict],
    action: str = "auto",
    message_template: str = "",
    template_id: int | None = None,
    batch_name: str = "",
    dry_run: bool = True,
    ai_personalize: bool = False,
    ai_voice: str = "auto",
    webhook_url: str = "",
) -> dict:
    """Start a LinkedIn outbound batch.

    Args:
        operator: operator key configured in the tool (e.g. "me").
        profiles: list of {linkedin_url, first_name?, last_name?, company?, role?, email?}.
        action: one of auto | connect | connect_note | message | inmail.
        message_template: inline template body with {first_name}/{company}/{role}/{sender} placeholders.
        template_id: alternatively, the id of a saved template.
        dry_run: when true (default), detect + decide + log but send nothing. Set false to actually send.
        ai_personalize: tailor each message using the live profile context.
        ai_voice: professional | founder | casual | auto.
        webhook_url: optional URL POSTed with the summary when the batch finishes.

    Returns the batch summary including batch_id and batch_public_id.
    """
    return client.enqueue(
        operator=operator, profiles=profiles, action=action,
        message_template=message_template, template_id=template_id,
        batch_name=batch_name, dry_run=dry_run, ai_personalize=ai_personalize,
        ai_voice=ai_voice, webhook_url=webhook_url,
    )


@mcp.tool()
def get_batch(batch_id: int) -> dict:
    """Get a batch and its per-profile audit trail (status, action, decision trace)."""
    return client.batch(batch_id)


if __name__ == "__main__":
    mcp.run()
