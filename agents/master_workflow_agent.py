"""MasterWorkflowAgent: Pure logic agent for polling and event-processing."""

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents.factory import create_agent

# Ensure default agent implementations register themselves with the factory.
from agents import (  # noqa: F401  # pylint: disable=unused-import
    crm_agent as _crm_module,
    dossier_research_agent as _dossier_research_module,
    event_polling_agent as _polling_module,
    extraction_agent as _extraction_module,
    human_in_loop_agent as _human_module,
    int_lvl_1_agent as _similar_company_module,
    internal_research_agent as _internal_research_module,
    trigger_detection_agent as _trigger_module,
)
from agents.interfaces import (
    BaseCrmAgent,
    BaseExtractionAgent,
    BaseHumanAgent,
    BasePollingAgent,
    BaseResearchAgent,
    BaseTriggerAgent,
)
from agents.local_storage_agent import LocalStorageAgent
from config.config import settings
from config.watcher import LlmConfigurationWatcher
from logs.workflow_log_manager import WorkflowLogManager
from utils import concurrency
from utils.audit_log import AuditLog
from utils.observability import (
    observe_operation,
    record_hitl_outcome,
    record_trigger_match,
)
from utils.negative_cache import NegativeEventCache
from utils.pii import mask_pii
from utils.trigger_loader import load_trigger_words
from utils.workflow_steps import workflow_step_recorder  # NEU

logger = logging.getLogger("MasterWorkflowAgent")


class MasterWorkflowAgent:
    def __init__(
        self,
        communication_backend: Optional[Any] = None,
        event_agent: Optional[BasePollingAgent] = None,
        trigger_agent: Optional[BaseTriggerAgent] = None,
        extraction_agent: Optional[BaseExtractionAgent] = None,
        human_agent: Optional[BaseHumanAgent] = None,
        crm_agent: Optional[BaseCrmAgent] = None,
        agent_overrides: Optional[Dict[str, str]] = None,
    ) -> None:
        resolved_overrides: Dict[str, str] = dict(settings.agent_overrides)
        if agent_overrides:
            resolved_overrides.update({k: v for k, v in agent_overrides.items() if v})

        self.event_agent = event_agent or create_agent(
            BasePollingAgent,
            resolved_overrides.get("polling"),
            config=settings,
        )
        trigger_words_file = (
            Path(__file__).resolve().parents[1] / "config" / "trigger_words.txt"
        )
        self.trigger_words = load_trigger_words(
            settings.trigger_words, triggers_file=trigger_words_file, logger=logger
        )
        self._rule_hash = hashlib.sha256(
            "\n".join(sorted(self.trigger_words)).encode("utf-8")
        ).hexdigest()
        self.trigger_agent = trigger_agent or create_agent(
            BaseTriggerAgent,
            resolved_overrides.get("trigger"),
            trigger_words=self.trigger_words,
        )
        self.extraction_agent = extraction_agent or create_agent(
            BaseExtractionAgent,
            resolved_overrides.get("extraction"),
        )
        self.human_agent = human_agent or create_agent(
            BaseHumanAgent,
            resolved_overrides.get("human"),
            communication_backend=communication_backend,
        )
        self.crm_agent = crm_agent or create_agent(
            BaseCrmAgent,
            resolved_overrides.get("crm"),
        )

        self.on_pending_audit = None
        self.internal_research_agent = self._create_research_agent(
            resolved_overrides.get("internal_research"),
            default_name=None,
            required=False,
            description="internal research",
        )
        self.dossier_research_agent = self._create_research_agent(
            resolved_overrides.get("dossier_research"),
            default_name="dossier_research",
            required=False,
            description="dossier research",
        )
        self.similar_companies_agent = self._create_research_agent(
            resolved_overrides.get("similar_companies"),
            default_name="similar_companies_level1",
            required=False,
            description="similar company",
        )

        self.storage_agent = LocalStorageAgent(settings.run_log_dir, logger=logger)
        self.workflow_log_manager = WorkflowLogManager(settings.workflow_log_dir)
        self._negative_cache_path = (
            self.storage_agent.base_dir / "state" / "negative_cache.json"
        )
        self._negative_cache: Optional[NegativeEventCache] = None

        self.run_id: str = ""
        self.run_directory: Path = self.storage_agent.base_dir
        self.log_file_path: Path = self.run_directory / "polling_trigger.log"
        self.log_filename: str = str(self.log_file_path)
        self.audit_log: Optional[AuditLog] = None

        self.llm_confidence_thresholds: Dict[str, float] = {}
        self.llm_cost_caps: Dict[str, float] = {}
        self.llm_retry_budgets: Dict[str, int] = {}
        self._apply_llm_settings(settings)

        self._config_watcher = LlmConfigurationWatcher(
            settings, on_update=self._apply_llm_settings
        )
        self._config_watcher.start()

    def attach_run(self, run_id: str, workflow_log_manager: WorkflowLogManager) -> None:
        if not run_id:
            raise ValueError("attach_run requires a non-empty run_id")

        self.run_id = run_id
        self.workflow_log_manager = workflow_log_manager

        self.run_directory = self.storage_agent.create_run_directory(self.run_id)
        self.log_file_path = self.run_directory / "polling_trigger.log"
        self.log_filename = str(self.log_file_path)

        audit_log_path = self.storage_agent.get_audit_log_path(self.run_id)
        self.audit_log = AuditLog(audit_log_path, logger=logger)
        if hasattr(self.human_agent, "set_audit_log"):
            self.human_agent.set_audit_log(self.audit_log)
        if hasattr(self.human_agent, "set_run_context"):
            try:
                self.human_agent.set_run_context(self.run_id, self.workflow_log_manager)
            except Exception:
                logger.exception("Failed to set run context on human agent")

        existing_handler: Optional[logging.FileHandler] = None
        for handler in list(logger.handlers):
            if isinstance(handler, logging.FileHandler) and getattr(
                handler, "_master_agent_handler", False
            ):
                existing_handler = handler
                break

        expected_path = str(self.log_file_path)
        file_handler: Optional[logging.FileHandler] = None
        if existing_handler is not None:
            handler_path = getattr(existing_handler, "_master_agent_handler_path", None)
            if handler_path == expected_path:
                file_handler = existing_handler
            else:
                logger.removeHandler(existing_handler)
                existing_handler.close()

        if file_handler is None:
            file_handler = logging.FileHandler(
                self.log_file_path, mode="w", encoding="utf-8"
            )

            class _RunIdInjector(logging.Filter):
                def __init__(self, current_run_id: str) -> None:
                    super().__init__(name="master-workflow-run")
                    self._run_id = current_run_id

                def filter(
                    self, record: logging.LogRecord
                ) -> bool:  # pragma: no cover - simple setter
                    if getattr(record, "run_id", None) in {None, ""}:
                        record.run_id = self._run_id
                    return True

            run_filter = _RunIdInjector(self.run_id)
            file_handler.addFilter(run_filter)
            setattr(file_handler, "_master_agent_run_id_filter", run_filter)
            formatter = logging.Formatter(
                "%(asctime)s %(levelname)s [run_id=%(run_id)s] %(message)s"
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(logging.INFO)
            setattr(file_handler, "_master_agent_handler", True)
            setattr(file_handler, "_master_agent_handler_path", expected_path)
            logger.addHandler(file_handler)
        else:
            run_filter = getattr(file_handler, "_master_agent_run_id_filter", None)
            if isinstance(run_filter, logging.Filter):
                # Update run_id on reused handler in case of manual reattachment.
                setattr(run_filter, "_run_id", self.run_id)

        logger.setLevel(logging.INFO)

    async def process_all_events(self) -> List[Dict[str, Any]]:
        logger.info("MasterWorkflowAgent: Processing events...")

        processed_results: List[Dict[str, Any]] = []
        if self._negative_cache is None:
            self._negative_cache = NegativeEventCache.load(
                self._negative_cache_path,
                rule_hash=self._rule_hash,
                now=time.time(),
            )
        events = await self.event_agent.poll()
        for event in events:
            masked_event = self._mask_for_logging(event)
            logger.info("Polled event: %s", masked_event)
            event_id = event.get("id")

            event_result: Dict[str, Any] = {
                "event_id": event_id,
                "research": {},
                "research_errors": [],
                "status": "received",
            }
            processed_results.append(event_result)

            # Step: start
            workflow_step_recorder.record_step(self.run_id, event_id, "start")

            if self._negative_cache and self._negative_cache.should_skip(
                event, self._rule_hash
            ):
                decision = self._negative_cache.get_decision(
                    event_id if isinstance(event_id, str) else None
                )
                logger.info(
                    "Prefilter skip (negative_cache) event_id=%s decision=%s",
                    event_id,
                    decision,
                )
                workflow_step_recorder.record_step(
                    self.run_id, event_id, "prefilter.negative_cache"
                )
                event_result["status"] = "skipped_negative_cache"
                continue

            with observe_operation(
                "trigger_detection", {"event.id": str(event_id)} if event_id else None
            ):
                trigger_result = await self._detect_trigger(event)
            event_result["trigger"] = trigger_result

            if not self._meets_confidence_threshold("trigger", trigger_result):
                logger.info(
                    "Skipping event %s due to trigger confidence %.3f below threshold %.3f",
                    event_id,
                    trigger_result.get("confidence", 0.0),
                    self.llm_confidence_thresholds.get("trigger", 0.0),
                )
                event_result["status"] = "skipped_trigger_threshold"
                if self._negative_cache:
                    self._negative_cache.record_no_trigger(
                        event,
                        self._rule_hash,
                        "skipped_trigger_threshold",
                    )
                continue
            if not trigger_result.get("trigger"):
                logger.info(f"No trigger detected for event {event_id}")
                event_result["status"] = "no_trigger"
                if self._negative_cache:
                    self._negative_cache.record_no_trigger(
                        event, self._rule_hash, "no_trigger"
                    )
                continue

            record_trigger_match(trigger_result.get("type", "unknown"))
            if self._negative_cache:
                self._negative_cache.forget(event_id)
            masked_trigger = self._mask_for_logging(trigger_result)
            logger.info(
                "%s trigger detected in event %s (matched: %s in %s)",
                masked_trigger.get("type", "").capitalize(),
                event_id,
                masked_trigger.get("matched_word"),
                masked_trigger.get("matched_field"),
            )

            with observe_operation(
                "extraction", {"event.id": str(event_id)} if event_id else None
            ):
                extraction_input = dict(event)
                context = trigger_result.get("extraction_context")
                if isinstance(context, dict):
                    soft_matches = context.get("soft_trigger_matches")
                    if soft_matches:
                        extraction_input["soft_trigger_matches"] = soft_matches
                    hard_triggers = context.get("hard_triggers")
                    if hard_triggers:
                        extraction_input["hard_triggers"] = hard_triggers
                    for field in ("summary", "description"):
                        if (
                            field not in extraction_input
                            and context.get(field) is not None
                        ):
                            extraction_input[field] = context.get(field)
                extracted = await self.extraction_agent.extract(extraction_input)
            event_result["extraction"] = extracted

            info = extracted.get("info", {}) or {}
            is_complete = bool(extracted.get("is_complete"))
            if not self._meets_confidence_threshold("extraction", extracted):
                logger.info(
                    "Extraction confidence %.3f below threshold %.3f for event %s",
                    extracted.get("confidence", 0.0),
                    self.llm_confidence_thresholds.get("extraction", 0.0),
                    event_id,
                )
                event_result["status"] = "skipped_extraction_threshold"
                continue

            normalised_info = self._normalise_info_for_research(info)
            internal_status = None
            internal_result = None

            # Verhindere doppelten internen Research bei Hard Trigger + vollständigen Daten:
            if self._has_research_inputs(normalised_info) and not (
                is_complete and trigger_result.get("type") == "hard"
            ):
                internal_result = await self._run_internal_research(
                    event_result,
                    event,
                    normalised_info,
                    event_id,
                    force=False,
                )
                internal_status = self._extract_internal_status(internal_result)
                workflow_step_recorder.record_step(
                    self.run_id, event_id, "internal_lookup_completed"
                )

            if is_complete and internal_status == "AWAIT_REQUESTOR_DETAILS":
                event_result["status"] = "awaiting_requestor_details"
                continue
            if is_complete and internal_status == "AWAIT_REQUESTOR_DECISION":
                event_result["status"] = "awaiting_requestor_decision"
                continue

            if trigger_result.get("type") == "hard":
                if is_complete:
                    await self._process_crm_dispatch(
                        event,
                        normalised_info,
                        event_result,
                        event_id,
                        force_internal=False,
                    )
                    continue
                else:
                    with observe_operation(
                        "hitl_missing_info",
                        {"event.id": str(event_id)} if event_id else None,
                    ):
                        filled = self.human_agent.request_info(event, extracted)
                    audit_id = filled.get("audit_id")
                    status = filled.get("status")
                    if isinstance(status, str) and status.lower() == "pending":
                        logger.info(
                            "Missing info request pending for event %s [audit_id=%s]",
                            event_id,
                            audit_id or "n/a",
                        )
                        record_hitl_outcome("missing_info", "pending")
                        event_result["status"] = "missing_info_pending"
                        if audit_id and callable(self.on_pending_audit):
                            context = {
                                "event": event,
                                "info": info,
                                "requested_fields": extracted.get("missing") or [],
                                "run_id": self.run_id,
                                "event_id": event_id,
                                "kind": "missing_info",
                            }
                            try:
                                self.on_pending_audit("missing_info", audit_id, context)
                            except Exception:
                                logger.exception(
                                    "on_pending_audit callback failed for missing_info"
                                )
                        continue
                    if filled.get("is_complete"):
                        logger.info(
                            "Missing info provided for event %s [audit_id=%s]",
                            event_id,
                            audit_id or "n/a",
                        )
                        workflow_step_recorder.record_step(
                            self.run_id, event_id, "missing_optional_fields"
                        )
                        workflow_step_recorder.record_step(
                            self.run_id, event_id, "fields_validated"
                        )
                        record_hitl_outcome("missing_info", "completed")
                        filled_info = self._normalise_info_for_research(
                            filled.get("info", {}) or {}
                        )
                        await self._process_crm_dispatch(
                            event,
                            filled_info,
                            event_result,
                            event_id,
                            force_internal=True,
                        )
                    else:
                        logger.warning(
                            "Required info still missing for event %s [audit_id=%s]",
                            event_id,
                            audit_id or "n/a",
                        )
                        record_hitl_outcome("missing_info", "incomplete")
                        event_result["status"] = "missing_info_incomplete"
                continue

            if trigger_result.get("type") == "soft" and is_complete:
                with observe_operation(
                    "hitl_dossier", {"event.id": str(event_id)} if event_id else None
                ):
                    response = self.human_agent.request_dossier_confirmation(
                        event, info
                    )
                event_result["hitl_dossier"] = response
                audit_id = response.get("audit_id")
                status = self._resolve_dossier_status(response)
                if status == "pending":
                    self._log_dossier_pending(event_id, audit_id, response)
                    record_hitl_outcome("dossier", "pending")
                    event_result["status"] = "dossier_pending"
                    if audit_id and callable(self.on_pending_audit):
                        context = {
                            "event": event,
                            "info": info,
                            "run_id": self.run_id,
                            "event_id": event_id,
                            "kind": "dossier",
                        }
                        try:
                            self.on_pending_audit("dossier", audit_id, context)
                        except Exception:
                            logger.exception(
                                "on_pending_audit callback failed for dossier"
                            )
                elif response.get("dossier_required") or status == "approved":
                    logger.info(
                        "Organizer approved dossier for event %s [audit_id=%s]: %s",
                        event_id,
                        audit_id or "n/a",
                        self._mask_for_logging(response.get("details")),
                    )
                    record_hitl_outcome("dossier", "approved")
                    await self._process_crm_dispatch(
                        event,
                        normalised_info,
                        event_result,
                        event_id,
                        force_internal=False,
                    )
                else:
                    logger.info(
                        "Organizer declined dossier for event %s [audit_id=%s]: %s",
                        event_id,
                        audit_id or "n/a",
                        self._mask_for_logging(response.get("details")),
                    )
                    record_hitl_outcome("dossier", "declined")
                    event_result["status"] = "dossier_declined"
                continue

            if trigger_result.get("type") == "soft" and not is_complete:
                with observe_operation(
                    "hitl_dossier", {"event.id": str(event_id)} if event_id else None
                ):
                    response = self.human_agent.request_dossier_confirmation(
                        event, info
                    )
                event_result["hitl_dossier"] = response
                audit_id = response.get("audit_id")
                status = self._resolve_dossier_status(response)
                if status == "pending":
                    self._log_dossier_pending(event_id, audit_id, response)
                    record_hitl_outcome("dossier", "pending")
                    event_result["status"] = "dossier_pending"
                    if audit_id and callable(self.on_pending_audit):
                        context = {
                            "event": event,
                            "info": info,
                            "run_id": self.run_id,
                            "event_id": event_id,
                            "kind": "dossier",
                        }
                        try:
                            self.on_pending_audit("dossier", audit_id, context)
                        except Exception:
                            logger.exception(
                                "on_pending_audit callback failed for dossier"
                            )
                elif response.get("dossier_required") or status == "approved":
                    logger.info(
                        "Organizer approved dossier for event %s [audit_id=%s]: %s",
                        event_id,
                        audit_id or "n/a",
                        self._mask_for_logging(response.get("details")),
                    )
                    record_hitl_outcome("dossier", "approved")
                    with observe_operation(
                        "hitl_missing_info",
                        {"event.id": str(event_id)} if event_id else None,
                    ):
                        filled = self.human_agent.request_info(event, extracted)
                    fill_audit_id = filled.get("audit_id")
                    if filled.get("is_complete"):
                        logger.info(
                            "Missing info provided for event %s [audit_id=%s]",
                            event_id,
                            fill_audit_id or "n/a",
                        )
                        workflow_step_recorder.record_step(
                            self.run_id, event_id, "missing_optional_fields"
                        )
                        workflow_step_recorder.record_step(
                            self.run_id, event_id, "fields_validated"
                        )
                        record_hitl_outcome("missing_info", "completed")
                        filled_info = self._normalise_info_for_research(
                            filled.get("info", {}) or {}
                        )
                        await self._process_crm_dispatch(
                            event,
                            filled_info,
                            event_result,
                            event_id,
                            force_internal=True,
                        )
                    else:
                        logger.warning(
                            "Required info still missing after HITL for event %s [audit_id=%s]",
                            event_id,
                            fill_audit_id or "n/a",
                        )
                        record_hitl_outcome("missing_info", "incomplete")
                        event_result["status"] = "missing_info_incomplete"
                else:
                    logger.info(
                        "Organizer declined dossier for event %s [audit_id=%s]: %s",
                        event_id,
                        audit_id or "n/a",
                        self._mask_for_logging(response.get("details")),
                    )
                    record_hitl_outcome("dossier", "declined")
                    event_result["status"] = "dossier_declined"
                continue

            logger.warning(f"Unhandled trigger/info state for event {event_id}")
            event_result["status"] = "unhandled_state"

        return processed_results

    async def _detect_trigger(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return await self.trigger_agent.check(event)

    async def _send_to_crm_agent(
        self, event: Dict[str, Any], info: Dict[str, Any]
    ) -> None:
        await self.crm_agent.send(event, info)

    def _create_research_agent(
        self,
        override_name: Optional[str],
        *,
        default_name: Optional[str],
        required: bool,
        description: str,
    ) -> Optional[BaseResearchAgent]:
        name = override_name or default_name
        label = name or "<default>"
        try:
            return create_agent(BaseResearchAgent, name)  # type: ignore[arg-type]
        except KeyError:
            if required:
                logger.error(
                    "Required %s agent '%s' is not registered.", description, label
                )
                raise
            logger.warning(
                "%s agent '%s' is not registered; continuing without it.",
                description.capitalize(),
                label,
            )
            return None
        except OSError as exc:
            logger.warning(
                "Unable to initialise %s agent '%s': %s",
                description,
                label,
                exc,
            )
            return None

    def _normalise_info_for_research(self, info: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(info or {})
        if "company_name" not in payload and payload.get("name"):
            payload["company_name"] = payload.get("name")
        domain = (
            payload.get("company_domain")
            or payload.get("web_domain")
            or payload.get("domain")
        )
        if domain:
            payload["company_domain"] = domain
        return payload

    def _has_research_inputs(self, info: Dict[str, Any]) -> bool:
        return bool(info.get("company_name")) and bool(info.get("company_domain"))

    def _build_research_trigger(
        self,
        event: Dict[str, Any],
        info: Dict[str, Any],
        event_id: Optional[Any],
    ) -> Dict[str, Any]:
        payload = dict(info)
        payload.setdefault("run_id", self.run_id)
        if event_id is not None:
            payload.setdefault("event_id", event_id)
        trigger: Dict[str, Any] = {
            "id": event_id,
            "event_id": event_id,
            "run_id": self.run_id,
            "source": "workflow_orchestrator",
            "payload": payload,
        }
        creator = event.get("creator") if isinstance(event, dict) else None
        recipient = event.get("recipient") if isinstance(event, dict) else None
        if creator:
            trigger["creator"] = creator
        if recipient:
            trigger["recipient"] = recipient
        return trigger

    async def _run_research_agent(
        self,
        agent: Optional[BaseResearchAgent],
        agent_name: str,
        event_result: Dict[str, Any],
        event: Dict[str, Any],
        info: Dict[str, Any],
        event_id: Optional[Any],
        *,
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if agent is None:
            self._log_research_step(
                agent_name,
                event_id,
                "skipped",
                details={"reason": "agent_unavailable"},
            )
            return None

        research_store = event_result.setdefault("research", {})
        if agent_name in research_store and not force:
            existing = research_store[agent_name]
            self._log_research_step(
                agent_name,
                event_id,
                "cached",
                result=existing if isinstance(existing, dict) else None,
            )
            # Kein zusätzlicher Step-Recorder-Eintrag → bereits geloggt
            return existing

        trigger = self._build_research_trigger(event, info, event_id)
        attributes = {"event.id": str(event_id)} if event_id is not None else None
        with observe_operation(agent_name, attributes):
            try:
                async with concurrency.RESEARCH_TASK_SEMAPHORE:
                    result = await agent.run(trigger)
            except Exception as exc:  # pragma: no cover
                logger.exception(
                    "%s research agent failed for event %s", agent_name, event_id
                )
                event_result.setdefault("research_errors", []).append(
                    {"agent": agent_name, "error": str(exc)}
                )
                self._log_research_step(
                    agent_name,
                    event_id,
                    "error",
                    error=str(exc),
                )
                return research_store.get(agent_name)

        research_store[agent_name] = result
        self._log_research_step(
            agent_name,
            event_id,
            "completed",
            result=result if isinstance(result, dict) else None,
        )

        # Zentrale Step-Namensgebung:
        step_map = {
            "internal_research": "research.internal_research",
            "dossier_research": "research.dossier_research",
            "similar_companies": "research.similar_companies",
            "similar_companies_level1": "research.similar_companies",
        }
        step_name = step_map.get(agent_name)
        if step_name:
            workflow_step_recorder.record_step(self.run_id, event_id, step_name)

        return result

    async def _run_internal_research(
        self,
        event_result: Dict[str, Any],
        event: Dict[str, Any],
        info: Dict[str, Any],
        event_id: Optional[Any],
        *,
        force: bool,
    ) -> Optional[Dict[str, Any]]:
        if not self.internal_research_agent:
            self._log_research_step(
                "internal_research",
                event_id,
                "skipped",
                details={"reason": "agent_unavailable"},
            )
            return event_result.get("research", {}).get("internal_research")

        if not self._has_research_inputs(info):
            self._log_research_step(
                "internal_research",
                event_id,
                "skipped",
                details={"reason": "missing_inputs"},
            )
            return event_result.get("research", {}).get("internal_research")

        return await self._run_research_agent(
            self.internal_research_agent,
            "internal_research",
            event_result,
            event,
            info,
            event_id,
            force=force,
        )

    def _extract_internal_status(
        self, result: Optional[Dict[str, Any]]
    ) -> Optional[str]:
        if not isinstance(result, dict):
            return None
        status = result.get("status")
        if isinstance(status, str) and status:
            return status
        payload = result.get("payload")
        if isinstance(payload, dict):
            action = payload.get("action")
            if isinstance(action, str) and action:
                return action
        return None

    async def _execute_precrm_research(
        self,
        event_result: Dict[str, Any],
        event: Dict[str, Any],
        info: Dict[str, Any],
        event_id: Optional[Any],
        *,
        requires_dossier: bool,
    ) -> None:
        runners = []

        if requires_dossier:
            if self._can_run_dossier(info):

                async def run_dossier() -> None:
                    await self._run_research_agent(
                        self.dossier_research_agent,
                        "dossier_research",
                        event_result,
                        event,
                        info,
                        event_id,
                        force=True,
                    )

                runners.append(run_dossier)
            else:
                self._log_research_step(
                    "dossier_research",
                    event_id,
                    "skipped",
                    details={"reason": "missing_inputs"},
                )

        if self._can_run_similar(info):

            async def run_similar() -> None:
                await self._run_research_agent(
                    self.similar_companies_agent,
                    "similar_companies",
                    event_result,
                    event,
                    info,
                    event_id,
                    force=True,
                )

            runners.append(run_similar)
        else:
            self._log_research_step(
                "similar_companies",
                event_id,
                "skipped",
                details={"reason": "missing_inputs"},
            )

        if not runners:
            return

        if len(runners) == 1:
            await runners[0]()
            return

        await concurrency.run_in_task_group(runners)

    def _can_run_dossier(self, info: Dict[str, Any]) -> bool:
        return bool(info.get("company_name")) and bool(info.get("company_domain"))

    def _can_run_similar(self, info: Dict[str, Any]) -> bool:
        return bool(info.get("company_name"))

    async def _process_crm_dispatch(
        self,
        event: Dict[str, Any],
        info: Dict[str, Any],
        event_result: Dict[str, Any],
        event_id: Optional[Any],
        *,
        force_internal: bool,
    ) -> None:
        prepared_info = self._normalise_info_for_research(info)
        if not self._has_research_inputs(prepared_info):
            event_result["status"] = "missing_research_inputs"
            return

        internal_result = await self._run_internal_research(
            event_result,
            event,
            prepared_info,
            event_id,
            force=force_internal,
        )
        internal_status = self._extract_internal_status(internal_result)
        if internal_status == "AWAIT_REQUESTOR_DETAILS":
            event_result["status"] = "awaiting_requestor_details"
            return
        if internal_status == "AWAIT_REQUESTOR_DECISION":
            event_result["status"] = "awaiting_requestor_decision"
            return

        requires_dossier = internal_status in (None, "REPORT_REQUIRED")
        await self._execute_precrm_research(
            event_result,
            event,
            prepared_info,
            event_id,
            requires_dossier=requires_dossier,
        )

        crm_payload = dict(prepared_info)
        if event_result.get("research"):
            crm_payload["research"] = event_result["research"]

        with observe_operation(
            "crm_dispatch", {"event.id": str(event_id)} if event_id else None
        ):
            await self._send_to_crm_agent(event, crm_payload)

        # Steps auf CRM-Pfad:
        workflow_step_recorder.record_step(
            self.run_id, event_id, "crm_matching_recorded"
        )
        if internal_status in (None, "REPORT_REQUIRED"):
            workflow_step_recorder.record_step(self.run_id, event_id, "report_required")
        workflow_step_recorder.record_step(self.run_id, event_id, "completed")

        event_result["status"] = "dispatched_to_crm"
        event_result["crm_dispatched"] = True
        event_result["crm_payload"] = crm_payload

    def finalize_run_logs(self) -> None:
        log_size = 0
        if self.log_file_path.exists():
            log_size = self.log_file_path.stat().st_size

        audit_path = self.storage_agent.get_audit_log_path(self.run_id)
        audit_entries = []
        if audit_path.exists() and self.audit_log is not None:
            try:
                audit_entries = self.audit_log.load_entries()
            except Exception:
                logger.warning(
                    "Unable to read audit log at %s during finalization", audit_path
                )

        metadata = {
            "log_size_bytes": log_size,
            "audit_log_path": audit_path.as_posix(),
            "audit_entry_count": len(audit_entries),
        }

        self.storage_agent.record_run(
            self.run_id,
            self.log_file_path,
            metadata=metadata,
        )

        if self._negative_cache:
            self._negative_cache.flush()

        if hasattr(self.human_agent, "shutdown"):
            try:
                self.human_agent.shutdown()
            except Exception:
                logger.exception("Failed to shutdown human agent reminders")

        if hasattr(self, "_config_watcher"):
            self._config_watcher.stop()

    def _log_research_step(
        self,
        agent_name: str,
        event_id: Optional[Any],
        outcome: str,
        *,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.workflow_log_manager or not self.run_id:
            return

        data: Dict[str, Any] = {"stage": agent_name, "outcome": outcome}

        if event_id is not None:
            data["event_id"] = str(event_id)

        if details:
            data["details"] = details

        if result:
            status = result.get("status")
            if status:
                data["status"] = status

            source = result.get("source")
            if source:
                data["source"] = source

            payload = result.get("payload")
            artifacts: Dict[str, str] = {}
            if isinstance(result.get("artifact_path"), str):
                artifacts.setdefault("primary", str(result["artifact_path"]))

            if isinstance(payload, dict):
                decision = payload.get("action") or payload.get("status")
                if decision:
                    data["decision"] = decision

                payload_artifact = payload.get("artifact_path")
                if isinstance(payload_artifact, str):
                    artifacts.setdefault("primary", payload_artifact)

                payload_artifacts = payload.get("artifacts")
                if isinstance(payload_artifacts, dict):
                    for key, value in payload_artifacts.items():
                        if isinstance(value, str):
                            artifacts[key] = value

                results = payload.get("results")
                if isinstance(results, list):
                    data["result_count"] = len(results)

            if artifacts:
                data["artifacts"] = artifacts

        if error:
            data["error"] = error

        message = json.dumps(data, ensure_ascii=False, sort_keys=True)
        try:
            self.workflow_log_manager.append_log(
                self.run_id,
                f"research.{agent_name}",
                message,
                event_id=str(event_id) if event_id is not None else None,
                error=error,
            )
        except Exception:
            logger.exception(
                "Failed to append research workflow log for %s", agent_name
            )

    async def continue_after_missing_info(
        self,
        *,
        audit_id: str,
        fields: Dict[str, Any],
        context: Dict[str, Any],
    ) -> None:
        event = context.get("event") or {}
        base_info = context.get("info") or {}
        merged = dict(base_info)
        merged.update({k: v for k, v in (fields or {}).items() if v})
        prepared = self._normalise_info_for_research(merged)
        event_id = context.get("event_id")

        if not self._has_research_inputs(prepared):
            extracted = {
                "info": merged,
                "is_complete": False,
                "status": "incomplete",
                "missing": [
                    key for key in ("company_name", "web_domain") if not merged.get(key)
                ],
            }
            filled = self.human_agent.request_info(event, extracted)
            new_audit_id = filled.get("audit_id")
            if filled.get("status") == "pending" and new_audit_id and callable(
                self.on_pending_audit
            ):
                next_context = dict(context)
                next_context["info"] = merged
                try:
                    self.on_pending_audit("missing_info", new_audit_id, next_context)
                except Exception:
                    logger.exception(
                        "on_pending_audit callback failed for follow-up missing_info"
                    )
            return

        event_result = {"event_id": event_id, "status": "continuation"}
        await self._process_crm_dispatch(
            event,
            prepared,
            event_result,
            event_id,
            force_internal=True,
        )

    async def continue_after_dossier_decision(
        self,
        *,
        audit_id: str,
        decision: Optional[str],
        context: Dict[str, Any],
    ) -> None:
        event = context.get("event") or {}
        info = context.get("info") or {}
        event_id = context.get("event_id")
        prepared = self._normalise_info_for_research(info)
        is_complete = self._has_research_inputs(prepared)

        if decision == "approved":
            if is_complete:
                event_result = {"event_id": event_id, "status": "continuation"}
                await self._process_crm_dispatch(
                    event,
                    prepared,
                    event_result,
                    event_id,
                    force_internal=False,
                )
            else:
                extracted = {
                    "info": info,
                    "is_complete": False,
                    "status": "incomplete",
                    "missing": [
                        key for key in ("company_name", "web_domain") if not info.get(key)
                    ],
                }
                filled = self.human_agent.request_info(event, extracted)
                new_audit_id = filled.get("audit_id")
                if filled.get("status") == "pending" and new_audit_id and callable(
                    self.on_pending_audit
                ):
                    next_context = dict(context)
                    self.on_pending_audit("missing_info", new_audit_id, next_context)
            return

        if decision == "declined":
            logger.info("Dossier declined for event %s", event_id)
            return

        logger.info(
            "No actionable dossier decision for audit_id=%s event=%s", audit_id, event_id
        )

    def _resolve_dossier_status(self, response: Dict[str, Any]) -> str:
        status = response.get("status")
        if status:
            return str(status)
        decision = response.get("dossier_required")
        if decision is True:
            return "approved"
        if decision is False:
            return "declined"
        return "pending"

    def _log_dossier_pending(
        self,
        event_id: Optional[Any],
        audit_id: Optional[str],
        response: Dict[str, Any],
    ) -> None:
        logger.info(
            "Organizer decision pending for event %s [audit_id=%s]",
            event_id,
            audit_id or "n/a",
        )
        details = self._mask_for_logging(response.get("details"))
        if details:
            logger.info(
                "Pending dossier reminder context for event %s: %s",
                event_id,
                details,
            )

    async def aclose(self) -> None:
        async def _close_agent(name: str, agent: Optional[Any]) -> None:
            if agent is None:
                return
            closer = getattr(agent, "aclose", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:
                    logger.exception("Failed to close %s agent", name)

        agents_to_close = {
            "polling": getattr(self, "event_agent", None),
            "trigger": getattr(self, "trigger_agent", None),
            "extraction": getattr(self, "extraction_agent", None),
            "human": getattr(self, "human_agent", None),
            "crm": getattr(self, "crm_agent", None),
            "internal_research": getattr(self, "internal_research_agent", None),
            "dossier_research": getattr(self, "dossier_research_agent", None),
            "similar_companies": getattr(self, "similar_companies_agent", None),
        }

        await asyncio.gather(
            *(
                _close_agent(name, agent)
                for name, agent in agents_to_close.items()
                if agent is not None
            ),
            return_exceptions=True,
        )

        watcher = getattr(self, "_config_watcher", None)
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:
                logger.exception("Failed to stop configuration watcher")

    def _apply_llm_settings(self, current_settings) -> None:
        self.llm_confidence_thresholds = dict(
            current_settings.llm_confidence_thresholds
        )
        self.llm_cost_caps = dict(current_settings.llm_cost_caps)
        self.llm_retry_budgets = dict(current_settings.llm_retry_budgets)
        logger.debug(
            "Updated LLM thresholds: confidence=%s cost_caps=%s retry_budgets=%s",
            self.llm_confidence_thresholds,
            self.llm_cost_caps,
            self.llm_retry_budgets,
        )

    def _meets_confidence_threshold(self, key: str, payload: Dict[str, Any]) -> bool:
        threshold = self.llm_confidence_thresholds.get(key)
        if threshold is None:
            return True

        confidence = payload.get("confidence")
        if confidence is None:
            return True

        try:
            return float(confidence) >= float(threshold)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid confidence value '%s' for key '%s'; allowing by default.",
                confidence,
                key,
            )
            return True

    def _mask_for_logging(self, payload: Any) -> Any:
        if not getattr(settings, "mask_pii_in_logs", False):
            return payload
        return mask_pii(
            payload,
            whitelist=getattr(settings, "pii_field_whitelist", None),
            mode=getattr(settings, "compliance_mode", "standard"),
        )
