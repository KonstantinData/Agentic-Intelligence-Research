from datetime import datetime


from utils import datetime_formatting


def test_format_report_datetime_with_naive_datetime():
    naive = datetime(2024, 5, 17, 8, 30)

    formatted = datetime_formatting.format_report_datetime(naive)

    assert formatted == "17.05.2024 10:30 CET"


def test_format_report_datetime_with_iso_string():
    formatted = datetime_formatting.format_report_datetime("2024-05-17T08:30:00Z")

    assert formatted == "17.05.2024 10:30 CET"


def test_format_report_datetime_with_unparseable_input():
    assert datetime_formatting.format_report_datetime("not a timestamp") == "not a timestamp"
