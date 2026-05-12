import re

FAMILY_COLUMNS = {
    "name": "Name",
    "phone": "Phone",
    "email": "Email",
    "address": "Address",
    "birthday": "Birthday",
    "relation": "Relationship",
}

def parse_query(text: str) -> dict | None:
    """
    Parses natural language questions about family info.
    Returns {"name": extracted_name, "column": mapped_column} or None.
    """
    # Normalize text
    text = text.lower().strip().rstrip("?")
    text = text.replace("what's", "what is")

    # 1. "What is [person]'s [attribute]?"
    # Matches: "What is Mom's phone number", "What is Alice's email", "What is Jean-Luc's address"
    match = re.search(r"what is ([\w\s-]+)'s ([\w\s]+)", text)
    if match:
        name = match.group(1).strip().title()
        attr = match.group(2).strip()

        # Mapping attributes
        column = None
        if "phone" in attr:
            column = FAMILY_COLUMNS["phone"]
        elif "email" in attr:
            column = FAMILY_COLUMNS["email"]
        elif "address" in attr:
            column = FAMILY_COLUMNS["address"]
        elif "birthday" in attr:
            column = FAMILY_COLUMNS["birthday"]
        elif "relation" in attr or "relationship" in attr:
            column = FAMILY_COLUMNS["relation"]

        if column:
            return {"name": name, "column": column}

    # 2. "When is [person]'s birthday?"
    match = re.search(r"when is ([\w\s-]+)'s birthday", text)
    if match:
        name = match.group(1).strip().title()
        return {"name": name, "column": FAMILY_COLUMNS["birthday"]}

    # 3. "Where does [person] live?"
    match = re.search(r"where does ([\w\s-]+) live", text)
    if match:
        name = match.group(1).strip().title()
        return {"name": name, "column": FAMILY_COLUMNS["address"]}

    # 4. "What is [person]'s phone number?" (alternative phrasing if #1 missed it)
    match = re.search(r"what is ([\w\s-]+)'s phone", text)
    if match:
        name = match.group(1).strip().title()
        return {"name": name, "column": FAMILY_COLUMNS["phone"]}

    return None
