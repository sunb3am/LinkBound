"""Disposable headed Chrome download probe. Never use a signed-in profile."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright


PDF = os.environ.get("LINKBOUND_PROBE_PDF") == "1"
PAYLOAD = (b"%PDF-1.4\n" + b"0" * 52240) if PDF else b"linkbound-probe-download\n"
FILENAME = "probe.pdf" if PDF else "probe.txt"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf" if PDF else "text/plain")
        self.send_header("Content-Disposition", f'attachment; filename="{FILENAME}"')
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
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
    with tempfile.TemporaryDirectory(prefix="linkbound-probe-downloads-") as download_dir:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            async with async_playwright() as pw:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile), channel="chrome", headless=False,
                    accept_downloads=True, chromium_sandbox=True,
                    downloads_path=download_dir,
                )
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.set_content(
                        f'<a href="http://127.0.0.1:{server.server_port}/{FILENAME}">Download</a>'
                    )
                    async with page.expect_download(timeout=20000) as event:
                        await page.get_by_text("Download").click()
                    download = await event.value
                    print("DOWNLOAD_EVENT", download.suggested_filename, flush=True)
                    found = None
                    for _ in range(100):
                        for candidate in Path(download_dir).iterdir():
                            if candidate.is_file() and candidate.read_bytes() == PAYLOAD:
                                found = candidate
                                break
                        if found:
                            break
                        await asyncio.sleep(0.1)
                    if found is None:
                        raise RuntimeError("No complete download bytes found in Playwright download directory")
                    saved = output / f"probe-{os.getpid()}.txt"
                    shutil.copyfile(found, saved)
                    print("SAVED", saved, saved.stat().st_size, flush=True)
                finally:
                    await context.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
