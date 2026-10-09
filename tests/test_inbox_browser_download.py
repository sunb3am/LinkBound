import asyncio
import base64
from types import SimpleNamespace

import pytest

from app.inbox_browser import InboxBrowser, InboxStateError


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.commands = []
        self.paused = None

    def on(self, event, callback):
        assert event == "Fetch.requestPaused"
        self.paused = callback

    async def send(self, command, params=None):
        self.commands.append((command, params))
        if command == "Fetch.getResponseBody":
            return {"body": base64.b64encode(b"resume bytes").decode(),
                    "base64Encoded": True}

    async def detach(self):
        self.commands.append(("detach", None))


class FakeLocator:
    def __init__(self, session):
        self.session = session

    async def all(self):
        return [self]

    async def get_attribute(self, name):
        assert name == "data-event-urn"
        return "urn:message:1"

    def locator(self, _selector):
        return self

    async def count(self):
        return 1

    def nth(self, index):
        assert index == 0
        return self

    async def inner_text(self):
        return "resume.pdf"

    async def click(self):
        for response in self.session.responses:
            self.session.paused(response)
        await asyncio.sleep(0)


def browser_with_responses(responses):
    session = FakeSession(responses)
    page = SimpleNamespace(
        locator=lambda _selector: FakeLocator(session),
        context=SimpleNamespace(new_cdp_session=lambda _page: asyncio.sleep(0, result=session)),
    )
    return InboxBrowser(page), session


def response(request_id, content_type, *, length="12"):
    return {"requestId": request_id, "responseStatusCode": 200,
            "responseHeaders": [
                {"name": "Content-Type", "value": content_type},
                {"name": "Content-Length", "value": length},
            ]}


def test_captures_only_attachment_response_and_denies_native_download():
    browser, session = browser_with_responses([
        response("other", "application/json"),
        response("file", "application/pdf"),
    ])
    filename, data = asyncio.run(browser.download_attachment("urn:message:1", 0))
    assert (filename, data) == ("resume.pdf", b"resume bytes")
    assert ("Fetch.continueResponse", {"requestId": "other"}) in session.commands
    assert ("Fetch.fulfillRequest", {"requestId": "file", "responseCode": 204,
                                     "body": ""}) in session.commands
    assert ("Browser.setDownloadBehavior", {"behavior": "deny"}) in session.commands
    assert ("Browser.setDownloadBehavior", {"behavior": "default"}) in session.commands


def test_rejects_oversized_attachment_before_reading_body():
    browser, session = browser_with_responses([
        response("file", "application/pdf", length=str(26 * 1024 * 1024)),
    ])
    with pytest.raises(InboxStateError, match="25 MiB"):
        asyncio.run(browser.download_attachment("urn:message:1", 0))
    assert not any(command == "Fetch.getResponseBody" for command, _ in session.commands)
    assert ("Fetch.failRequest", {"requestId": "file", "errorReason": "Aborted"}) in session.commands
