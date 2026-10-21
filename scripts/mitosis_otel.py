"""Opt-in OpenTelemetry GenAI spans for Mitosis routing decisions.

Stdlib only, Python 3.8 compatible. Nothing happens unless MITOSIS_OTEL=1
(or CLAUDE_/CODEX_/AGY_ prefixed MITOSIS_OTEL=1) AND either MITOSIS_OTEL_FILE
or an OTLP endpoint is configured:

    MITOSIS_OTEL=1
    MITOSIS_OTEL_FILE=<path>                    append one OTLP JSON body per line
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=<url>    used as-is
    OTEL_EXPORTER_OTLP_ENDPOINT=<url>           "/v1/traces" is appended
    OTEL_EXPORTER_OTLP_HEADERS=k=v,k2=v2        values are URL-decoded
    MITOSIS_OTEL_TIMEOUT=<seconds>              HTTP timeout, default 0.5

Each routing decision becomes one `invoke_agent {agent_name}` span. The span
marks the moment of the routing decision: start and end are the same instant,
because the subagent itself runs later inside the host CLI. Every failure is
swallowed; telemetry must never affect the hook or the MCP server.
"""

import hashlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List

SCOPE_NAME = "mitosis-orchestrator"
_PROVIDERS = {
    "claude": "anthropic",
    "codex": "openai",
    "codex-mcp": "openai",
    "agy": "gcp.gemini",
}

_LOCK = threading.Lock()
_QUEUE: List[Dict[str, Any]] = []


def _flag() -> bool:
    for name in ("MITOSIS_OTEL", "CLAUDE_MITOSIS_OTEL", "CODEX_MITOSIS_OTEL", "AGY_MITOSIS_OTEL"):
        val = os.environ.get(name, "").strip()
        if val:
            return val == "1"
    return False


def _endpoint() -> str:
    traces = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if traces:
        return traces
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    return base.rstrip("/") + "/v1/traces" if base else ""


def _out_file() -> str:
    return os.environ.get("MITOSIS_OTEL_FILE", "").strip()


def enabled() -> bool:
    return _flag() and bool(_out_file() or _endpoint())


def _headers() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").split(","):
        if "=" in part:
            key, val = part.split("=", 1)
            if key.strip():
                out[key.strip()] = urllib.parse.unquote(val.strip())
    return out


def _timeout() -> float:
    try:
        return float(os.environ.get("MITOSIS_OTEL_TIMEOUT", "0.5"))
    except ValueError:
        return 0.5


def _attr(key: str, value: Any) -> Dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def build_span(span: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a routing record into an OTLP span dict.

    Keys: agent_name, platform, tier, rule, confidence, source, model, effort, session_id.
    """
    now = str(time.time_ns()) if hasattr(time, "time_ns") else str(int(time.time() * 1e9))
    session = str(span.get("session_id") or "")
    platform = str(span.get("platform") or "")
    agent = str(span.get("agent_name") or "subagent")
    attrs = [
        _attr("gen_ai.operation.name", "invoke_agent"),
        _attr("gen_ai.provider.name", _PROVIDERS.get(platform, platform or "unknown")),
        _attr("gen_ai.agent.name", agent),
    ]
    if span.get("model"):
        attrs.append(_attr("gen_ai.request.model", span["model"]))
    if span.get("session_id"):
        attrs.append(_attr("gen_ai.conversation.id", session))
    attrs.append(_attr("mitosis.tier", span.get("tier") or ""))
    attrs.append(_attr("mitosis.rule", span.get("rule") or ""))
    attrs.append(_attr("mitosis.confidence", float(span.get("confidence") or 0.0)))
    attrs.append(_attr("mitosis.source", span.get("source") or ""))
    attrs.append(_attr("mitosis.platform", platform))
    if span.get("effort"):
        attrs.append(_attr("mitosis.effort", span["effort"]))
    trace_seed = session or span.get("fallback_trace_seed") or "mitosis-%d" % os.getpid()
    return {
        "traceId": hashlib.sha256(str(trace_seed).encode("utf-8")).hexdigest()[:32],
        "spanId": os.urandom(8).hex(),
        "name": "invoke_agent " + agent,
        "kind": 1,
        "startTimeUnixNano": now,
        "endTimeUnixNano": now,
        "attributes": attrs,
    }


def queue(span: Dict[str, Any]) -> None:
    """Queue a routing record (see build_span) for the next flush()."""
    try:
        built = build_span(span)
        with _LOCK:
            _QUEUE.append(built)
            del _QUEUE[:-256]
    except Exception:
        pass


def _body(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "resourceSpans": [{
            "resource": {"attributes": [_attr("service.name", "mitosis-orchestrator")]},
            "scopeSpans": [{"scope": {"name": SCOPE_NAME}, "spans": spans}],
        }]
    }


def flush() -> None:
    """Send every queued span in one request and/or one file line. Never raises."""
    try:
        with _LOCK:
            spans = list(_QUEUE)
            del _QUEUE[:]
        if not spans or not enabled():
            return
        data = json.dumps(_body(spans), separators=(",", ":")).encode("utf-8")
        path = _out_file()
        if path:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
                try:
                    os.write(fd, data + b"\n")
                finally:
                    os.close(fd)
            except Exception:
                pass
        url = _endpoint()
        if url:
            headers = {"Content-Type": "application/json"}
            headers.update(_headers())
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=_timeout()) as resp:
                    resp.read()
            except Exception:
                pass
    except Exception:
        pass
