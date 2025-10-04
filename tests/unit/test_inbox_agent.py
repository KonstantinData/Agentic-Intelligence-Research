from agents.inbox_agent import InboxAgent


def test_parse_dossier_decision_yes_variants():
    body = "Yes\nthanks"
    assert InboxAgent.parse_dossier_decision(body) == "approved"


def test_parse_dossier_decision_no_variants():
    body = "No\nplease stop"
    assert InboxAgent.parse_dossier_decision(body) == "declined"


def test_parse_dossier_decision_unknown():
    body = "Maybe"
    assert InboxAgent.parse_dossier_decision(body) is None


def test_parse_missing_info_key_values_extracts_known_keys():
    body = "Company Name: Example Inc\nweb_domain: example.com\nignore: value"
    result = InboxAgent.parse_missing_info_key_values(body)
    assert result == {"company_name": "Example Inc", "web_domain": "example.com"}
