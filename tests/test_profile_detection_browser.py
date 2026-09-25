"""Browser regression for LinkedIn's rendered H2 profile heading."""

import asyncio

import pytest
from playwright.async_api import Error, async_playwright

from app.decision import decide_action
from app.models import ActionType
from app.runner import LinkedInRunner


def test_connected_profile_with_h2_and_first_degree_uses_message():
    async def run():
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(channel="chrome", headless=True)
            except Error as exc:
                pytest.skip(f"Google Chrome is unavailable for DOM regression: {exc}")
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 720})
                await page.set_content("""
                  <main style="position:relative;height:900px;width:1240px">
                    <section style="position:absolute;left:80px;top:300px;width:700px">
                      <div style="display:flex;align-items:center;gap:6px">
                        <h2>Yash Thakkar</h2><span>He/Him</span><p>· 1st</p>
                      </div>
                      <a href="/messaging/compose/">Message</a>
                      <button>More</button>
                    </section>
                    <aside style="position:absolute;left:900px;top:300px">
                      <h2>Suggested Person</h2><span>2nd</span>
                      <button>Connect</button>
                    </aside>
                  </main>
                """)
                runner = LinkedInRunner.__new__(LinkedInRunner)
                state = await runner.detect_page_state(page)
                assert state.profile_name == "Yash Thakkar"
                assert state.degree == "1st"
                assert state.has_message_button
                decision = decide_action(
                    ActionType.MESSAGE, state,
                    inmail_enabled=False, message_if_connected=True,
                    has_message=True,
                )
                assert decision.action == ActionType.MESSAGE
            finally:
                await browser.close()

    asyncio.run(run())


def test_sidebar_first_degree_does_not_mark_main_profile_connected():
    async def run():
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(channel="chrome", headless=True)
            except Error as exc:
                pytest.skip(f"Google Chrome is unavailable for DOM regression: {exc}")
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 720})
                await page.set_content("""
                  <main style="position:relative;height:900px;width:1240px">
                    <section style="position:absolute;left:80px;top:300px;width:700px">
                      <h1>Alex Example</h1><span>2nd</span>
                      <button>Connect</button><button>More</button>
                    </section>
                    <aside style="position:absolute;left:900px;top:300px">
                      <h2>Suggested Connection</h2><span>1st</span>
                      <button>Message</button>
                    </aside>
                  </main>
                """)
                runner = LinkedInRunner.__new__(LinkedInRunner)
                state = await runner.detect_page_state(page)
                assert state.profile_name == "Alex Example"
                assert state.degree == "2nd"
                assert state.connect == "direct"
                assert not state.has_message_button
            finally:
                await browser.close()

    asyncio.run(run())
