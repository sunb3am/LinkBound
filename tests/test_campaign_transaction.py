import asyncio

import pytest
from fastapi import HTTPException

from app import campaigns, db
from app.models import CampaignUpdate


def test_missing_campaign_mutations_do_not_leave_sqlite_transaction_open(tmp_path):
    db.close_db()
    db.init_db(tmp_path / "campaigns.sqlite")
    try:
        with pytest.raises(HTTPException) as update_error:
            asyncio.run(campaigns.update_campaign(999, CampaignUpdate(name="missing")))
        assert update_error.value.status_code == 404
        assert not db._conn().in_transaction

        with pytest.raises(HTTPException) as delete_error:
            asyncio.run(campaigns.delete_campaign(999))
        assert delete_error.value.status_code == 404
        assert not db._conn().in_transaction
    finally:
        db.close_db()
