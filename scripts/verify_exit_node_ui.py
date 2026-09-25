"""Smoke-check the route controls against a local, no-send LinkBound server."""

import json

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(
            "**/api/exit-nodes",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "nodes": [
                        {"id": "laptop-a", "name": "Home laptop", "ip": "100.101.102.103", "online": True},
                        {"id": "phone-b", "name": "Phone", "ip": "100.101.102.104", "online": False},
                    ],
                    "default_node_id": "",
                    "required": True,
                    "error": "",
                }),
            ),
        )
        page.goto("http://127.0.0.1:8765/", wait_until="networkidle")
        page.locator("#onboardingOverlay").evaluate("el => el.style.display = 'none'")
        page.locator("#nav-settings").click()
        assert page.locator("#exitNodeSettings").is_visible()
        assert "browser work is blocked" in page.locator("#defaultEgressStatus").inner_text()
        assert page.locator("#defaultExitNode option").count() == 3
        page.locator("#nav-campaigns").click()
        page.evaluate("goToStep(3)")
        assert page.locator("#runEgressChoice").is_visible()
        assert page.locator("#queueEgressChoice").is_visible()
        assert page.locator("#runExitNode option[value='phone-b']").is_disabled()
        assert page.locator("#btnLaunch").is_disabled()
        page.locator("#runExitNode").select_option("laptop-a")
        assert page.locator("#btnLaunch").is_enabled()
        assert "Home laptop" in page.locator("#runEgressStatus").inner_text()
        assert not errors, errors
        page.screenshot(path="exit-node-ui-smoke.png", full_page=True)
        browser.close()
        print("Exit-node selectors and blocked state rendered; no page errors")


if __name__ == "__main__":
    main()
