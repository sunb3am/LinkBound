import asyncio

import pytest

from app.inbox_browser import InboxAuthError, InboxBrowser


class Locator:
    def __init__(self, urls=None, *, count=1, on_click=None):
        self.urls = urls or []
        self._count = count
        self.on_click = on_click

    async def all_text_contents(self):
        return []

    async def count(self):
        return self._count() if callable(self._count) else self._count

    async def click(self):
        if self.on_click:
            self.on_click()
        return None

    @property
    def first(self):
        return self

    async def wait_for(self, *, state, timeout):
        assert state == "visible"
        assert timeout == 8000
        assert await self.is_visible()

    async def all(self):
        return [self]

    async def is_visible(self):
        return bool(await self.count())

    async def inner_text(self):
        return "Sign out"

    def filter(self, **kwargs):
        return self

    def locator(self, selector):
        assert selector == "a[href*='/in/']"
        return self

    async def evaluate_all(self, script):
        return self.urls


class Page:
    def __init__(self, urls, *, menu_open=False):
        self.url = "about:blank"
        self.menu_open = menu_open
        self.menu = Locator(urls, count=lambda: int(self.menu_open))
        self.me = Locator(on_click=self._toggle_menu)

    def _toggle_menu(self):
        self.menu_open = not self.menu_open

    async def goto(self, url, *, wait_until):
        assert wait_until == "domcontentloaded"
        self.url = url

    def locator(self, selector):
        if selector == "[role=menu]":
            return self.menu
        assert selector == "h1,h2,[role=alert]"
        return Locator()

    def get_by_role(self, role, **kwargs):
        return Locator(count=0) if role == "menu" else self.me

    def get_by_text(self, text, **kwargs):
        assert text == "Sign out"
        return self.menu


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


def test_account_menu_already_open_is_not_toggled_closed():
    page = Page(["https://www.linkedin.com/in/shubham67/"], menu_open=True)
    assert asyncio.run(InboxBrowser(page).verify_identity(
        "https://www.linkedin.com/in/shubham67/"
    )) == "https://www.linkedin.com/in/shubham67"
    assert page.menu_open
