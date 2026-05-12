import os
import logging
import difflib
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger("panda-bot")

class FamilySheetReader:
    DEFAULT_KEY_PATH = "/home/discord-bot/.config/pandabot/family-sheet-key.json"

    REQUIRED_HEADERS = {
        "First Name": "first_name",
        "Last Name": "last_name",
        "Discord Name": "discord_name",
        "DOB": "dob",
        "Relationship": "relationship",
        "Location": "location",
        "Phone": "phone",
        "Email": "email",
        "Address": "address",
        "Notes": "notes",
    }

    def __init__(self, key_path=None, sheet_id=None):
        self.key_path = key_path or self.DEFAULT_KEY_PATH
        self.sheet_id = sheet_id or os.environ.get("FAMILY_SHEET_ID")

        if not self.sheet_id:
            raise ValueError("FAMILY_SHEET_ID environment variable not set")

        self.credentials = None
        if os.path.exists(self.key_path):
            try:
                self.credentials = service_account.Credentials.from_service_account_file(
                    self.key_path, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
                )
            except Exception as e:
                logger.error(f"Failed to load Google Service Account key from {self.key_path}: {e}")
        else:
            logger.error(f"Google Service Account key file not found at {self.key_path}")

    def get_all_members(self):
        if not self.credentials:
            logger.error("No valid credentials to access Google Sheets")
            return []

        try:
            service = build("sheets", "v4", credentials=self.credentials)
            sheet = service.spreadsheets()
            # Assuming data is in the first sheet, A:Z range to get all columns
            result = sheet.values().get(spreadsheetId=self.sheet_id, range="A:Z").execute()
            values = result.get("values", [])

            if not values:
                logger.warning("No data found in the family sheet")
                return []

            headers = values[0]
            rows = values[1:]

            # Map header names to their column index
            header_to_idx = {header.strip(): i for i, header in enumerate(headers)}

            # Check if all required headers are present
            missing_headers = [h for h in self.REQUIRED_HEADERS if h not in header_to_idx]
            if missing_headers:
                logger.error(f"Missing required headers in sheet: {missing_headers}")
                # We can still proceed if some are missing, but we'll have empty values for them

            members = []
            for row in rows:
                member = {}
                for header_name, key in self.REQUIRED_HEADERS.items():
                    idx = header_to_idx.get(header_name)
                    if idx is not None and idx < len(row):
                        value = row[idx]
                        member[key] = str(value).strip() if value is not None else ""
                    else:
                        member[key] = ""
                members.append(member)

            return members

        except HttpError as e:
            logger.error(f"Google API HTTP error: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error reading family sheet: {e}")
            return []

    def find_member(self, name, members=None):
        if members is None:
            members = self.get_all_members()

        if not members:
            return None

        # Prepare names for fuzzy matching
        # We search across first_name, last_name, and discord_name
        search_map = {}
        for member in members:
            names = [member["first_name"], member["last_name"], member["discord_name"]]
            for n in names:
                if n:
                    if n not in search_map:
                        search_map[n] = []
                    search_map[n].append(member)

        all_names = list(search_map.keys())
        matches = difflib.get_close_matches(name, all_names, n=1, cutoff=0.6)

        if matches:
            best_name = matches[0]
            # Return the first member that matched this name
            return search_map[best_name][0]

        return None

    def search(self, query, members=None):
        if members is None:
            members = self.get_all_members()

        if not members:
            return []

        query = query.lower()
        results = []
        for member in members:
            # Search across all text fields
            found = False
            for value in member.values():
                if query in str(value).lower():
                    found = True
                    break
            if found:
                results.append(member)

        return results
