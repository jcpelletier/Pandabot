import pytest
from unittest.mock import MagicMock, patch
import os
from pandabot.family.sheet_reader import FamilySheetReader
from googleapiclient.errors import HttpError

@pytest.fixture
def mock_credentials():
    with patch("google.oauth2.service_account.Credentials.from_service_account_file") as mock:
        yield mock

@pytest.fixture
def reader(mock_credentials, monkeypatch):
    monkeypatch.setenv("FAMILY_SHEET_ID", "test-sheet-id")
    # Mock os.path.exists to always return True for the key file
    with patch("os.path.exists", return_value=True):
        return FamilySheetReader(key_path="fake-key.json")

def test_reader_init_missing_env():
    with pytest.raises(ValueError, match="FAMILY_SHEET_ID environment variable not set"):
        with patch.dict(os.environ, {}, clear=True):
            FamilySheetReader()

def test_get_all_members_success(reader):
    mock_service = MagicMock()
    mock_sheet = mock_service.spreadsheets.return_value
    mock_values = mock_sheet.values.return_value
    mock_get = mock_values.get.return_value

    mock_get.execute.return_value = {
        "values": [
            ["First Name", "Last Name", "Discord Name", "DOB", "Relationship", "Location", "Phone", "Email", "Address", "Notes"],
            ["John", "Doe", "johndoe#1234", "1990-01-01", "Cousin", "NYC", "555-1234", "john@example.com", "123 Main St", "Some notes"],
            ["Jane", "Smith", "janesmith#5678", "1992-02-02", "Sister", "LA", "555-5678", "jane@example.com", "", None]
        ]
    }

    with patch("pandabot.family.sheet_reader.build", return_value=mock_service):
        members = reader.get_all_members()

    assert len(members) == 2
    assert members[0]["first_name"] == "John"
    assert members[0]["last_name"] == "Doe"
    assert members[1]["first_name"] == "Jane"
    assert members[1]["address"] == ""
    assert members[1]["notes"] == "" # Check None becomes empty string

def test_get_all_members_reordered_columns(reader):
    mock_service = MagicMock()
    mock_sheet = mock_service.spreadsheets.return_value
    mock_values = mock_sheet.values.return_value
    mock_get = mock_values.get.return_value

    # Discord Name moved to front, First Name moved elsewhere
    mock_get.execute.return_value = {
        "values": [
            ["Discord Name", "Last Name", "First Name", "DOB", "Relationship", "Location", "Phone", "Email", "Address", "Notes"],
            ["johndoe#1234", "Doe", "John", "1990-01-01", "Cousin", "NYC", "555-1234", "john@example.com", "123 Main St", "Some notes"]
        ]
    }

    with patch("pandabot.family.sheet_reader.build", return_value=mock_service):
        members = reader.get_all_members()

    assert len(members) == 1
    assert members[0]["first_name"] == "John"
    assert members[0]["discord_name"] == "johndoe#1234"

def test_get_all_members_missing_columns(reader):
    mock_service = MagicMock()
    mock_sheet = mock_service.spreadsheets.return_value
    mock_values = mock_sheet.values.return_value
    mock_get = mock_values.get.return_value

    # Missing 'Notes' and 'Address' columns
    mock_get.execute.return_value = {
        "values": [
            ["First Name", "Last Name", "Discord Name", "DOB", "Relationship", "Location", "Phone", "Email"],
            ["John", "Doe", "johndoe#1234", "1990-01-01", "Cousin", "NYC", "555-1234", "john@example.com"]
        ]
    }

    with patch("pandabot.family.sheet_reader.build", return_value=mock_service):
        members = reader.get_all_members()

    assert len(members) == 1
    assert members[0]["first_name"] == "John"
    assert members[0]["notes"] == ""
    assert members[0]["address"] == ""

def test_get_all_members_empty_sheet(reader):
    mock_service = MagicMock()
    mock_sheet = mock_service.spreadsheets.return_value
    mock_values = mock_sheet.values.return_value
    mock_get = mock_values.get.return_value

    mock_get.execute.return_value = {
        "values": [
            ["First Name", "Last Name", "Discord Name", "DOB", "Relationship", "Location", "Phone", "Email", "Address", "Notes"]
        ]
    }

    with patch("pandabot.family.sheet_reader.build", return_value=mock_service):
        members = reader.get_all_members()

    assert members == []

def test_find_member_fuzzy(reader):
    sample_data = [
        {"first_name": "John", "last_name": "Doe", "discord_name": "johndoe#1234"},
        {"first_name": "Jane", "last_name": "Smith", "discord_name": "janesmith#5678"}
    ]

    with patch.object(FamilySheetReader, "get_all_members", return_value=sample_data):
        # Exact match
        assert reader.find_member("John")["first_name"] == "John"
        # Fuzzy match
        assert reader.find_member("Jon")["first_name"] == "John"
        # Discord name match
        assert reader.find_member("johndoe")["first_name"] == "John"
        # No match
        assert reader.find_member("Zzzzz") is None

def test_search(reader):
    sample_data = [
        {"first_name": "John", "last_name": "Doe", "notes": "Likes pizza"},
        {"first_name": "Jane", "last_name": "Smith", "notes": "Likes pasta"}
    ]

    with patch.object(FamilySheetReader, "get_all_members", return_value=sample_data):
        # Search by name
        assert len(reader.search("John")) == 1
        # Search by notes
        assert len(reader.search("pizza")) == 1
        # Case insensitive
        assert len(reader.search("PIZZA")) == 1
        # No results
        assert reader.search("burger") == []

def test_google_api_error_handled(reader):
    with patch("pandabot.family.sheet_reader.build") as mock_build:
        mock_service = mock_build.return_value
        mock_sheet = mock_service.spreadsheets.return_value
        mock_values = mock_sheet.values.return_value
        mock_get = mock_values.get.return_value

        # Mock a HttpError
        mock_get.execute.side_effect = HttpError(MagicMock(status=403), b"Forbidden")

        members = reader.get_all_members()
        assert members == []
