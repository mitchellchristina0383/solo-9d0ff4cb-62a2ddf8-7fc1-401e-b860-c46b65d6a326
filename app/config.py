"""Application configuration."""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("WATER_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "water.db"

# Reason codes used on REVIEW rows
REASON_UNPAIRED = "unpaired_reading"
REASON_REVERSED = "reversed_reading"
REASON_CROSS_SWITCH = "crosses_switchover"
REASON_OUTSIDE_WINDOW = "outside_meter_service_window"
REASON_NO_REPLACEMENT = "no_replacement_record"
