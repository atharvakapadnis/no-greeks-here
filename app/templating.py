"""Single shared Jinja2Templates instance.

Kept separate from main.py so routers can import it without a circular
import (main.py includes the routers, so the routers can't import
templates back from main.py).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
