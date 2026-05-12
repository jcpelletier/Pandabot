import pytest
from pandabot.family.query_parser import parse_query

def test_parse_what_is_phone():
    result = parse_query("What is Mom's phone number?")
    assert result == {"name": "Mom", "column": "Phone"}

def test_parse_when_is_birthday():
    result = parse_query("When is Alice's birthday?")
    assert result == {"name": "Alice", "column": "Birthday"}

def test_parse_where_does_live():
    result = parse_query("Where does Bob live?")
    assert result == {"name": "Bob", "column": "Address"}

def test_parse_whats_shorthand():
    result = parse_query("What's Charlie's email?")
    assert result == {"name": "Charlie", "column": "Email"}

def test_parse_relationship():
    result = parse_query("What is Dave's relationship?")
    assert result == {"name": "Dave", "column": "Relationship"}

def test_parse_unrelated_query_returns_none():
    assert parse_query("How's the weather?") is None
    assert parse_query("Who are you?") is None

def test_parse_extra_whitespace_and_punctuation():
    result = parse_query("  What is   Mom's   phone???  ")
    assert result == {"name": "Mom", "column": "Phone"}

def test_parse_case_insensitive():
    result = parse_query("what IS mom's PHONE?")
    assert result == {"name": "Mom", "column": "Phone"}

def test_parse_hyphenated_name():
    result = parse_query("What is Jean-Luc's address?")
    assert result == {"name": "Jean-Luc", "column": "Address"}
