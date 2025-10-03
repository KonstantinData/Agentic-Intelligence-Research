"""Tests for extraction agent enhancements."""

import pytest

from agents.extraction_agent import ExtractionAgent


@pytest.mark.asyncio
async def test_extract_returns_enhanced_contract():
    """Test that extract returns status, missing, and sources."""
    agent = ExtractionAgent()
    event = {
        "summary": "Meeting with Acme Corp",
        "description": "Visit acme.com for details.",
    }

    result = await agent.extract(event)

    assert "status" in result
    assert "missing" in result
    assert "sources" in result
    assert result["status"] == "ok"
    assert result["missing"] == []
    assert result["is_complete"] is True


@pytest.mark.asyncio
async def test_extract_reports_missing_fields():
    """Test that extract reports missing fields correctly."""
    agent = ExtractionAgent()
    event = {
        "summary": "Meeting next week",
        "description": "Discussion about plans.",
    }

    result = await agent.extract(event)

    assert result["status"] == "incomplete"
    assert "company_name" in result["missing"] or "web_domain" in result["missing"]
    assert result["is_complete"] is False


@pytest.mark.asyncio
async def test_extract_tracks_sources():
    """Test that extract tracks field sources."""
    agent = ExtractionAgent()
    event = {
        "company_name": "TestCorp",
        "summary": "Meeting",
        "description": "Visit test.com",
    }

    result = await agent.extract(event)

    assert result["sources"]["company_name"] == "event"
    assert result["sources"]["web_domain"] == "text"


@pytest.mark.asyncio
async def test_extract_handles_german_stop_words():
    """Test that German meeting terms are filtered out."""
    agent = ExtractionAgent()
    event = {
        "summary": "Termin mit Acme Besprechung",
        "description": "Important Treffen about products.",
    }

    result = await agent.extract(event)

    # Should extract Acme, not Termin/Besprechung/Treffen
    assert result["info"]["company_name"] == "Acme"


@pytest.mark.asyncio
async def test_extract_handles_de_legal_forms():
    """Test that DE/EU legal forms are recognized."""
    agent = ExtractionAgent()
    event = {
        "summary": "Meeting with Siemens AG",
        "description": "Discussion with Volkswagen GmbH and BMW KG.",
    }

    result = await agent.extract(event)

    # Should extract company name
    company = result["info"]["company_name"]
    assert company is not None
    # Should handle AG/GmbH/KG suffixes
    assert any(term in company.lower() for term in ["siemens", "volkswagen", "bmw"])


@pytest.mark.asyncio
async def test_extract_handles_second_level_tlds():
    """Test that .co.uk, .com.au, etc. are handled correctly."""
    agent = ExtractionAgent()
    event = {
        "summary": "Meeting",
        "description": "Visit example.co.uk and test.com.au for details.",
    }

    result = await agent.extract(event)

    # Should extract domain
    domain = result["info"]["web_domain"]
    assert domain is not None
    assert "co.uk" in domain or "com.au" in domain


@pytest.mark.asyncio
async def test_derive_company_from_second_level_tld():
    """Test deriving company name from .co.uk domain."""
    agent = ExtractionAgent()
    
    # Test foo.co.uk -> Foo
    result = agent._derive_company_from_domain("foo.co.uk")
    assert result == "Foo"
    
    # Test bar.com.au -> Bar
    result = agent._derive_company_from_domain("bar.com.au")
    assert result == "Bar"
    
    # Test baz.com -> Baz
    result = agent._derive_company_from_domain("baz.com")
    assert result == "Baz"


@pytest.mark.asyncio
async def test_normalise_domain_removes_trailing_punctuation():
    """Test that domain normalization removes trailing punctuation."""
    agent = ExtractionAgent()
    
    assert agent._normalise_domain("example.com.") == "example.com"
    assert agent._normalise_domain("example.com,") == "example.com"
    assert agent._normalise_domain("example.com;") == "example.com"
    assert agent._normalise_domain("https://example.com/") == "example.com"
