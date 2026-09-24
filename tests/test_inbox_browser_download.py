import asyncio
from types import SimpleNamespace

import pytest

from app.inbox_browser import InboxBrowser, InboxStateError


def test_reads_one_completed_file_from_isolated_download_directory(tmp_path):
    browser = InboxBrowser(SimpleNamespace())
    browser.download_dir = tmp_path
    (tmp_path / "older-file").write_bytes(b"older")
    (tmp_path / "new-guid").write_bytes(b"resume bytes")
    data = asyncio.run(browser._completed_download_bytes({"older-file"}))
    assert data == b"resume bytes"


def test_rejects_ambiguous_download_files(tmp_path):
    browser = InboxBrowser(SimpleNamespace())
    browser.download_dir = tmp_path
    (tmp_path / "first-guid").write_bytes(b"one")
    (tmp_path / "second-guid").write_bytes(b"two")
    with pytest.raises(InboxStateError, match="More than one"):
        asyncio.run(browser._completed_download_bytes(set()))
