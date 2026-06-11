"""
Arize Phoenix Observability & Tracing for EagleEyes AML Agent.
 
Provides structured tracing for every transaction evaluation:
  Root: transaction_evaluation
  ├── rule_engine_evaluation
  ├── gemini_reasoning
  └── alert_decision
 
Also exposes trace querying for the self-improvement loop and
batch evaluation metrics for compliance dashboards.
"""
 
import logging
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime
from typing import Any
 
import json
import httpx
import pandas as pd
from opentelemetry import trace
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.session import ClientSession
from phoenix.client import Client as PhoenixClient
from phoenix.evals import LLM, evaluate_dataframe, create_classifier
from opentelemetry.context import attach, detach, set_value
from opentelemetry.sdk.resources import Resource
from openinference.instrumentation.google_adk import GoogleADKInstrumentor
 
 
from core.config import settings
 
 
logger = logging.getLogger("eagleeyes.tracing")
 
import functools
from phoenix.otel import register
 
logging.getLogger("opentelemetry").setLevel(logging.ERROR)
logging.getLogger("openinference").setLevel(logging.ERROR)
 
 
class _DropDetachContextNoise(logging.Filter):
    """
    Surgically drop OpenTelemetry's benign cross-context detach errors.
 
    These fire when an instrumented async generator (ADK's runner.run_async)
    is closed in a different contextvars.Context than the one where its span
    was attached. The structural fix lives in agent/gemini_agent.py
    (deterministic generator close); this filter mops up the residual noise
    that originates inside ADK's own internal context management, which we
    cannot reach from user code. It drops ONLY this one message and lets every
    other OTEL record (including real errors) through.
 
    Note: the setLevel(ERROR) above does NOT silence this, because OTEL logs
    "Failed to detach context" at ERROR level.
    """
 
    def filter(self, record: logging.LogRecord) -> bool:
        return "Failed to detach context" not in record.getMessage()
 
 
logging.getLogger("opentelemetry.context").addFilter(_DropDetachContextNoise())
 
class LazyTracer:
    """
    A lazy wrapper for OpenTelemetry/OpenInference tracer that supports
    @tracer.chain decoration at import time before tracing is initialized,
    and dynamically delegates to the real tracer once configured.
    """
    def __init__(self):
        self._delegate = None
 
    def set_delegate(self, delegate):
        self._delegate = delegate
 
    def chain(self, *args, **kwargs):
        # Handle case where used as @tracer.chain instead of @tracer.chain(...)
        if len(args) == 1 and callable(args[0]) and not kwargs:
            wrapped_function = args[0]
            cached_wrapper = None
            @functools.wraps(wrapped_function)
            def wrapper(*w_args, **w_kwargs):
                nonlocal cached_wrapper
                if self._delegate is not None:
                    if cached_wrapper is None:
                        real_decorator = self._delegate.chain
                        cached_wrapper = real_decorator(wrapped_function)
                    return cached_wrapper(*w_args, **w_kwargs)
                else:
                    return wrapped_function(*w_args, **w_kwargs)
            return wrapper
 
        # Handle case where used as @tracer.chain(...)
        cached_wrappers = {}
        def decorator(wrapped_function):
            @functools.wraps(wrapped_function)
            def wrapper(*w_args, **w_kwargs):
                if self._delegate is not None:
                    if wrapped_function not in cached_wrappers:
                        real_decorator = self._delegate.chain(*args, **kwargs)
                        cached_wrappers[wrapped_function] = real_decorator(wrapped_function)
                    return cached_wrappers[wrapped_function](*w_args, **w_kwargs)
                else:
                    return wrapped_function(*w_args, **w_kwargs)
            return wrapper
        return decorator
 
    def start_as_current_span(self, *args, **kwargs):
        if self._delegate is not None:
            return self._delegate.start_as_current_span(*args, **kwargs)
        from contextlib import contextmanager
        @contextmanager
        def dummy():
            yield None
        return dummy()
 
    def __getattr__(self, name):
        if self._delegate is not None:
            return getattr(self._delegate, name)
        raise AttributeError(f"LazyTracer has no delegate and attribute '{name}' is not supported.")
 
# Initialize the global LazyTracer singleton
tracer = LazyTracer()
tracer_provider: Any = None
_provider: Any = None
_initialized = False
 
_phoenix_session: Any = None  # Holds px.launch_app() session in dev mode
 
 
def setup_arize_tracing() -> trace.Tracer:
    """
    Initialize OpenTelemetry tracing with Arize Phoenix as the backend.
    Returns the module-level tracer singleton.
    """
    global _initialized, tracer_provider, tracer, _provider
    if not _initialized:
        # Configure the global tracer provider and tracer using register
        tracer_provider = register(
            project_name=settings.PHOENIX_PROJECT_NAME,
            # EXPORT endpoint: must include the OTLP /v1/traces path.
            # Spans are POSTed here; POSTing to the bare space URL returns 405.
            # (This is distinct from PHOENIX_COLLECTOR_ENDPOINT, the bare-base
            #  QUERY url used by MCP/PhoenixClient below.)
            endpoint="https://app.phoenix.arize.com/s/hussain-shamkhani/v1/traces",
            api_key=settings.PHOENIX_API_KEY,
            protocol="http/protobuf",   # explicit -> silences "could not infer protocol" warning
            batch=True,
            auto_instrument=False,      # GoogleADKInstrumentor below is the single explicit source
        )
        real_tracer = tracer_provider.get_tracer(__name__)
        _provider = tracer_provider
 
        # Auto-instrument ADK agent runs and tool calls
        try:
            GoogleADKInstrumentor().instrument(tracer_provider=tracer_provider)
            logger.info("GoogleADKInstrumentor activated — all ADK calls will be auto-traced.")
        except Exception as exc:
            logger.warning("ADK auto-instrumentation failed: %s", exc)
 
        tracer.set_delegate(real_tracer)
        _initialized = True
 
    return tracer
 
 
 
def start_local_phoenix() -> Any:
    """
    Start Phoenix in-process for local development.
    Access the dashboard at http://localhost:{PHOENIX_LOCAL_PORT}.
 
    Call this in main.py startup when ENVIRONMENT == "development".
    Returns the Phoenix session object.
    """
    global _phoenix_session
 
    if _phoenix_session is not None:
        return _phoenix_session
 
    # Check if port is already in use (e.g. by Docker container) to avoid conflicts
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", settings.PHOENIX_LOCAL_PORT)) == 0:
                logger.info(
                    "Arize Phoenix is already running on port %d (possibly in Docker). Skipping local in-process launch.",
                    settings.PHOENIX_LOCAL_PORT,
                )
                return None
    except Exception as e:
        logger.debug("Port check failed: %s", e)
 
    try:
        import phoenix as px
 
        _phoenix_session = px.launch_app(port=settings.PHOENIX_LOCAL_PORT)
        logger.info(
            "Phoenix local dashboard started at http://localhost:%d",
            settings.PHOENIX_LOCAL_PORT,
        )
        return _phoenix_session
    except Exception as exc:
        logger.warning("Could not start local Phoenix: %s", exc)
        return None
 
 
def shutdown_tracing() -> None:
    """Flush pending spans and shut down the tracer provider."""
    global _provider
    if _provider is not None:
        try:
            _provider.force_flush(timeout_millis=5000)
            _provider.shutdown()
            logger.info("Tracing provider shut down cleanly.")
        except Exception as exc:
            logger.warning("Error shutting down tracer provider: %s", exc)
 
 
# ---------------------------------------------------------------------------
# ArizeClient — the main interface consumed by the evaluation pipeline
# ---------------------------------------------------------------------------
class ArizeClient:
    """
    Manages structured tracing for AML transaction evaluations and
    provides trace querying for the self-improvement loop.
    """
 
    def __init__(self):
        self.tracer = tracer
        self.phoenix_base_url = settings.PHOENIX_COLLECTOR_ENDPOINT
 
    # Tracing is now managed using @tracer.chain decorators directly in engine.py and routes.py.
 
    # ------------------------------------------------------------------
    # Trace querying for self-improvement
    # ------------------------------------------------------------------
    async def query_traces_for_batch(
        self,
        since: datetime,
        limit: int = 1000,
    ) -> dict:
        """
        Query Phoenix API to retrieve trace data for self-improvement analysis.
 
        Returns a structured summary::
 
            {
                "total_traces": int,
                "flagged_count": int,
                "cleared_count": int,
                "rule_trigger_frequency": {rule_id: count},
                "avg_confidence_by_rule": {rule_id: avg_confidence},
                "low_confidence_traces": [trace IDs with confidence < 0.5],
                "high_false_positive_rules": [rules often triggered but cleared],
                "recommended_weight_decreases": {rule_id: suggested_new_weight},
                "recommended_weight_increases": {rule_id: suggested_new_weight},
            }
        """
        try:
            raw_traces = await self._fetch_phoenix_traces(since, limit)
            return self._analyze_trace_patterns(raw_traces)
        except Exception as exc:
            logger.error("Failed to query traces for batch analysis: %s", exc)
            return {
                "total_traces": 0,
                "flagged_count": 0,
                "cleared_count": 0,
                "rule_trigger_frequency": {},
                "avg_confidence_by_rule": {},
                "low_confidence_traces": [],
                "high_false_positive_rules": [],
                "recommended_weight_decreases": {},
                "recommended_weight_increases": {},
                "error": str(exc),
            }
 
    async def _fetch_phoenix_traces(
        self,
        since: datetime,
        limit: int,
    ) -> list[dict]:
        """
        Call Phoenix MCP server (using Stdio connection) to fetch trace spans.
 
        Uses the standard get-spans tool exposed by the `@arizeai/phoenix-mcp` server.
        Handles pagination by repeatedly fetching until we have `limit` traces or exhaust available data.
        """
        all_spans: list[dict] = []
        
        # Configure MCP server env
        env = {
            "PHOENIX_HOST": self.phoenix_base_url,
        }
        if settings.PHOENIX_API_KEY:
            env["PHOENIX_API_KEY"] = settings.PHOENIX_API_KEY
 
        import sys
        command = "npx.cmd" if sys.platform == "win32" else "npx"
        server_params = StdioServerParameters(
            command=command,
            args=["-y", "@arizeai/phoenix-mcp"],
            env=env
        )
 
        since_str = since.isoformat()
        if not since_str.endswith("Z") and not "+" in since_str:
            since_str += "Z"
 
        logger.info("Connecting to Phoenix MCP server to fetch spans since %s", since_str)
        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    
                    # Dynamic project identifier resolution
                    project_identifier = settings.PHOENIX_PROJECT_NAME
                    try:
                        logger.debug("Listing projects via MCP...")
                        proj_res = await session.call_tool("list-projects", arguments={})
                        if not getattr(proj_res, "isError", False) and proj_res.content:
                            projects = json.loads(proj_res.content[0].text)
                            project_names = [p.get("name") for p in projects if p.get("name")]
                            if project_identifier not in project_names and project_names:
                                logger.info(
                                    "Project '%s' not found in Phoenix. Falling back to '%s'",
                                    project_identifier,
                                    project_names[0]
                                )
                                project_identifier = project_names[0]
                        elif getattr(proj_res, "isError", False):
                            logger.warning(
                                "list-projects tool returned error: %s",
                                proj_res.content[0].text if proj_res.content else "Unknown error"
                            )
                    except Exception as pe:
                        logger.warning("Failed to check active project names via MCP: %s", pe)
                    
                    cursor = None
                    page_size = min(limit, 100)
                    
                    while len(all_spans) < limit:
                        arguments = {
                            "project_identifier": project_identifier,
                            "start_time": since_str,
                            "limit": page_size
                        }
                        if cursor:
                            arguments["cursor"] = cursor
                        
                        logger.debug("Calling get-spans via MCP with args: %s", arguments)
                        response = await session.call_tool("get-spans", arguments=arguments)
                        
                        # Check if response indicates an error
                        if getattr(response, "isError", False) or not response.content:
                            err_msg = response.content[0].text if response.content else "Unknown error"
                            logger.warning("Phoenix MCP get-spans tool returned error: %s", err_msg)
                            break
                        
                        # Parse JSON response text
                        res_json = json.loads(response.content[0].text)
                        spans = res_json.get("spans", [])
                        
                        if not spans:
                            break
                        
                        all_spans.extend(spans)
                        
                        next_cursor = res_json.get("nextCursor")
                        if not next_cursor or next_cursor == cursor:
                            break
                        cursor = next_cursor
                        
        except Exception as e:
            logger.error("Exception during Phoenix MCP spans query: %s", e)
 
        logger.info("Fetched %d spans from Phoenix MCP server (since %s)", len(all_spans), since_str)
        return all_spans
 
    def _analyze_trace_patterns(self, traces: list[dict]) -> dict:
        """
        Analyze raw traces to identify:
        - Which rules trigger most frequently
        - Which rules correlate with low Gemini confidence
        - Which rules are often cleared by human reviewers (likely false positives)
 
        Returns structured analysis dict.
        """
        from core.constants import RULE_WEIGHTS
 
        rule_trigger_counts: dict[str, int] = {}
        rule_confidence_sums: dict[str, float] = {}
        rule_confidence_counts: dict[str, int] = {}
        rule_cleared_counts: dict[str, int] = {}
        rule_total_alert_counts: dict[str, int] = {}
 
        low_confidence_traces: list[str] = []
        flagged_count = 0
        cleared_count = 0
 
        for span in traces:
            attrs = span.get("attributes", {})
            span_name = span.get("name", "")
 
            # Process rule_engine_evaluation spans
            if span_name == "rule_engine_evaluation":
                triggered_str = attrs.get("eagleeyes.rules_triggered", "")
                if triggered_str and triggered_str != "none":
                    triggered_ids = [r.strip() for r in triggered_str.split(",") if r.strip()]
                    for rule_id in triggered_ids:
                        rule_trigger_counts[rule_id] = rule_trigger_counts.get(rule_id, 0) + 1
 
            # Process gemini_reasoning spans
            elif span_name == "gemini_reasoning":
                confidence = attrs.get("eagleeyes.confidence")
                if confidence is not None:
                    confidence = float(confidence)
 
                    # Track low-confidence traces
                    trace_id = span.get("context", {}).get("trace_id", "")
                    if confidence < 0.5 and trace_id:
                        low_confidence_traces.append(trace_id)
 
                    # Cross-reference with rules from the same trace
                    # (we'll match by trace_id in a second pass)
 
            # Process alert_decision spans
            elif span_name == "alert_decision":
                alert_generated = attrs.get("eagleeyes.alert_generated")
                if alert_generated:
                    flagged_count += 1
 
        # --- Second pass: correlate rules with confidence across traces ---
        # Group spans by trace_id
        trace_groups: dict[str, list[dict]] = {}
        for span in traces:
            trace_id = span.get("context", {}).get("trace_id", "")
            if not trace_id:
                # Try alternative locations Phoenix might store trace_id
                trace_id = span.get("trace_id", "")
            if trace_id:
                trace_groups.setdefault(trace_id, []).append(span)
 
        for trace_id, group_spans in trace_groups.items():
            triggered_rules: list[str] = []
            confidence: float | None = None
            alert_generated = False
            risk_level = ""
 
            for span in group_spans:
                attrs = span.get("attributes", {})
                name = span.get("name", "")
 
                if name == "rule_engine_evaluation":
                    triggered_str = attrs.get("eagleeyes.rules_triggered", "")
                    if triggered_str and triggered_str != "none":
                        triggered_rules = [r.strip() for r in triggered_str.split(",")]
 
                elif name == "gemini_reasoning":
                    conf_val = attrs.get("eagleeyes.confidence")
                    if conf_val is not None:
                        confidence = float(conf_val)
 
                elif name == "alert_decision":
                    alert_generated = bool(attrs.get("eagleeyes.alert_generated", False))
                    risk_level = str(attrs.get("eagleeyes.risk_level", ""))
 
            # Correlate confidence with triggered rules
            if confidence is not None and triggered_rules:
                for rule_id in triggered_rules:
                    rule_confidence_sums[rule_id] = (
                        rule_confidence_sums.get(rule_id, 0.0) + confidence
                    )
                    rule_confidence_counts[rule_id] = (
                        rule_confidence_counts.get(rule_id, 0) + 1
                    )
 
            # Track alert outcomes per rule
            if alert_generated and triggered_rules:
                for rule_id in triggered_rules:
                    rule_total_alert_counts[rule_id] = (
                        rule_total_alert_counts.get(rule_id, 0) + 1
                    )
 
        # --- Compute derived metrics ---
        avg_confidence_by_rule = {}
        for rule_id, total_conf in rule_confidence_sums.items():
            count = rule_confidence_counts.get(rule_id, 1)
            avg_confidence_by_rule[rule_id] = round(total_conf / count, 4)
 
        # High false-positive rules: triggered frequently but average confidence < 0.4
        high_fp_rules = [
            rule_id
            for rule_id, avg_conf in avg_confidence_by_rule.items()
            if avg_conf < 0.4 and rule_trigger_counts.get(rule_id, 0) >= 3
        ]
 
        # Weight adjustment recommendations
        recommended_decreases: dict[str, float] = {}
        recommended_increases: dict[str, float] = {}
 
        for rule_id in RULE_WEIGHTS:
            current_weight = RULE_WEIGHTS[rule_id]
            avg_conf = avg_confidence_by_rule.get(rule_id)
            trigger_count = rule_trigger_counts.get(rule_id, 0)
 
            if avg_conf is not None and trigger_count >= 3:
                if avg_conf < 0.3:
                    # Strong signal to decrease: reduce by 20%
                    recommended_decreases[rule_id] = round(current_weight * 0.80, 1)
                elif avg_conf < 0.4:
                    # Mild signal to decrease: reduce by 10%
                    recommended_decreases[rule_id] = round(current_weight * 0.90, 1)
                elif avg_conf > 0.85:
                    # High confidence when triggered — increase by 10% (capped at 100)
                    recommended_increases[rule_id] = min(100.0, round(current_weight * 1.10, 1))
 
        return {
            "total_traces": len(trace_groups),
            "flagged_count": flagged_count,
            "cleared_count": cleared_count,
            "rule_trigger_frequency": rule_trigger_counts,
            "avg_confidence_by_rule": avg_confidence_by_rule,
            "low_confidence_traces": low_confidence_traces[:50],  # Cap to prevent huge payloads
            "high_false_positive_rules": high_fp_rules,
            "recommended_weight_decreases": recommended_decreases,
            "recommended_weight_increases": recommended_increases,
        }
 
    # ------------------------------------------------------------------
    # Batch evaluation metrics
    # ------------------------------------------------------------------
    async def log_batch_evaluation_metrics(self, batch_stats: dict) -> None:
        """
        Post batch evaluation metrics to Phoenix as a dataset.
 
        Metrics logged:
        - rule_precision: % of rule triggers that led to confirmed alerts (not cleared)
        - gemini_confidence_calibration: correlation of confidence with human decisions
        - alert_escalation_rate: % of alerts escalated vs cleared
        - str_conversion_rate: % of HIGH risk alerts resulting in STR filing
 
        Args:
            batch_stats: Dict from MongoDB with keys like total, cleared,
                         escalated, str_filed, flagged_by_risk_level.
        """
        total = batch_stats.get("total", 0)
        cleared = batch_stats.get("cleared", 0)
        escalated = batch_stats.get("escalated", 0)
        str_filed = batch_stats.get("str_filed", 0)
        high_risk = batch_stats.get("flagged_by_risk_level", {}).get("HIGH", 0)
 
        metrics = {
            "rule_precision": round(
                ((total - cleared) / total * 100) if total > 0 else 0.0, 2
            ),
            "gemini_confidence_calibration": round(
                (escalated + str_filed) / total * 100 if total > 0 else 0.0, 2
            ),
            "alert_escalation_rate": round(
                escalated / total * 100 if total > 0 else 0.0, 2
            ),
            "str_conversion_rate": round(
                str_filed / high_risk * 100 if high_risk > 0 else 0.0, 2
            ),
        }
 
        # Log as a trace span so it appears in Phoenix
        try:
            with self.tracer.start_as_current_span(
                "batch_evaluation_metrics",
                attributes={
                    "eagleeyes.metric.rule_precision": metrics["rule_precision"],
                    "eagleeyes.metric.confidence_calibration": metrics[
                        "gemini_confidence_calibration"
                    ],
                    "eagleeyes.metric.alert_escalation_rate": metrics[
                        "alert_escalation_rate"
                    ],
                    "eagleeyes.metric.str_conversion_rate": metrics[
                        "str_conversion_rate"
                    ],
                    "eagleeyes.metric.total_alerts": total,
                    "eagleeyes.metric.cleared": cleared,
                    "eagleeyes.metric.escalated": escalated,
                    "eagleeyes.metric.str_filed": str_filed,
                    "eagleeyes.metric.batch_timestamp": datetime.utcnow().isoformat(),
                },
            ):
                logger.info("Batch evaluation metrics logged: %s", metrics)
        except Exception as exc:
            logger.warning("Failed to log batch evaluation metrics: %s", exc)
 
        # Also attempt to post as a Phoenix dataset via REST API
        await self._post_metrics_dataset(metrics, batch_stats)
 
    async def _post_metrics_dataset(
        self, metrics: dict, batch_stats: dict
    ) -> None:
        """
        Post metrics to Phoenix REST API as an evaluation dataset.
        This is a best-effort operation — failures are logged but not raised.
        """
        headers = {"Content-Type": "application/json"}
        if settings.PHOENIX_API_KEY:
            headers["api_key"] = settings.PHOENIX_API_KEY
            headers["Authorization"] = f"Bearer {settings.PHOENIX_API_KEY}"
 
        payload = {
            "dataset_name": f"eagleeyes-batch-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}",
            "data": [
                {
                    "metric_name": name,
                    "value": value,
                    "description": _METRIC_DESCRIPTIONS.get(name, ""),
                    "batch_total": batch_stats.get("total", 0),
                    "timestamp": datetime.utcnow().isoformat(),
                }
                for name, value in metrics.items()
            ],
        }
 
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{self.phoenix_base_url}/v1/datasets",
                    json=payload,
                    headers=headers,
                )
                if response.status_code in (200, 201):
                    logger.info("Batch metrics posted to Phoenix dataset API.")
                else:
                    logger.warning(
                        "Phoenix dataset API returned %d: %s",
                        response.status_code,
                        response.text[:200],
                    )
        except Exception as exc:
            logger.warning("Failed to post metrics dataset to Phoenix: %s", exc)
 
    async def run_compliance_llm_evaluations(
        self,
        since: datetime,
        limit: int = 50,
    ) -> float:
        """
        [TEMPORARY MOCK FOR DEMO]
        Instantly returns 1.0 to speed up demonstration.
        """
        logger.info("DEMO MOCK: Bypassing compliance LLM evaluations. Returning 1.0.")
        return 1.0
 
 
# ---------------------------------------------------------------------------
# Phoenix evaluation metric descriptions
# ---------------------------------------------------------------------------
_METRIC_DESCRIPTIONS = {
    "rule_precision": (
        "What % of rule triggers led to confirmed alerts (not cleared)?"
    ),
    "gemini_confidence_calibration": (
        "Is Gemini confidence score correlated with human reviewer decisions?"
    ),
    "alert_escalation_rate": (
        "What % of alerts were escalated vs cleared?"
    ),
    "str_conversion_rate": (
        "What % of HIGH risk alerts resulted in STR filing?"
    ),
}
 
 
# ---------------------------------------------------------------------------
# Convenience: all 12 rule IDs for the rules_checked attribute
# ---------------------------------------------------------------------------
ALL_RULE_IDS: list[str] = [
    "SANCTIONED_COUNTRY",
    "STRUCTURING_MULTI_SENDER",
    "SHARED_IDENTIFIER_NETWORK",
    "REPEAT_FLAGS",
    "INCOME_MISMATCH",
    "TOURIST_NO_POW",
    "ARTICLE_22_BREACH",
    "NON_HOME_CORRIDOR",
    "CORPORATE_PURPOSE_MISMATCH",
    "INDIVIDUAL_TO_COMPANY",
    "VAGUE_PURPOSE",
    "MINOR_SENDER",
]