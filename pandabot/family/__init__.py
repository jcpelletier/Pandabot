import logging
from .sheet_reader import FamilySheetReader, FamilySheetError
from .cache import FamilyDataCache
from .query_parser import parse_query

logger = logging.getLogger("panda-bot")

# Global singleton instance
_family_cache = None

def init_family_module():
    """Initializes the family module components."""
    global _family_cache
    try:
        reader = FamilySheetReader()
        _family_cache = FamilyDataCache(reader)
        # The constructor of FamilySheetReader already validates the key file's existence.
        # We don't want to crash bot startup if sheet is unavailable,
        # but we do want to log success.
        logger.info("Family module initialized")
    except Exception as e:
        logger.warning(f"Family module initialization failed (tool will be disabled): {e}")
        _family_cache = None

def query_family_info(ctx=None, *, question: str) -> str:
    """
    Pandabot tool to query family information.
    question: Natural language question (e.g., "What is Mom's phone number?")
    """
    if _family_cache is None:
        return "Family info is not available right now. (Module initialization failed)"

    parsed = parse_query(question)
    if not parsed:
        return ("I couldn't understand that question. Try: 'What is Mom's phone number?' "
                "or 'When is Alice's birthday?'")

    name = parsed["name"]
    column = parsed["column"]

    try:
        data = _family_cache.get_data()
        # Find matching row by name (case-insensitive)
        match = next((row for row in data if row.get("Name") and row["Name"].lower() == name.lower()), None)

        if not match:
            return f"I couldn't find anyone named '{name}' in the family sheet."

        value = match.get(column)
        if not value:
            return f"I found {name} but I don't have their {column.lower()} on file."

        return f"**{match['Name']}**'s {column.lower()}: {value}"

    except FamilySheetError as e:
        logger.error(f"Family sheet query error: {e}")
        return f"Family info is not available: {e}"
    except Exception as e:
        logger.exception("Unexpected error in query_family_info")
        return f"An unexpected error occurred while fetching family info."
