"""Visible LinkedIn inbox controls used only by the no-send collector.

The compose URL renders the folder list without auto-opening its first thread.
This was checked on the hosted headed Chrome profile on 2026-09-23. If that
behavior changes, stop rather than silently consuming an unread marker.
"""

from __future__ import annotations

import asyncio
import base64
from email.message import Message
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import Page

from .linkedin_urls import canonical_profile_url


COMPOSE_URL = "https://www.linkedin.com/messaging/compose/"
ROW = "li.msg-conversation-listitem"
FOLDERS = ("focused", "other", "archived", "spam")


class InboxStateError(RuntimeError):
    pass


class InboxAuthError(InboxStateError):
    pass


@dataclass(frozen=True)
class InboxRow:
    index: int
    participant_name: str
    preview_text: str
    unread: bool
    occluded: bool


def thread_key_from_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.hostname not in {"www.linkedin.com", "linkedin.com"}:
        raise InboxStateError("LinkedIn thread did not open on the expected site")
    path = parsed.path.rstrip("/")
    if not re.fullmatch(r"/messaging/thread/[^/]+", path) or path.endswith("/new"):
        raise InboxStateError("LinkedIn thread has no stable URL key")
    return path


class InboxBrowser:
    def __init__(self, page: Page):
        self.page = page

    async def _check_state(self) -> None:
        path = urlsplit(self.page.url).path.lower()
        if any(marker in path for marker in ("/login", "/checkpoint", "/signup", "/authwall")):
            raise InboxAuthError("LinkedIn login or verification is required")
        warnings = await self.page.locator("h1,h2,[role=alert]").all_text_contents()
        if any(re.search(r"account restricted|verify your identity|security verification|unusual activity", text, re.I)
               for text in warnings):
            raise InboxAuthError("LinkedIn verification or restriction is visible")

    async def verify_identity(self, expected_profile_url: str) -> str:
        """Read the signed-in profile from LinkedIn's visible Me menu."""
        await self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        await self._check_state()
        me = self.page.get_by_role("button", name="Me", exact=True)
        await me.first.wait_for(state="visible", timeout=8000)
        if await me.count() != 1:
            raise InboxAuthError("LinkedIn account menu is unavailable")

        async def visible_account_menus():
            # The account menu can be omitted by role/text selectors even when
            # its DOM node is visible. Inspect the actual rendered menus.
            return [menu for menu in await self.page.locator("[role=menu]").all()
                    if await menu.is_visible() and
                    re.search(r"\bSign out\b", await menu.inner_text(), re.I)]

        menus = await visible_account_menus()
        if not menus:
            await me.click()
            await self.page.get_by_text("Sign out", exact=False).first.wait_for(
                state="visible", timeout=8000
            )
            menus = await visible_account_menus()
        if len(menus) != 1:
            raise InboxAuthError("LinkedIn account menu could not be verified")
        urls = set()
        for href in await menus[0].locator("a[href*='/in/']").evaluate_all(
            "links => links.filter(link => link.getClientRects().length > 0).map(link => link.href)"
        ):
            path = urlsplit(href).path.rstrip("/")
            if re.fullmatch(r"/in/[^/]+", path):
                urls.add(canonical_profile_url(href))
        if len(urls) != 1:
            raise InboxAuthError("LinkedIn account profile is ambiguous")
        actual = urls.pop()
        if actual.casefold() != canonical_profile_url(expected_profile_url).casefold():
            raise InboxAuthError("Signed-in LinkedIn account differs from the selected sender")
        return actual

    async def open_list(self, folder: str = "focused") -> None:
        if folder not in FOLDERS:
            raise ValueError("Unknown LinkedIn inbox folder")
        await self.page.goto(COMPOSE_URL, wait_until="domcontentloaded")
        await self.page.locator("button.msg-cross-pillar-inbox-filters-v3__drop-down-trigger").wait_for()
        await self._check_state()
        if urlsplit(self.page.url).path not in {"/messaging/compose/", "/messaging/thread/new/"}:
            raise InboxStateError("LinkedIn no longer opens a blank compose view")
        if await self.page.locator("li.msg-s-message-list__event").count():
            raise InboxStateError("Compose view unexpectedly opened a conversation")
        if folder != "focused":
            await self.page.locator("button.msg-cross-pillar-inbox-filters-v3__drop-down-trigger").click()
            option = self.page.locator("[role=button].msg-cross-pillar-inbox-filters-v3__filter").filter(
                has_text=re.compile(rf"^\s*{re.escape(folder)}\s*$", re.I)
            )
            if await option.count() != 1:
                raise InboxStateError(f"LinkedIn {folder} folder control is unavailable")
            await option.click()
            await self._check_state()
        selected = (await self.page.locator(
            "button.msg-cross-pillar-inbox-filters-v3__drop-down-trigger"
        ).inner_text()).strip().lower()
        if selected != folder:
            raise InboxStateError(f"LinkedIn showed {selected or 'unknown'} instead of {folder}")
        # Folder rows hydrate asynchronously. A 300 ms snapshot once reported
        # one Other row where the same account later rendered 16.
        await self.page.wait_for_timeout(800)
        previous = None
        stable = 0
        for _ in range(7):
            signature = await self.page.locator(ROW).evaluate_all("""rows => [rows.length,
              rows.filter(e=>!!e.querySelector('.msg-conversation-card--occluded')).length,
              (rows[0]?.innerText||'').length,(rows[rows.length-1]?.innerText||'').length]""")
            stable = stable + 1 if signature == previous else 0
            if stable >= 2:
                return
            previous = signature
            await self.page.wait_for_timeout(450)
        raise InboxStateError(f"LinkedIn {folder} folder did not settle")

    async def rows(self) -> list[InboxRow]:
        await self._check_state()
        raw = await self.page.locator(ROW).evaluate_all("""rows => rows.map((row, index) => ({
            index,
            participant_name:(row.querySelector('.msg-conversation-listitem__participant-names')?.innerText||'').trim(),
            preview_text:(row.querySelector('.msg-conversation-card__message-snippet')?.innerText||'').trim(),
            unread:!!row.querySelector('.msg-conversation-card__convo-item-container--unread'),
            occluded:!!row.querySelector('.msg-conversation-card--occluded')
        }))""")
        return [InboxRow(**item) for item in raw]

    async def validate_row(self, baseline: InboxRow) -> None:
        row = self.page.locator(ROW).nth(baseline.index)
        await row.scroll_into_view_if_needed()
        rows = await self.rows()
        if baseline.index >= len(rows):
            raise InboxStateError("Inbox row disappeared before it could be opened")
        current = rows[baseline.index]
        identity = (baseline.participant_name, baseline.preview_text)
        if sum((item.participant_name, item.preview_text) == identity
               for item in rows if not item.occluded) != 1:
            raise InboxStateError("Inbox row identity is ambiguous")
        if (current.participant_name != baseline.participant_name or
                current.preview_text != baseline.preview_text or
                current.unread != baseline.unread or current.occluded):
            raise InboxStateError("Inbox row changed before it could be opened")

    async def open_row(self, baseline: InboxRow) -> str:
        await self.validate_row(baseline)
        row = self.page.locator(ROW).nth(baseline.index)
        await row.locator(".msg-conversation-listitem__link").click()
        await self._check_state()
        return thread_key_from_url(self.page.url)

    async def messages(self) -> list[dict]:
        await self._check_state()
        return await self.page.locator("li.msg-s-message-list__event").evaluate_all("""rows => rows.map(row => {
            const event = row.querySelector('.msg-s-event-listitem[data-event-urn]');
            if (!event) return null;
            const author = event.querySelector('.msg-s-message-group__name');
            const link = author?.closest('a');
            const body = event.querySelector('.msg-s-event-listitem__body');
            const buttons = [...event.querySelectorAll('button.msg-s-event-listitem__download-attachment-button')];
            return {source_key:event.getAttribute('data-event-urn'),
                author_name:(author?.innerText||'').trim(), author_url:link?.href||'',
                body:(body?.innerText||'').trim(),
                has_attachment:!!event.querySelector('[class*="attachment"]'),
                attachments:buttons.map((button,index)=>({
                    index,filename:(button.querySelector('.ui-attachment__filename')?.innerText||'').trim()
                }))};
        }).filter(Boolean)""")

    async def download_attachment(self, message_key: str, index: int) -> tuple[str, bytes]:
        """Click a visible file button and capture its response inside Chrome.

        Chrome 154 on the hosted VM crashes on native downloads after a
        persistent-profile restart. DevTools response interception avoids the
        download manager while keeping the request in the headed browser.
        """
        if index < 0:
            raise ValueError("Invalid attachment index")
        events = self.page.locator(".msg-s-event-listitem[data-event-urn]")
        matching = [event for event in await events.all()
                    if await event.get_attribute("data-event-urn") == message_key]
        if len(matching) != 1:
            raise InboxStateError("Attachment source message is not unique")
        buttons = matching[0].locator("button.msg-s-event-listitem__download-attachment-button")
        if index >= await buttons.count():
            raise InboxStateError("Attachment button is missing")
        button = buttons.nth(index)
        name_locator = button.locator(".ui-attachment__filename")
        visible_name = (await name_locator.inner_text()).strip() if await name_locator.count() else ""
        session = await self.page.context.new_cdp_session(self.page)
        loop = asyncio.get_running_loop()
        captured = loop.create_future()
        tasks: set[asyncio.Task] = set()

        async def on_paused(event: dict) -> None:
            request_id = event["requestId"]
            try:
                headers = {item["name"].lower(): item["value"]
                           for item in event.get("responseHeaders", [])}
                content_type = headers.get("content-type", "").split(";", 1)[0].lower()
                disposition = headers.get("content-disposition", "")
                is_file = "attachment" in disposition.lower() or content_type in {
                    "application/pdf", "application/octet-stream", "application/zip",
                    "application/msword", "application/vnd.ms-excel",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                }
                if not is_file or captured.done():
                    await session.send("Fetch.continueResponse", {"requestId": request_id})
                    return
                if event.get("responseStatusCode") != 200:
                    raise InboxStateError("Attachment response was not HTTP 200")
                length = headers.get("content-length", "")
                if length.isdecimal() and int(length) > 25 * 1024 * 1024:
                    raise InboxStateError("Attachment exceeds the 25 MiB storage limit")
                response = await session.send("Fetch.getResponseBody", {
                    "requestId": request_id,
                })
                data = (base64.b64decode(response["body"], validate=True)
                        if response["base64Encoded"] else response["body"].encode())
                if not data or len(data) > 25 * 1024 * 1024:
                    raise InboxStateError("Attachment is empty or exceeds the 25 MiB storage limit")
                header = Message()
                header["content-disposition"] = disposition
                filename = visible_name or header.get_filename()
                if not filename:
                    raise InboxStateError("Attachment filename is unavailable")
                await session.send("Fetch.fulfillRequest", {
                    "requestId": request_id, "responseCode": 204, "body": "",
                })
                captured.set_result((filename, data))
            except Exception as exc:
                try:
                    await session.send("Fetch.failRequest", {
                        "requestId": request_id, "errorReason": "Aborted",
                    })
                except Exception:
                    pass
                if not captured.done():
                    captured.set_exception(exc)

        def schedule(event: dict) -> None:
            task = asyncio.create_task(on_paused(event))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        session.on("Fetch.requestPaused", schedule)
        try:
            await session.send("Browser.setDownloadBehavior", {"behavior": "deny"})
            await session.send("Fetch.enable", {"patterns": [{
                "urlPattern": "*", "requestStage": "Response",
            }]})
            await button.click()
            return await asyncio.wait_for(captured, timeout=30)
        except asyncio.TimeoutError as exc:
            raise InboxStateError("No browser attachment response was captured") from exc
        finally:
            try:
                await session.send("Fetch.disable")
                await session.send("Browser.setDownloadBehavior", {"behavior": "default"})
            finally:
                await session.detach()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _active_row(self, thread_key: str | None, baseline: InboxRow):
        if thread_key is not None and thread_key_from_url(self.page.url) != thread_key:
            raise InboxStateError("Active LinkedIn thread changed before unread restoration")
        row = self.page.locator(f"{ROW}:has(.msg-conversations-container__convo-item-link--active)")
        if await row.count() != 1:
            raise InboxStateError("Active LinkedIn inbox row is unavailable")
        participant = (await row.locator(".msg-conversation-listitem__participant-names").inner_text()).strip()
        preview = (await row.locator(".msg-conversation-card__message-snippet").inner_text()).strip()
        if (participant, preview) != (baseline.participant_name, baseline.preview_text):
            raise InboxStateError("Active LinkedIn inbox row changed identity")
        return row

    async def unread_now(self, folder: str, baseline: InboxRow) -> bool:
        """Verify the original row in a blank folder view after any reordering."""
        await self.open_list(folder)
        matches = [row for row in await self.rows()
                   if not row.occluded and
                   (row.participant_name, row.preview_text) ==
                   (baseline.participant_name, baseline.preview_text)]
        if len(matches) != 1:
            raise InboxStateError("Conversation cannot be uniquely verified in its folder")
        return matches[0].unread

    async def restore_unread(self, thread_key: str | None, folder: str, baseline: InboxRow) -> bool:
        if thread_key is not None:
            if thread_key_from_url(self.page.url) != thread_key:
                raise InboxStateError("Active LinkedIn thread changed before unread restoration")
            action = self.page.locator(".msg-title-bar button.msg-thread-actions__control")
            await action.first.wait_for(timeout=8000)
            if await action.count() != 1:
                raise InboxStateError("Target thread toolbar is unavailable")
        else:
            # A changed URL may lack a stable key. Use only a uniquely active
            # row whose visible identity still matches the pre-open snapshot.
            row = await self._active_row(None, baseline)
            action = row.locator("button.msg-thread-actions__control")
        await action.click()
        visible_unread = []
        visible_read = []
        for _ in range(12):
            visible_unread = [item for item in await self.page.get_by_text(
                "Mark as unread", exact=True
            ).all() if await item.is_visible()]
            visible_read = [item for item in await self.page.get_by_text(
                "Mark as read", exact=True
            ).all() if await item.is_visible()]
            if visible_unread or visible_read:
                break
            await self.page.wait_for_timeout(250)
        if len(visible_unread) == 1 and not visible_read:
            await visible_unread[0].click()
        elif len(visible_read) != 1 or visible_unread:
            raise InboxStateError("LinkedIn Mark as unread action is unavailable")
        return await self.unread_now(folder, baseline)
