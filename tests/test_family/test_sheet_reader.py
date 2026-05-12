import pytest
from unittest.mock import MagicMock, patch
from pandabot.family.sheet_reader import FamilySheetReader, FamilySheetError

@pytest.fixture
def mock_gspread():
    with patch("gspread.authorize") as mock_auth:
        yield mock_auth

@pytest.fixture
def reader():
    with patch.dict("os.environ", {"FAMILY_SHEET_ID": "fake_id", "FAMILY_SHEET_NAME": "Family"}):
        with patch("os.path.exists", return_value=True):
            with patch("google.oauth2.service_account.Credentials.from_service_account_file"):
                yield FamilySheetReader()

def test_get_all_rows_returns_list_of_dicts(reader, mock_gspread):
    mock_client = mock_gspread.return_value
    mock_sheet = mock_client.open_by_key.return_value.worksheet.return_value
    mock_sheet.get_all_records.return_value = [
        {"Name": "Alice", "Phone": "123-4567", "Email": ""},
        {"Name": "Bob", "Phone": "987-6543", "Email": "bob@example.com"}
    ]

    rows = reader.get_all_rows()

    assert len(rows) == 2
    assert rows[0]["Name"] == "Alice"
    assert rows[0]["Email"] is None  # Normalized empty string to None
    assert rows[1]["Email"] == "bob@example.com"

def test_lookup_finds_case_insensitive(reader, mock_gspread):
    mock_client = mock_gspread.return_value
    mock_sheet = mock_client.open_by_key.return_value.worksheet.return_value
    mock_sheet.get_all_records.return_value = [
        {"Name": "Alice", "Phone": "123-4567"},
        {"Name": "Bob", "Phone": "987-6543"}
    ]

    result = reader.lookup("Name", "alice")
    assert result is not None
    assert result["Name"] == "Alice"

    result = reader.lookup("Name", "BOB")
    assert result is not None
    assert result["Name"] == "Bob"

def test_lookup_returns_none_for_missing_value(reader, mock_gspread):
    mock_client = mock_gspread.return_value
    mock_sheet = mock_client.open_by_key.return_value.worksheet.return_value
    mock_sheet.get_all_records.return_value = [
        {"Name": "Alice", "Phone": "123-4567"}
    ]

    result = reader.lookup("Name", "Charlie")
    assert result is None

def test_missing_key_file_raises_file_not_found():
    with patch("os.path.exists", return_value=False):
        with pytest.raises(FileNotFoundError, match="key file missing"):
            FamilySheetReader()

def test_sheet_not_found_raises_family_sheet_error(reader, mock_gspread):
    import gspread
    mock_client = mock_gspread.return_value
    mock_client.open_by_key.side_effect = gspread.exceptions.SpreadsheetNotFound

    with pytest.raises(FamilySheetError, match="not found"):
        reader.get_all_rows()
