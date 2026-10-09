from __future__ import annotations

import io
import zipfile
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from . import db
from .settings import load_settings

router = APIRouter(prefix="/api/export", tags=["export"])

@router.get("/download")
async def export_data():
    """Export the entire SQLite database and config file as a ZIP."""
    settings = load_settings()
    db_path = settings.data_dir / "outbound.db"
    config_path = settings.root / "config.yaml"
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        if db_path.exists():
            zip_file.write(db_path, "outbound.db")
        if config_path.exists():
            zip_file.write(config_path, "config.yaml")
            
    return StreamingResponse(
        iter([zip_buffer.getvalue()]), 
        media_type="application/x-zip-compressed", 
        headers={"Content-Disposition": f"attachment; filename=LinkBound_Export_{db._now()[:10]}.zip"}
    )
