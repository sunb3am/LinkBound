import asyncio

import pytest

from app.inbox_browser import InboxAuthError, InboxBrowser


class Locator:
    def __init__(self, urls=None):
        self.urls = urls or []

    async def all_text_contents(self):
        return []

    async def count(self):
        return 1

    async def click(self):
        return None

    def filter(self, **kwargs):
        return self

    def locator(self, selector):
        assert selector == "a[href*='/in/']"
        return self

    async def evaluate_all(self, script):
        return self.urls


class Page:
    def __init__(self, urls):
        self.url = "about:blank"
        self.menu = Locator(urls)

    async def goto(self, url, *, wait_until):
        assert wait_until == "domcontentloaded"
        self.url = url

    def locator(self, selector):
        assert selector == "h1,h2,[role=alert]"
        return Locator()

    def get_by_role(self, role, **kwargs):
        return self.menu if role == "menu" else Locator()


def test_signed_in_profile_must_match_the_selected_sender():
    own = "https://www.linkedin.com/in/shubham67/"
    browser = InboxBrowser(Page([own, own + "recent-activity/"]))
    assert asyncio.run(browser.verify_identity("https://linkedin.com/in/shubham67")) == (
        "https://www.linkedin.com/in/shubham67"
    )

    with pytest.raises(InboxAuthError, match="differs"):
        asyncio.run(browser.verify_identity("https://linkedin.com/in/someone-else"))


def test_ambiguous_me_menu_stops_collection():
    browser = InboxBrowser(Page([
        "https://www.linkedin.com/in/shubham67/",
        "https://www.linkedin.com/in/someone-else/",
    ]))
    with pytest.raises(InboxAuthError, match="ambiguous"):
        asyncio.run(browser.verify_identity("https://linkedin.com/in/shubham67"))
