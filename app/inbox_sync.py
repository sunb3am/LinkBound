"""Bounded, no-send browser pilot for inbound message collection.

The pilot records partial coverage while scrolling, older history, invitation
state, and file downloads are still being validated. It must not be scheduled.
"""

from __future__ import annotations

import mimetypes
import tempfile
from pathlib import Path

from . import db, inbound_store
from .inbox_browser import FOLDERS, InboxAuthError, InboxBrowser, InboxRow, InboxStateError
from .runner import LinkedInRunner


MAX_ROWS_PER_FOLDER = 8


async def _recover_unread(
    settings, operator: str, run_id: int | None, expected_self_url: str,
) -> list[str]:
    """Retry current or interrupted unread openings before any new scan."""
    pending = inbound_store.pending_open_intents(operator, run_id)
    if not pending:
        return []
    errors: list[str] = []
    runner = LinkedInRunner(settings, operator)
    try:
        await runner.start()
        browser = InboxBrowser(runner._require_page())
        await browser.verify_identity(expected_self_url)
        for item in pending:
            baseline = InboxRow(-1, item["participant_name"], item["preview_text"], True, False)
            thread_key = item["thread_key"]
            try:
                if thread_key:
                    await browser.page.goto(
                        "https://www.linkedin.com" + thread_key + "/",
                        wait_until="domcontentloaded",
                    )
                else:
                    await browser.open_list(item["section"])
                    matches = [row for row in await browser.rows() if not row.occluded and
                               (row.participant_name, row.preview_text) ==
                               (baseline.participant_name, baseline.preview_text)]
                    if len(matches) != 1:
                        raise InboxStateError("Unread row cannot be uniquely recovered")
                    thread_key = await browser.open_row(matches[0])
                    inbound_store.bind_open_intent(operator, item["id"], thread_key)
                restored = await browser.restore_unread(
                    thread_key, item["section"], baseline
                )
                status = "restored" if restored else "failed"
                if inbound_store.has_scan_observation(operator, item["run_id"], thread_key):
                    inbound_store.record_unread_restore(
                        operator, item["run_id"], thread_key, status, restored,
                        error="" if restored else "Fresh browser could not verify unread marker",
                    )
                inbound_store.record_open_intent_restore(
                    operator, item["id"], status,
                    error="" if restored else "Fresh browser could not verify unread marker",
                )
                if not restored:
                    errors.append(f"{item['section']}: unread restoration needs manual review")
            except Exception as exc:
                inbound_store.record_open_intent_restore(
                    operator, item["id"], "unknown",
                    error=f"Recovery failed: {type(exc).__name__}",
                )
                if thread_key and inbound_store.has_scan_observation(
                    operator, item["run_id"], thread_key
                ):
                    inbound_store.record_unread_restore(
                        operator, item["run_id"], thread_key, "unknown", None,
                        error=f"Recovery failed: {type(exc).__name__}",
                    )
                errors.append(f"{item['section']}: unread recovery failed ({type(exc).__name__})")
    finally:
        await runner.close()
    return errors


def _contact_url(messages: list[dict], own_url: str | None) -> str | None:
    if own_url is None:
        return None
    candidates = {
        db.normalize_url(item.get("author_url") or "")
        for item in messages
        if db.normalize_url(item.get("author_url") or "") != own_url
    }
    candidates = {url for url in candidates if url.startswith("https://linkedin.com/in/")}
    return next(iter(candidates)) if len(candidates) == 1 else None


def _direction(message: dict, own_url: str | None) -> str:
    if own_url is None:
        return "unknown"
    author_url = db.normalize_url(message.get("author_url") or "")
    if author_url == own_url:
        return "outbound"
    if author_url.startswith("https://linkedin.com/in/"):
        return "inbound"
    return "unknown"


async def scan_account(settings, coordinator, operator: str, *, max_rows_per_folder: int = MAX_ROWS_PER_FOLDER) -> dict:
    """Run a manually triggered pilot with exclusive browser ownership."""
    if operator not in settings.operators:
        raise ValueError("Unknown LinkedIn account")
    if not 1 <= max_rows_per_folder <= 20:
        raise ValueError("Pilot row limit must be between 1 and 20")
    if getattr(getattr(settings, "browser", None), "cdp_url", ""):
        raise ValueError("Inbound scans require an owned persistent Chrome profile")
    account = next((item for item in db.list_operators() if item["key"] == operator), None)
    expected_self_url = account["linkedin_self_url"] if account else None
    if not expected_self_url:
        raise ValueError("Bind this sender's LinkedIn profile URL before inbox scanning")

    async def operation() -> dict:
        run_id = inbound_store.start_sync_run(
            operator, expected_sections=FOLDERS, mode="full"
        )
        runner = LinkedInRunner(settings, operator)
        download_dir = tempfile.TemporaryDirectory(prefix="linkbound-inbox-downloads-")
        errors: list[str] = []
        stopped = False
        try:
            errors.extend(await _recover_unread(settings, operator, None, expected_self_url))
            if errors:
                raise InboxStateError("Previous unread markers need manual review")
            await runner.start(accept_downloads=True, downloads_path=download_dir.name)
            browser = InboxBrowser(runner._require_page())
            browser.download_dir = Path(download_dir.name)
            await browser.verify_identity(expected_self_url)
            for folder in FOLDERS:
                observed = stored = unresolved = 0
                folder_errors: list[str] = []
                try:
                    await browser.open_list(folder)
                    baseline = await browser.rows()
                    observed = len(baseline)
                    for row_snapshot in baseline[:max_rows_per_folder]:
                        if row_snapshot.occluded:
                            unresolved += 1
                            continue
                        thread_key = None
                        intent_id = None
                        attempted_open = False
                        persisted = False
                        fatal: Exception | None = None
                        try:
                            await browser.open_list(folder)
                            await browser.validate_row(row_snapshot)
                            if row_snapshot.unread:
                                intent_id = inbound_store.record_open_intent(
                                    operator, run_id, folder,
                                    row_snapshot.participant_name, row_snapshot.preview_text,
                                )
                            attempted_open = True
                            thread_key = await browser.open_row(row_snapshot)
                            if intent_id is not None:
                                inbound_store.bind_open_intent(operator, intent_id, thread_key)
                            messages = await browser.messages()
                            contact_url = _contact_url(messages, expected_self_url)
                            inbound_store.record_inventory_section(run_id, folder, [{
                                "thread_key": thread_key,
                                "contact_url": contact_url,
                                "participant_name": row_snapshot.participant_name,
                                "preview_text": row_snapshot.preview_text,
                                "linkedin_unread": row_snapshot.unread,
                            }], complete=False)
                            persisted = True
                            stored += 1
                            inbound_store.record_scan_observation(
                                operator, run_id, thread_key, row_snapshot.unread
                            )
                            conversation = next(
                                item for item in inbound_store.list_conversations(operator, 500)
                                if item["thread_key"] == thread_key
                            )
                            for message in messages:
                                if not message.get("source_key"):
                                    raise InboxStateError("Message without a stable browser key")
                                message_id = inbound_store.record_message(
                                    operator, conversation["id"],
                                    source_key=message["source_key"],
                                    direction=_direction(message, expected_self_url),
                                    body=message.get("body") or "",
                                )
                                attachments = message.get("attachments") or []
                                if message.get("has_attachment") and not attachments:
                                    folder_errors.append("An attachment has no supported download button")
                                for attachment in attachments:
                                    filename, data = await browser.download_attachment(
                                        message["source_key"], attachment["index"]
                                    )
                                    name = attachment.get("filename") or filename
                                    inbound_store.save_attachment(
                                        operator, message_id,
                                        source_key=f"index:{attachment['index']}",
                                        filename=name,
                                        mime_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                                        data=data, data_dir=settings.data_dir,
                                    )
                        except InboxAuthError as exc:
                            fatal = exc
                            if not persisted:
                                unresolved += 1
                            folder_errors.append(f"Thread {row_snapshot.index}: verification required")
                        except Exception as exc:
                            fatal = exc
                            if not persisted:
                                unresolved += 1
                            folder_errors.append(f"Thread {row_snapshot.index}: {type(exc).__name__}")
                        finally:
                            if attempted_open:
                                try:
                                    if row_snapshot.unread:
                                        after = await browser.restore_unread(thread_key, folder, row_snapshot)
                                        status = "restored" if after else "failed"
                                    else:
                                        after = await browser.unread_now(folder, row_snapshot)
                                        status = "not_needed" if not after else "unknown"
                                    if thread_key and inbound_store.has_scan_observation(operator, run_id, thread_key):
                                        inbound_store.record_unread_restore(
                                            operator, run_id, thread_key, status, after,
                                            error="" if status in {"restored", "not_needed"}
                                            else "LinkedIn unread marker was not verified",
                                        )
                                    if intent_id is not None:
                                        inbound_store.record_open_intent_restore(
                                            operator, intent_id, status if status != "not_needed" else "unknown",
                                            error="" if status == "restored"
                                            else "LinkedIn unread marker was not verified",
                                        )
                                    if status in {"failed", "unknown"}:
                                        folder_errors.append(f"Thread {row_snapshot.index}: unread state needs review")
                                        fatal = fatal or InboxStateError(
                                            "LinkedIn unread state could not be verified"
                                        )
                                except Exception as exc:
                                    fatal = exc
                                    folder_errors.append(
                                        f"Thread {row_snapshot.index}: unread restoration failed ({type(exc).__name__})"
                                    )
                        if fatal is not None:
                            raise fatal
                    unresolved += max(0, observed - min(observed, max_rows_per_folder))
                except Exception as exc:
                    stopped = True
                    folder_errors.append(f"Folder unavailable: {type(exc).__name__}")
                    errors.append(f"{folder}: {type(exc).__name__}")
                    inbound_store.set_section_coverage(
                        run_id, folder, observed=observed, stored=stored,
                        unresolved=unresolved, complete=False,
                        error="; ".join(folder_errors),
                    )
                    break
                # The pilot does not assert that LinkedIn rendered all older
                # conversations or complete message history in a folder.
                folder_errors.append("Pilot is limited to rendered rows and recent visible messages")
                inbound_store.set_section_coverage(
                    run_id, folder, observed=observed, stored=stored,
                    unresolved=unresolved, complete=False,
                    error="; ".join(folder_errors),
                )
                errors.extend(f"{folder}: {item}" for item in folder_errors)
        except Exception as exc:
            stopped = True
            errors.append(f"Collector stopped: {type(exc).__name__}: {exc}")
        finally:
            try:
                await runner.close()
            except Exception as exc:
                stopped = True
                errors.append(f"Browser close failed: {type(exc).__name__}")
            download_dir.cleanup()
            try:
                recovery_errors = await _recover_unread(
                    settings, operator, run_id, expected_self_url
                )
                stopped = stopped or bool(recovery_errors)
                errors.extend(recovery_errors)
            except Exception as exc:
                stopped = True
                errors.append(f"Unread recovery stopped: {type(exc).__name__}")
            result = inbound_store.finish_sync_run(run_id, error="; ".join(errors)[:1000])
        return {"run_id": run_id, "status": result["status"],
                "coverage": result["coverage"], "stopped": stopped, "error": result["error"]}

    return await coordinator.run_inbound(operator, operation)
