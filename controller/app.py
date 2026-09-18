"""ASGI entry point used by the Rockcraft FastAPI extension."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from demo_controller.main import build_app

app = build_app()
