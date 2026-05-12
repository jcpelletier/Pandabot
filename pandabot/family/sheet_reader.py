import os
import logging
import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger("panda-bot")

class FamilySheetError(Exception):
    """Custom exception for Family Sheet related errors."""
    pass

class FamilySheetReader:
    def __init__(self):
        self.sheet_id = os.environ.get("FAMILY_SHEET_ID")
        self.sheet_name = os.environ.get("FAMILY_SHEET_NAME", "Family")
        self.key_file = os.path.expanduser("~/.config/pandabot/family-sheet-key.json")
        self._client = None
        self._sheet = None

        if not os.path.exists(self.key_file):
            raise FileNotFoundError(f"Google Sheets service account key file missing at {self.key_file}")

    def _get_client(self):
        if self._client:
            return self._client

        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
            credentials = Credentials.from_service_account_file(self.key_file, scopes=scopes)
            self._client = gspread.authorize(credentials)
            return self._client
        except Exception as e:
            raise FamilySheetError(f"Auth failed — check key file at {self.key_file}: {e}")

    def _get_sheet(self):
        if self._sheet:
            return self._sheet

        client = self._get_client()
        if not self.sheet_id:
            raise FamilySheetError("FAMILY_SHEET_ID environment variable is not set")

        try:
            spreadsheet = client.open_by_key(self.sheet_id)
            self._sheet = spreadsheet.worksheet(self.sheet_name)
            return self._sheet
        except gspread.exceptions.SpreadsheetNotFound:
            raise FamilySheetError(f"Spreadsheet with ID {self.sheet_id} not found")
        except gspread.exceptions.WorksheetNotFound:
            raise FamilySheetError(f"Worksheet '{self.sheet_name}' not found in spreadsheet {self.sheet_id}")
        except Exception as e:
            raise FamilySheetError(f"Failed to reach Google Sheets: {e}")

    def get_all_rows(self) -> list[dict]:
        """Fetches all rows from the worksheet. Assumes row 1 is a header row."""
        try:
            sheet = self._get_sheet()
            # get_all_records returns empty strings for empty cells by default.
            data = sheet.get_all_records()
            # Normalize empty cells to None as requested
            for row in data:
                for key, value in row.items():
                    if value == "":
                        row[key] = None
            return data
        except Exception as e:
            if isinstance(e, FamilySheetError):
                raise
            raise FamilySheetError(f"Failed to fetch all rows: {e}")

    def get_range(self, range_str: str) -> list[list]:
        """Fetches a specific A1-notation range. Returns raw 2D list."""
        try:
            sheet = self._get_sheet()
            return sheet.get(range_str)
        except Exception as e:
            if isinstance(e, FamilySheetError):
                raise
            raise FamilySheetError(f"Failed to fetch range {range_str}: {e}")

    def lookup(self, column: str, value: str) -> dict | None:
        """Finds the first row where column matches value (case-insensitive)."""
        try:
            rows = self.get_all_rows()
            search_value = value.lower()
            for row in rows:
                col_value = row.get(column)
                if col_value and str(col_value).lower() == search_value:
                    return row
            return None
        except Exception as e:
            if isinstance(e, FamilySheetError):
                raise
            raise FamilySheetError(f"Failed to lookup {column}={value}: {e}")
