import asyncio
import gspread
from google.oauth2.service_account import Credentials

SERVICE_ACCOUNT_FILE = "/home/discord-bot/.config/pandabot/family-sheet-key.json"
SPREADSHEET_ID = "1L-7kDO1aVkjM0a8Yn501Ks4wR9QLYFXpofAOeSOlmMc"
SHEET_NAME = "My Family Information"

async def read_family_sheet() -> list[dict]:
    """
    Reads the "My Family Information" sheet from the Google Spreadsheet.
    Authenticates using a service account key file.
    Returns a list of dictionaries keyed by column header.
    """
    return await asyncio.to_thread(_read_family_sheet_sync)

def _read_family_sheet_sync() -> list[dict]:
    """
    Synchronous implementation of Google Sheet reading using gspread.
    """
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    ]

    credentials = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=scopes
    )
    client = gspread.authorize(credentials)

    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.worksheet(SHEET_NAME)

    # get_all_records() returns a list of dictionaries keyed by header row.
    return worksheet.get_all_records()
