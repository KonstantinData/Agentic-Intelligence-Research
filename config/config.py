import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from dotenv import load_dotenv

_ORIGINAL_ENV = os.environ.copy()
_SKIP_DOTENV = os.getenv("SETTINGS_SKIP_DOTENV") == "1"
if not _SKIP_DOTENV:
    load_dotenv()


_YAML_MODULE = None
_yaml_spec = importlib.util.find_spec("yaml")
if _yaml_spec and _yaml_spec.loader:
    _YAML_MODULE = importlib.util.module_from_spec(_yaml_spec)
    _yaml_spec.loader.exec_module(_YAML_MODULE)


def _normalise(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    return value


def _get_env_var(name: str, *, aliases: Tuple[str, ...] = ()) -> Optional[str]:
    """Return an environment variable using the conventional uppercase name.

    When aliases are provided, values defined in the pre-dotenv environment take
    precedence. This ensures runtime overrides can win even if ``.env`` supplies
    the primary key.
    """

    if aliases:
        primary_original = _normalise(_ORIGINAL_ENV.get(name))
        for alias in aliases:
            alias_original = _normalise(_ORIGINAL_ENV.get(alias))
            if alias_original is not None and primary_original is None:
                alias_value = _normalise(os.getenv(alias))
                if alias_value is not None:
                    return alias_value

    value = _normalise(os.getenv(name))
    if value is not None:
        return value

    for alias in aliases:
        alias_value = _normalise(os.getenv(alias))
        if alias_value is not None:
            return alias_value

    return None


def _get_int_env(name: str, default: int) -> int:
    """Fetch an integer environment variable with fallback to a default value."""

    raw_value = _get_env_var(name)
    if raw_value is None:
        return default

    try:
        return int(raw_value)
    except ValueError as exc:  # pragma: no cover - defensive programming branch
        raise ValueError(f"Environment variable {name} must be an integer.") from exc


def _get_float_env(name: str, default: float) -> float:
    """Fetch a floating point environment variable with fallback."""

    raw_value = _get_env_var(name)
    if raw_value is None:
        return default

    try:
        return float(raw_value)
    except ValueError as exc:  # pragma: no cover - defensive programming branch
        raise ValueError(f"Environment variable {name} must be a float.") from exc


def _get_bool_env(name: str, default: bool) -> bool:
    """Return a boolean value derived from an environment variable."""

    raw_value = _get_env_var(name)
    if raw_value is None:
        return default

    normalised = raw_value.strip().lower()
    if normalised in {"1", "true", "yes", "on"}:
        return True
    if normalised in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"Environment variable {name} must be a boolean (accepted values: 1/0, true/false)."
    )


def _get_path_env(name: str, default: Path) -> Path:
    """Return the path from an environment variable or a default."""

    raw_value = _get_env_var(name)
    if raw_value:
        return Path(raw_value).expanduser().resolve()
    return default


def _read_agent_config_file(path: Path) -> Mapping[str, Any]:
    """Load agent configuration overrides from a JSON or YAML file."""

    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")

    if suffix in {".yaml", ".yml"}:
        if _YAML_MODULE is None:
            raise RuntimeError(
                "PyYAML is required to load YAML agent configuration files."
            )
        data = _YAML_MODULE.safe_load(text) or {}
    elif suffix == ".json":
        data = json.loads(text)
    else:
        raise ValueError(
            f"Unsupported agent configuration format '{suffix}'. Use JSON or YAML."
        )

    if not isinstance(data, Mapping):
        raise ValueError(
            "Agent configuration file must contain a mapping at the top level."
        )

    return data


def _extract_agent_overrides(config_data: Mapping[str, Any]) -> Dict[str, str]:
    """Normalise agent override names from structured configuration data."""

    candidates = config_data
    agents_section = (
        config_data.get("agents") if isinstance(config_data, Mapping) else None
    )
    if isinstance(agents_section, Mapping):
        candidates = agents_section

    overrides: Dict[str, str] = {}
    key_aliases = {
        "polling": ("polling", "polling_agent"),
        "trigger": ("trigger", "trigger_agent"),
        "extraction": ("extraction", "extraction_agent"),
        "human": ("human", "human_agent"),
        "crm": ("crm", "crm_agent"),
        "internal_research": (
            "internal_research",
            "internal_research_agent",
        ),
        "dossier_research": (
            "dossier_research",
            "dossier_agent",
            "dossier_research_agent",
        ),
        "similar_companies": (
            "similar_companies",
            "similar_companies_agent",
            "similar_company_agent",
        ),
    }

    for key, aliases in key_aliases.items():
        for alias in aliases:
            value = candidates.get(alias)
            if isinstance(value, str) and value.strip():
                overrides[key] = value.strip()
                break

    return overrides


def _prefixed_env_mapping(prefix: str, cast: Callable[[str], Any]) -> Dict[str, Any]:
    """Extract a mapping of values from environment variables using a prefix."""

    result: Dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :].strip()
        if not suffix:
            continue
        normalised_key = suffix.lower()
        try:
            result[normalised_key] = cast(value)
        except (TypeError, ValueError):  # pragma: no cover - defensive branch
            raise ValueError(
                f"Environment variable {key} must be coercible to {cast.__name__}."
            )
    return result


def _cast_non_empty_str(value: Any) -> str:
    """Cast a value to a stripped non-empty string."""

    text = str(value).strip()
    if not text:
        raise ValueError("Expected a non-empty string value.")
    return text


def _coerce_mapping(
    mapping: Optional[Mapping[str, Any]], cast: Callable[[Any], Any]
) -> Dict[str, Any]:
    """Coerce keys to lowercase strings and values using ``cast``."""

    if not mapping:
        return {}

    result: Dict[str, Any] = {}
    for key, value in mapping.items():
        if value is None:
            continue
        try:
            result[str(key).lower()] = cast(value)
        except (
            TypeError,
            ValueError,
        ):  # pragma: no cover - configuration error surface
            raise ValueError(
                f"Invalid value for '{key}' in LLM configuration; expected {cast.__name__}."
            ) from None
    return result


class Settings:
    """Application configuration loaded from environment variables or defaults."""

    def __init__(self) -> None:
        self.cal_lookahead_days: int = _get_int_env("CAL_LOOKAHEAD_DAYS", 14)
        self.cal_lookback_days: int = _get_int_env("CAL_LOOKBACK_DAYS", 1)

        value = _get_env_var("GOOGLE_CALENDAR_ID")
        if not value:
            raise EnvironmentError("GOOGLE_CALENDAR_ID must be set")
        self.google_calendar_id: str = value
        self.google_oauth_credentials: Dict[str, str] = (
            self._load_google_oauth_credentials()
        )
        self.google_api_base_url: str = (
            _get_env_var("GOOGLE_API_BASE_URL") or "https://www.googleapis.com"
        )

        project_root = Path(__file__).resolve().parents[1]
        default_log_root = project_root / "log_storage" / "run_history"

        self.log_storage_dir: Path = _get_path_env("LOG_STORAGE_DIR", default_log_root)
        self.event_log_dir: Path = _get_path_env(
            "EVENT_LOG_DIR", self.log_storage_dir / "events"
        )
        self.workflow_log_dir: Path = _get_path_env(
            "WORKFLOW_LOG_DIR", self.log_storage_dir / "workflows"
        )
        self.run_log_dir: Path = _get_path_env(
            "RUN_LOG_DIR", self.log_storage_dir / "runs"
        )

        self.hubspot_access_token: Optional[str] = _get_env_var("HUBSPOT_ACCESS_TOKEN")
        self.hubspot_client_secret: Optional[str] = _get_env_var(
            "HUBSPOT_CLIENT_SECRET"
        )
        self.hubspot_api_base_url: str = _get_env_var("HUBSPOT_API_BASE_URL") or (
            "https://api.hubapi.com"
        )
        self.hubspot_request_timeout: int = _get_int_env("HUBSPOT_REQUEST_TIMEOUT", 10)
        self.hubspot_max_retries: int = _get_int_env("HUBSPOT_MAX_RETRIES", 3)
        self.hubspot_retry_backoff_seconds: float = _get_float_env(
            "HUBSPOT_RETRY_BACKOFF_SECONDS", 1.0
        )
        self.max_concurrent_hubspot: int = max(
            1, _get_int_env("MAX_CONCURRENT_HUBSPOT", 5)
        )
        self.max_concurrent_research: int = max(
            1, _get_int_env("MAX_CONCURRENT_RESEARCH", 3)
        )

        self.agent_log_dir: Path
        self.research_artifact_dir: Path
        self.research_pdf_dir: Path
        self.crm_attachment_base_url: str
        self._load_storage_extensions()

        self.prompt_directory: Path = _get_path_env(
            "PROMPT_DIRECTORY", project_root / "templates" / "prompts"
        )

        self.trigger_words: Optional[str] = _get_env_var("TRIGGER_WORDS")

        validator_flag = (
            (_get_env_var("SOFT_TRIGGER_VALIDATOR") or "on").strip().lower()
        )
        self.soft_trigger_validator_enabled: bool = validator_flag not in {
            "off",
            "0",
            "false",
        }
        self.synonym_trigger_path: Path = _get_path_env(
            "SYNONYM_TRIGGER_PATH", project_root / "config" / "synonym-trigger.txt"
        )
        self.validator_require_evidence_substring: bool = _get_bool_env(
            "VALIDATOR_REQUIRE_EVIDENCE_SUBSTRING", True
        )
        self.validator_fuzzy_evidence_threshold: float = _get_float_env(
            "VALIDATOR_FUZZY_EVIDENCE_THRESHOLD", 0.88
        )
        self.validator_similarity_method: str = (
            (_get_env_var("VALIDATOR_SIMILARITY_METHOD") or "jaccard").strip().lower()
        )
        self.validator_similarity_threshold: float = _get_float_env(
            "VALIDATOR_SIMILARITY_THRESHOLD", 0.60
        )
        self.soft_validator_write_artifacts: bool = _get_bool_env(
            "SOFT_VALIDATOR_WRITE_ARTIFACTS", False
        )

        self.compliance_mode: str = (
            (_get_env_var("COMPLIANCE_MODE") or "standard").strip().lower()
        )
        if self.compliance_mode not in {"standard", "strict"}:
            self.compliance_mode = "standard"

        default_mask_logs = True
        default_mask_messages = self.compliance_mode == "strict"
        self.mask_pii_in_logs: bool = _get_bool_env(
            "MASK_PII_IN_LOGS", default_mask_logs
        )
        self.mask_pii_in_messages: bool = _get_bool_env(
            "MASK_PII_IN_MESSAGES", default_mask_messages
        )

        self.daily_cost_cap: float = _get_float_env("DAILY_COST_CAP", 50.0)
        self.monthly_cost_cap: float = _get_float_env("MONTHLY_COST_CAP", 1000.0)
        self.service_rate_limits: Dict[str, int] = _prefixed_env_mapping(
            "SERVICE_RATE_LIMIT_", int
        )

        self.openai_api_base: str = (
            _get_env_var("OPENAI_API_BASE") or "https://api.openai.com"
        )

        # SMTP configuration
        self.smtp_host: Optional[str] = _get_env_var("SMTP_HOST")
        self.smtp_port: int = _get_int_env("SMTP_PORT", 465)
        self.smtp_username: Optional[str] = _get_env_var("SMTP_USER")
        self.smtp_password: Optional[str] = _get_env_var("SMTP_PASS")
        self.smtp_sender: Optional[str] = _get_env_var("SMTP_SENDER")

        # IMAP configuration
        self.imap_host: Optional[str] = _get_env_var("IMAP_HOST")
        self.imap_port: int = _get_int_env("IMAP_PORT", 993)
        self.imap_username: Optional[str] = _get_env_var("IMAP_USERNAME")
        self.imap_password: Optional[str] = _get_env_var("IMAP_PASSWORD")
        self.imap_use_ssl: bool = _get_bool_env("IMAP_USE_SSL", True)
        self.imap_mailbox: str = _get_env_var("IMAP_MAILBOX") or "INBOX"

        # HITL orchestration
        self.hitl_inbox_poll_seconds: float = _get_float_env(
            "HITL_INBOX_POLL_SECONDS", 60.0
        )
        self.hitl_timezone: str = _get_env_var("HITL_TIMEZONE") or "Europe/Berlin"
        self.hitl_admin_email: Optional[str] = _get_env_var("HITL_ADMIN_EMAIL")
        self.hitl_admin_reminder_hours: float = _get_float_env(
            "HITL_ADMIN_REMINDER_HOURS", 24.0
        )

        whitelist_env = _get_env_var("PII_FIELD_WHITELIST")
        whitelist = {
            "company_name",
            "company",
            "business",
            "business_name",
            "organisation",
            "organization",
            "org_name",
            "web_domain",
            "domain",
            "website",
            "summary",
            "description",
            "id",
            "event_id",
        }
        if whitelist_env:
            whitelist.update(
                {
                    item.strip().lower()
                    for item in whitelist_env.split(",")
                    if item and item.strip()
                }
            )
        self.pii_field_whitelist = whitelist

        self.agent_config_file: Optional[Path] = None
        self.agent_overrides: Dict[str, str] = {}
        self._raw_agent_config: Mapping[str, Any] = {}

        self.llm_confidence_thresholds: Dict[str, float] = {}
        self.llm_cost_caps: Dict[str, float] = {}
        self.llm_retry_budgets: Dict[str, int] = {}
        self.prompt_versions: Dict[str, str] = {}

        agent_config_path = _get_env_var("AGENT_CONFIG_FILE")
        if agent_config_path:
            candidate = Path(agent_config_path).expanduser().resolve()
            if candidate.exists():
                try:
                    data = _read_agent_config_file(candidate)
                    self._raw_agent_config = data
                    self.agent_overrides.update(_extract_agent_overrides(data))
                    self.agent_config_file = candidate
                except (
                    Exception
                ) as exc:  # pragma: no cover - configuration error surface
                    raise EnvironmentError(
                        f"Failed to read agent configuration from {candidate}: {exc}"
                    ) from exc

        env_overrides = {
            "polling": _get_env_var("POLLING_AGENT"),
            "trigger": _get_env_var("TRIGGER_AGENT"),
            "extraction": _get_env_var("EXTRACTION_AGENT"),
            "human": _get_env_var("HUMAN_AGENT"),
            "crm": _get_env_var("CRM_AGENT"),
        }
        self.agent_overrides.update(
            {key: value for key, value in env_overrides.items() if value}
        )

        self._load_llm_configuration(self._raw_agent_config)
        self._load_prompt_configuration(self._raw_agent_config)

    def _load_llm_configuration(self, config_data: Mapping[str, Any]) -> None:
        """Populate LLM configuration dictionaries from env defaults and YAML overrides."""

        default_confidence = {
            "trigger": _get_float_env("LLM_CONFIDENCE_THRESHOLD_TRIGGER", 0.6),
            "extraction": _get_float_env("LLM_CONFIDENCE_THRESHOLD_EXTRACTION", 0.55),
        }
        default_cost_caps = {
            "daily": _get_float_env("LLM_COST_CAP_DAILY", 25.0),
            "monthly": _get_float_env("LLM_COST_CAP_MONTHLY", 500.0),
        }
        default_retry_budgets = {
            "trigger": _get_int_env("LLM_RETRY_BUDGET_TRIGGER", 2),
            "extraction": _get_int_env("LLM_RETRY_BUDGET_EXTRACTION", 2),
        }

        confidence_thresholds = dict(default_confidence)
        confidence_thresholds.update(
            _prefixed_env_mapping("LLM_CONFIDENCE_THRESHOLD_", float)
        )

        cost_caps = dict(default_cost_caps)
        cost_caps.update(_prefixed_env_mapping("LLM_COST_CAP_", float))

        retry_budgets = dict(default_retry_budgets)
        retry_budgets.update(_prefixed_env_mapping("LLM_RETRY_BUDGET_", int))

        llm_section = (
            config_data.get("llm") if isinstance(config_data, Mapping) else None
        )
        if isinstance(llm_section, Mapping):
            confidence_thresholds.update(
                _coerce_mapping(llm_section.get("confidence_thresholds"), float)
            )
            cost_caps.update(_coerce_mapping(llm_section.get("cost_caps"), float))
            retry_budgets.update(_coerce_mapping(llm_section.get("retry_budgets"), int))

        self.llm_confidence_thresholds = confidence_thresholds
        self.llm_cost_caps = cost_caps
        self.llm_retry_budgets = retry_budgets

    def _load_prompt_configuration(self, config_data: Mapping[str, Any]) -> None:
        """Populate prompt version overrides from environment variables and config files."""

        prompt_versions = _prefixed_env_mapping("PROMPT_VERSION_", _cast_non_empty_str)

        prompts_section = (
            config_data.get("prompts") if isinstance(config_data, Mapping) else None
        )
        if isinstance(prompts_section, Mapping):
            prompt_versions.update(
                _coerce_mapping(prompts_section, _cast_non_empty_str)
            )

        self.prompt_versions = prompt_versions

    def _load_storage_extensions(self) -> None:
        """Load optional storage-related settings from environment variables."""

        self.agent_log_dir = _get_path_env(
            "AGENT_LOG_DIR", self.log_storage_dir / "agents"
        )

        research_root = self.log_storage_dir / "research"
        self.research_artifact_dir = _get_path_env(
            "RESEARCH_ARTIFACT_DIR", research_root / "artifacts"
        )
        self.research_pdf_dir = _get_path_env(
            "RESEARCH_PDF_DIR", research_root / "pdfs"
        )

        self.crm_attachment_base_url = _get_env_var("CRM_ATTACHMENT_BASE_URL") or ""

    def refresh_llm_configuration(self) -> None:
        """Reload LLM configuration from the configured sources."""

        config_data: Mapping[str, Any] = {}
        if self.agent_config_file and self.agent_config_file.exists():
            try:
                config_data = _read_agent_config_file(self.agent_config_file)
                self._raw_agent_config = config_data
            except Exception as exc:  # pragma: no cover - configuration error surface
                raise EnvironmentError(
                    f"Failed to refresh agent configuration from {self.agent_config_file}: {exc}"
                ) from exc
        else:
            self._raw_agent_config = {}

        self._load_storage_extensions()
        self._load_llm_configuration(self._raw_agent_config)
        self._load_prompt_configuration(self._raw_agent_config)

    def _load_google_oauth_credentials(self) -> Dict[str, str]:
        """Return Google OAuth credentials defined via environment variables."""

        env_mapping = {
            "client_id": "GOOGLE_CLIENT_ID",
            "client_secret": "GOOGLE_CLIENT_SECRET",
            "refresh_token": "GOOGLE_REFRESH_TOKEN",
            "token_uri": "GOOGLE_TOKEN_URI",
        }

        credentials = {
            key: value
            for key, env_name in env_mapping.items()
            if (value := _get_env_var(env_name))
        }

        optional_mapping = {
            "auth_uri": "GOOGLE_AUTH_URI",
            "project_id": "GOOGLE_PROJECT_ID",
            "redirect_uris": "GOOGLE_REDIRECT_URIS",
        }

        for key, env_name in optional_mapping.items():
            value = _get_env_var(env_name)
            if value:
                credentials[key] = value

        auth_provider = _get_env_var(
            "GOOGLE_AUTH_PROVIDER_X509_CERT_URL"
        ) or _get_env_var("GHOOGLE_AUTH_PROVIDER_X509_CERT_URL")
        if auth_provider:
            credentials["auth_provider_x509_cert_url"] = auth_provider

        return credentials


# Notes: Singleton instance for importing settings in other modules
settings = Settings()
