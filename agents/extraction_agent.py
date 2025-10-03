# Notes:
# ExtractionAgent is responsible for extracting core business information
# (such as company name and web domain) from event dictionaries.
# This version implements basic logic and can be extended to use NLP or regex.

import logging
import re
from typing import Any, Dict, List, Optional

from agents.factory import register_agent
from agents.interfaces import BaseExtractionAgent


@register_agent(BaseExtractionAgent, "extraction", "default", is_default=True)
class ExtractionAgent(BaseExtractionAgent):
    """
    Agent for extracting required information (e.g., company name, web domain)
    from an event dictionary.
    """

    # Harden domain regex to handle trailing punctuation and normalization
    DOMAIN_REGEX = re.compile(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?=[.,;:\s\)]|$)")
    
    STOP_WORDS = {
        "first",
        "second",
        "third",
        "meeting",
        "touchpoint",
        "catchup",
        "catch-up",
        "catch",
        "sync",
        "call",
        "intro",
        "introduction",
        "kickoff",
        "kick-off",
        "planning",
        "review",
        "status",
        "weekly",
        "monthly",
        "daily",
        "update",
        "discussion",
        "talk",
        "conversation",
        "chat",
        "checkin",
        "check-in",
        "follow",
        "follow-up",
        "followup",
        "new",
        "exciting",
        "great",
        "important",
        "special",
        "strategy",
        "product",
        "sales",
        "marketing",
        "team",
        "with",
        "for",
        "from",
        "about",
        "around",
        "october",
        "november",
        "december",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        # German function/meeting terms
        "termin",
        "besprechung",
        "treffen",
        "gespräch",
        "gesprach",
        "austausch",
        "jour",
        "fixe",
        "abstimmung",
        "rücksprache",
        "rucksprache",
        "telko",
        "telefonat",
    }
    
    COMPANY_SUFFIXES = {
        "inc",
        "inc.",
        "llc",
        "ltd",
        "co",
        "co.",
        "corp",
        "corp.",
        "corporation",
        "group",
        "company",
        "ag",
        "gmbh",
        "plc",
        "sa",
        # DE/EU legal forms
        "kg",
        "ohg",
        "e.k.",
        "ek",
        "kgaa",
        "se",
        "s.p.a.",
        "spa",
        "s.a.",
        "b.v.",
        "bv",
        "nv",
        "ab",
        "sas",
        "oy",
        "as",
        "sp. z o.o.",
        "sp",
        "z",
        "o.o.",
    }
    
    SUBDOMAIN_EXCLUSIONS = {
        "www",
        "app",
        "api",
        "go",
        "get",
        "portal",
        "mail",
    }
    
    SECOND_LEVEL_TLDS = {
        "co",
        "com",
        "net",
        "org",
        "gov",
        "edu",
        # Common SLD zones
        "uk",
        "au",
        "br",
        "nz",
        "za",
        "in",
        "jp",
        "kr",
        "cn",
        "mx",
        "ar",
    }

    async def extract(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Asynchronously extract company metadata from the event payload."""
        try:
            summary = event.get("summary", "") or ""
            description = event.get("description", "") or ""

            company_name = self._clean_string(event.get("company_name"))
            company_name_source = "event" if company_name else None

            # Try to find web domain in a dedicated field or extract from text.
            web_domain = self._normalise_domain(event.get("web_domain"))
            web_domain_source = "event" if web_domain else None

            extracted_domain = self._find_domain_in_text(summary, description)
            if extracted_domain:
                if not web_domain:
                    web_domain = extracted_domain
                    web_domain_source = "text"

            if web_domain:
                derived_name = self._derive_company_from_domain(web_domain)
                if derived_name and (not company_name or company_name_source != "event"):
                    company_name = derived_name
                    company_name_source = "domain"

            if not company_name:
                text_candidates = self._generate_text_candidates(summary, description)
                for candidate_text in text_candidates:
                    candidate_name = self._extract_company_from_unstructured(candidate_text)
                    if candidate_name:
                        company_name = candidate_name
                        company_name_source = "text"
                        break

            info = {
                "company_name": company_name,
                "web_domain": web_domain,
            }
            
            # Track sources for each field
            sources = {
                "company_name": company_name_source or "none",
                "web_domain": web_domain_source or "none",
            }
            
            # Determine missing required fields
            required_fields = ["company_name", "web_domain"]
            missing = [field for field in required_fields if not info.get(field)]
            
            is_complete = len(missing) == 0
            status = "ok" if is_complete else "incomplete"

            # Notes:
            # You can extend this logic to extract more fields, or to use more advanced NLP if needed.

            return {
                "info": info,
                "is_complete": is_complete,
                "status": status,
                "missing": missing,
                "sources": sources,
            }
        except Exception as e:
            logging.error(f"Error during info extraction: {e}")
            raise

    def _generate_text_candidates(self, summary: str, description: str) -> List[str]:
        """Return text snippets ordered by confidence for unstructured extraction."""
        candidates: List[str] = []
        if summary:
            candidates.extend(self._normalise_segments(summary))
        if description:
            candidates.extend(self._normalise_segments(description))
        return candidates

    def _normalise_segments(self, text: str) -> List[str]:
        segments = re.split(r"[\n\r\-|:/]+", text)
        normalised: List[str] = []
        for raw_segment in segments:
            segment = raw_segment.strip()
            if not segment:
                continue
            if segment.islower():
                segment = segment.title()
            normalised.append(segment)
        return normalised

    def _extract_company_from_unstructured(self, text: str) -> Optional[str]:
        words = re.findall(r"[A-Za-z0-9&'\-\.]+", text)
        if not words:
            return None

        cleaned_words = [word.rstrip(".") for word in words]
        length = len(cleaned_words)
        idx = 0
        while idx < length:
            word = cleaned_words[idx]
            lowered = word.lower()
            if not word or not word[0].isalpha():
                idx += 1
                continue
            if lowered in self.STOP_WORDS or not word[0].isupper():
                idx += 1
                continue

            end = idx + 1
            while end < length:
                next_word = cleaned_words[end]
                next_lower = next_word.lower()
                if not next_word or not next_word[0].isupper():
                    break
                if next_lower in self.STOP_WORDS and next_lower not in self.COMPANY_SUFFIXES:
                    break
                end += 1

            candidate_words = cleaned_words[idx:end]
            if candidate_words:
                candidate = " ".join(candidate_words)
                return candidate
            idx = end
        return None

    def _find_domain_in_text(self, summary: str, description: str) -> Optional[str]:
        search_space = f"{description} {summary}".strip()
        if not search_space:
            return None
        match = self.DOMAIN_REGEX.search(search_space)
        if match:
            return match.group(0).lower()
        return None

    def _normalise_domain(self, domain: Optional[str]) -> Optional[str]:
        if not domain:
            return None
        domain = domain.strip().lower()
        if not domain:
            return None
        # Remove URL scheme if provided.
        domain = re.sub(r"^https?://", "", domain)
        # Remove trailing punctuation
        domain = re.sub(r"[.,;:\s\)]+$", "", domain)
        domain = domain.split("/")[0]
        # Normalize IDN/Punycode domains (convert to ASCII)
        try:
            # Handle punycode domains by encoding/decoding
            if domain.startswith("xn--") or "xn--" in domain:
                domain = domain.encode("ascii").decode("idna")
        except (UnicodeError, UnicodeDecodeError):
            # If decoding fails, keep original
            pass
        return domain or None

    def _derive_company_from_domain(self, domain: str) -> Optional[str]:
        parts = domain.split(".")
        parts = [part for part in parts if part and part not in self.SUBDOMAIN_EXCLUSIONS]
        if not parts:
            return None

        # Handle second-level TLDs like .co.uk, .com.au, etc.
        candidate_index = -2 if len(parts) >= 2 else -1
        if len(parts) >= 3 and parts[-2] in self.SECOND_LEVEL_TLDS:
            # e.g., foo.co.uk -> use 'foo'
            candidate_index = -3
        if abs(candidate_index) > len(parts):
            candidate_index = -len(parts)
        candidate = parts[candidate_index]
        candidate = re.sub(r"[^a-z0-9]+", " ", candidate)
        candidate = candidate.strip()
        if not candidate:
            return None
        return candidate.title()

    @staticmethod
    def _clean_string(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None
