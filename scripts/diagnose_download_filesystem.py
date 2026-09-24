"""Disposable headed Chrome download probe. Never use a signed-in profile."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright


PDF = os.environ.get("LINKBOUND_PROBE_PDF") == "1"
SLOW = os.environ.get("LINKBOUND_PROBE_SLOW") == "1"
if PDF:
    PAYLOAD = b"%PDF-1.4\n" + b"0" * ((2 * 1024 * 1024) if SLOW else 52240)
else:
    PAYLOAD = b"linkbound-probe-download\n"
FILENAME = os.environ.get("LINKBOUND_PROBE_FILENAME", "probe.pdf" if PDF else "probe.txt")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf" if PDF else "text/plain")
        self.send_header("Content-Disposition", f'attachment; filename="{FILENAME}"')
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        if SLOW:
            for start in range(0, len(PAYLOAD), 65536):
                self.wfile.write(PAYLOAD[start:start + 65536])
                self.wfile.flush()
                time.sleep(0.1)
        else:
            self.wfile.write(PAYLOAD)

    def log_message(self, *_args):
        pass


async def main() -> None:
    profile = Path(os.environ["LINKBOUND_PROBE_PROFILE"]).resolve()
    output = Path(os.environ["LINKBOUND_PROBE_OUTPUT"]).resolve()
    if "linkbound-probe" not in profile.name or profile == output:
        raise ValueError("Use a dedicated linkbound-probe profile and separate output directory")
    profile.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    fixed_download_dir = os.environ.get("LINKBOUND_PROBE_DOWNLOAD_DIR")
    if fixed_download_dir:
        Path(fixed_download_dir).mkdir(parents=True, exist_ok=True)
    download_scope = (
        contextlib.nullcontext(fixed_download_dir) if fixed_download_dir
        else tempfile.TemporaryDirectory(prefix="linkbound-probe-downloads-")
    )
    with download_scope as download_dir:
        before = {p.name for p in Path(download_dir).iterdir()}
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            if os.environ.get("LINKBOUND_PROBE_BARE") == "1":
                prefs_path = profile / "Default" / "Preferences"
                prefs_path.parent.mkdir(parents=True, exist_ok=True)
                prefs = json.loads(prefs_path.read_text()) if prefs_path.exists() else {}
                prefs.setdefault("download", {}).update({
                    "default_directory": download_dir,
                    "prompt_for_download": False,
                })
                prefs_path.write_text(json.dumps(prefs))
                process = subprocess.Popen([
                    "/usr/bin/google-chrome", f"--user-data-dir={profile}",
                    "--no-first-run", "--no-default-browser-check",
                    f"http://127.0.0.1:{server.server_port}/{FILENAME}",
                ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    found = None
                    for _ in range(200):
                        for candidate in Path(download_dir).iterdir():
                            if (candidate.name not in before and candidate.is_file()
                                    and candidate.read_bytes() == PAYLOAD):
                                found = candidate
                                break
                        if found:
                            break
                        if process.poll() is not None:
                            break
                        await asyncio.sleep(0.1)
                    if found is None:
                        raise RuntimeError(
                            f"Bare Chrome download failed (exit={process.poll()}; "
                            f"files={[p.name for p in Path(download_dir).iterdir()]})"
                        )
                    print("BARE_SAVED", found, found.stat().st_size, flush=True)
                    return
                finally:
                    process.terminate()
                    try:
                        process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
            async with async_playwright() as pw:
                launch = dict(user_data_dir=str(profile), headless=False,
                              accept_downloads=True,
                              chromium_sandbox=os.environ.get("LINKBOUND_PROBE_SANDBOX") != "0",
                              downloads_path=download_dir)
                if os.environ.get("LINKBOUND_PROBE_BUNDLED") != "1":
                    launch["channel"] = "chrome"
                context = await pw.chromium.launch_persistent_context(**launch)
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.set_content(
                        f'<a href="http://127.0.0.1:{server.server_port}/{FILENAME}">Download</a>'
                    )
                    if os.environ.get("LINKBOUND_PROBE_CDP_CAPTURE") == "1":
                        captured: list[bytes] = []
                        capture_done = asyncio.Event()
                        session = await context.new_cdp_session(page)
                        if os.environ.get("LINKBOUND_PROBE_DENY_DOWNLOADS") == "1":
                            await session.send("Browser.setDownloadBehavior", {"behavior": "deny"})

                        async def capture_cdp(event):
                            try:
                                if not event["request"]["url"].endswith("/" + FILENAME):
                                    await session.send("Fetch.continueResponse", {
                                        "requestId": event["requestId"]
                                    })
                                    return
                                body = await session.send("Fetch.getResponseBody", {
                                    "requestId": event["requestId"]
                                })
                                captured.append(
                                    base64.b64decode(body["body"]) if body["base64Encoded"]
                                    else body["body"].encode()
                                )
                                await session.send("Fetch.fulfillRequest", {
                                    "requestId": event["requestId"],
                                    "responseCode": 204,
                                    "body": "",
                                })
                            finally:
                                capture_done.set()

                        session.on(
                            "Fetch.requestPaused",
                            lambda event: asyncio.create_task(capture_cdp(event)),
                        )
                        await session.send("Fetch.enable", {"patterns": [{
                            "urlPattern": "*" if os.environ.get("LINKBOUND_PROBE_ALL_RESPONSES") == "1"
                            else f"*{FILENAME}", "requestStage": "Response"
                        }]})
                        if os.environ.get("LINKBOUND_PROBE_ALL_RESPONSES") == "1":
                            await page.evaluate(
                                f"fetch('http://127.0.0.1:{server.server_port}/ping').catch(()=>{{}})"
                            )
                        await page.get_by_text("Download").click()
                        await asyncio.wait_for(capture_done.wait(), timeout=20)
                        if captured != [PAYLOAD]:
                            raise RuntimeError("CDP browser download bytes did not match")
                        print("CDP_CAPTURED", len(captured[0]), flush=True)
                        if os.environ.get("LINKBOUND_PROBE_DENY_DOWNLOADS") == "1":
                            await session.send("Browser.setDownloadBehavior", {"behavior": "default"})
                        return
                    if os.environ.get("LINKBOUND_PROBE_ROUTE_CAPTURE") == "1":
                        captured: list[bytes] = []
                        capture_done = asyncio.Event()

                        async def capture(route):
                            response = await route.fetch()
                            captured.append(await response.body())
                            await route.abort()
                            capture_done.set()

                        await page.route(f"**/{FILENAME}", capture)
                        await page.get_by_text("Download").click()
                        await asyncio.wait_for(capture_done.wait(), timeout=20)
                        if captured != [PAYLOAD]:
                            raise RuntimeError("Routed browser download bytes did not match")
                        print("ROUTE_CAPTURED", len(captured[0]), flush=True)
                        return
                    async with page.expect_download(timeout=20000) as event:
                        await page.get_by_text("Download").click()
                    download = await event.value
                    print("DOWNLOAD_EVENT", download.suggested_filename, flush=True)
                    found = None
                    for _ in range(100):
                        for candidate in Path(download_dir).iterdir():
                            if (candidate.name not in before and candidate.is_file()
                                    and candidate.read_bytes() == PAYLOAD):
                                found = candidate
                                break
                        if found:
                            break
                        await asyncio.sleep(0.1)
                    if found is None:
                        raise RuntimeError("No complete download bytes found in Playwright download directory")
                    saved = output / f"probe-{os.getpid()}{Path(FILENAME).suffix}"
                    shutil.copyfile(found, saved)
                    print("SAVED", saved, saved.stat().st_size, flush=True)
                finally:
                    await context.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
