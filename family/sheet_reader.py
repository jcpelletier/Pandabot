"""
SheetReader — thin wrapper around Google Sheets API v4.

Reads a published Google Sheet and returns rows as dicts keyed by header row.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("panda-bot.family.sheet")


class SheetReader:
    """Read rows from a Google Sheet by spreadsheet ID and sheet name."""

    def __init__(
        self,
        spreadsheet_id: str,
        sheet_name: str = "Sheet1",
        credentials_path: str | None = None,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self.credentials_path = credentials_path
        self._service: Any | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_all(self) -> list[dict[str, str]]:
        """Return every row as a list of dicts keyed by the header row."""
        rows = self._fetch_values()
        if not rows:
            return []
        headers = [str(h).strip() for h in rows[0]]
        return [dict(zip(headers, [str(v) for v in row])) for row in rows[1:]]

    def query(self, column: str, value: str) -> list[dict[str, str]]:
        """Return rows where *column* matches *value* (case-insensitive)."""
        all_rows = self.read_all()
        if not all_rows:
            return []
        return [
            row for row in all_rows
            if str(row.get(column, "")).lower() == value.lower()
        ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_service(self) -> Any:
        """Lazy-initialize the Google Sheets service."""
        if self._service is not None:
            return self._service
        try:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build

            scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
            if self.credentials_path:
                credentials = Credentials.from_service_account_file(
                    self.credentials_path, scopes=scopes
                )
            else:
                # For public sheets, we may not need auth — but the API still
                # requires a credentials object. Use anonymous-style credentials.
                credentials = Credentials.from_service_account_file(
                    self.credentials_path or "", scopes=scopes
                ) if self.credentials_path else None

            if credentials:
                self._service = build("sheets", "v4", credentials=credentials)
            else:
                # Public sheet fallback — use the published CSV export instead.
                log.info("No credentials; falling back to CSV export")
                self._service = None
        except ImportError:
            log.warning("google-api-python-client not installed; using CSV fallback")
            self._service = None
        except Exception as exc:
            log.warning("Google Sheets API init failed (%s); using CSV fallback", exc)
            self._service = None
        return self._service

    def _fetch_values(self) -> list[list[str]]:
        """Fetch sheet values via API or CSV fallback."""
        service = self._get_service()
        if service is not None:
            try:
                sheets = service.spreadsheets()
                result = sheets.values().get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{self.sheet_name}!A:Z",
                ).execute()
                return result.get("values", [])
            except Exception as exc:
                log.warning("Sheets API get failed (%s); falling back to CSV", exc)

        # CSV fallback — works for publicly shared sheets.
        url = (
            f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/gviz/tq?tqx=out:csv&sheet={self.sheet_name}"
        )
        import requests
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        import csv, io
        reader = csv.reader(io.StringIO(resp.text))
        return [row for row in reader if row]
