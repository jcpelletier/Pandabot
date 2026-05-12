from .sheet_reader import FamilySheetReader
from .cache import FamilyCache

family_cache = None
try:
    family_cache = FamilyCache(FamilySheetReader())
except ValueError:
    # This happens if FAMILY_SHEET_ID is missing (e.g. during tests or bot startup without env)
    pass

__all__ = ["FamilySheetReader", "FamilyCache", "family_cache"]
