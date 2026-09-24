import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.linkedin_guard import LinkedInAccountStop, classify_page, inspect_page
from app.models import ActionType, ItemStatus
from app.orchestrator import Orchestrator
from app.runner import LinkedInRunner


@pytest.mark.parametrize("url,text,kind", [
    ("https://www.linkedin.com/checkpoint/challenge/", "", "challenge"),
    ("https://www.linkedin.com/uas/login", "", "login"),
    ("https://www.linkedin.com/in/someone/", "Your account has been restricted", "restriction"),
    ("https://www.linkedin.com/in/someone/", "You've reached the weekly invitation limit", "limit"),
    ("https://www.linkedin.com/feed/", "Verify your identity", "challenge"),
    ("chrome-error://chromewebdata/", "", "browser"),
])
def test_classifies_account_stops(url, text, kind):
    assert classify_page(url, text).kind == kind


def test_normal_profile_does_not_trigger_a_stop():
    assert classify_page("https://www.linkedin.com/in/someone/", "Profile views and invitations") is None


def test_dom_read_failure_stops_before_an_action():
    class Page:
        url = "https://www.linkedin.com/in/someone/"

        async def evaluate(self, _script):
            raise RuntimeError("browser closed")

    assert asyncio.run(inspect_page(Page())).kind == "browser"


def test_runner_stops_on_checkpoint_before_profile_actions():
    class Page:
        url = "https://www.linkedin.com/feed/"

        async def goto(self, _url, **_kwargs):
            self.url = "https://www.linkedin.com/checkpoint/challenge/"
            return None

        async def wait_for_timeout(self, _milliseconds):
            pass

    runner = LinkedInRunner.__new__(LinkedInRunner)
    runner._page = Page()
    result = asyncio.run(runner.process(
        {"linkedin_url": "https://www.linkedin.com/in/someone/"},
        action=ActionType.CONNECT,
    ))
    assert result.status == ItemStatus.FAILED_OTHER
    assert "challenge" in result.detail.casefold()


def test_name_resolution_stops_on_profile_view_limit():
    class Page:
        url = "https://www.linkedin.com/in/someone/"

        async def goto(self, _url, **_kwargs):
            return None

        async def wait_for_timeout(self, _milliseconds):
            pass

        async def evaluate(self, _script):
            return "You've reached your profile viewing limit"

    runner = LinkedInRunner.__new__(LinkedInRunner)
    runner._page = Page()
    with pytest.raises(LinkedInAccountStop, match="activity limit"):
        asyncio.run(runner.resolve_profile("https://www.linkedin.com/in/someone/"))


def test_profile_http_429_stops_before_action():
    class Page:
        url = "https://www.linkedin.com/in/someone/"

        async def goto(self, _url, **_kwargs):
            return SimpleNamespace(status=429)

        async def wait_for_timeout(self, _milliseconds):
            pass

        async def evaluate(self, _script):
            return ""

    runner = LinkedInRunner.__new__(LinkedInRunner)
    runner._page = Page()
    result = asyncio.run(runner.process(
        {"linkedin_url": "https://www.linkedin.com/in/someone/"},
        action=ActionType.CONNECT,
    ))
    assert result.status == ItemStatus.FAILED_LIMIT
    assert "429" in result.detail


def test_name_resolution_closes_browser_when_feed_has_challenge(monkeypatch):
    runner = SimpleNamespace(
        start=AsyncMock(),
        open_feed=AsyncMock(side_effect=LinkedInAccountStop("checkpoint")),
        close=AsyncMock(),
    )
    monkeypatch.setattr("app.orchestrator.LinkedInRunner", lambda *_args: runner)
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.is_busy = lambda: False
    orchestrator.resolving = False
    orchestrator.settings = SimpleNamespace()
    orchestrator._broadcast = lambda _event: None
    with pytest.raises(LinkedInAccountStop, match="checkpoint"):
        asyncio.run(orchestrator.resolve_names(
            [{"linkedin_url": "https://www.linkedin.com/in/someone/"}],
            "me", mode="page", gemini=None,
        ))
    runner.close.assert_awaited_once()
    assert orchestrator.resolving is False


def test_unconfirmed_message_is_not_submitted_again():
    page = SimpleNamespace(
        url="https://www.linkedin.com/in/someone/",
        wait_for_timeout=AsyncMock(),
        evaluate=AsyncMock(return_value=""),
    )
    composer = SimpleNamespace(press=AsyncMock())
    runner = LinkedInRunner.__new__(LinkedInRunner)
    runner._close_all_message_overlays = AsyncMock()
    runner._click_message = AsyncMock(return_value=True)
    runner._target_composer = AsyncMock(return_value=(object(), composer, True, 1))
    runner._thread_contains = AsyncMock(return_value=False)
    runner._type_into = AsyncMock(return_value=True)
    runner._click_send_button = AsyncMock(return_value=True)
    runner._message_confirmed = AsyncMock(return_value=False)
    runner._screenshot = AsyncMock(return_value="")
    result = asyncio.run(runner._send_direct_message(
        page, "Hello", {"full_name": "Someone"}, [],
    ))
    assert result.status == ItemStatus.FAILED_OTHER
    runner._click_send_button.assert_awaited_once()
    composer.press.assert_not_awaited()
