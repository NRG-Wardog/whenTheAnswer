from __future__ import annotations

import argparse
import asyncio
import json
import platform
import traceback
from pathlib import Path
from typing import List, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

from .browser import (
    browser_keepalive if False else None,
)
