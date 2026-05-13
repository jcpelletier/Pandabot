"""
family — Google Sheets-backed family relationship lookup for PandaBot.

Provides query_family_info() which reads a public/shared Google Sheet
containing family member data and returns structured relationship info.
"""

from .sheet_reader import SheetReader
from .cache import Cache

__all__ = ["SheetReader", "Cache"]
