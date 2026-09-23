import asyncio
import importlib
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import db
from app.runner import LinkedInRunner


def test_screenshots_use_persistent_data_path_and_legacy_links_still_resolve(tmp_path, monkeypatch):
    db.close_db()
    monkeypatch.setenv("LINKBOUND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LINKBOUND_REQUIRE_TAILSCALE_AUTH", "false")
    main = importlib.reload(importlib.import_module("app.main"))

    class FakePage:
        async def screenshot(self, *, path):
            Path(path).write_bytes(b"test screenshot")

    async def check():
        runner = LinkedInRunner(main.settings, "me")
        stored = await runner._screenshot(FakePage(), {"row_index": 1, "full_name": "Person"}, "check")
        assert stored == "screenshots/me_0001_Person_check.png"
        assert (tmp_path / stored).read_bytes() == b"test screenshot"
        assert Path((await main.screenshot(stored)).path) == tmp_path / stored
        assert Path((await main.screenshot("data/" + stored)).path) == tmp_path / stored
        with pytest.raises(HTTPException) as forbidden:
            await main.screenshot("screenshots/../../outside.txt")
        assert forbidden.value.status_code == 404

    try:
        asyncio.run(check())
    finally:
        db.close_db()
