#!/usr/bin/env python3
"""Optional judge for low-confidence routing decisions (Python 3.8+, stdlib only).

enforce-orchestrator.py loads this file only when MITOSIS_JUDGE_URL is set.
The endpoint speaks the Jev request format, so TypeSafe Jev, a self-hosted
Jev-compatible gateway and Ollaya all work:

  POST {"model": ..., "state": <prompt>, "questions": {"tier": {"type": "choice", ...}}}

Environment:
  MITOSIS_JUDGE_URL             endpoint; unset means no judge
  MITOSIS_JUDGE_KEY             bearer token (optional for local gateways)
  MITOSIS_JUDGE_MODEL           default "jev-latest"
  MITOSIS_JUDGE_TIMEOUT         seconds, default 1.5
  MITOSIS_JUDGE_MIN_CONFIDENCE  below this the judge counts as unsure, default 0.7

The prompt leaves the machine, so it is cut to MAX_STATE_CHARS and obvious
secrets, addresses and home directories are masked first.
"""

import ipaddress
import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

MAX_STATE_CHARS = 2000
DEFAULT_MODEL = "jev-latest"

INSTRUCTIONS = (
    "A coding agent is about to hand this task to a subagent. "
    "Pick the cheapest subagent tier that can finish it without failing."
)
CRITERIA = {
    "scout": (
        "A bounded read-only lookup a small fast model finishes in a few tool calls: "
        "locate a file or symbol, grep, read one file, check git log or a log tail, report a fact."
    ),
    "worker": (
        "Anything else: edits or creates files, runs tests or builds, debugs, or researches "
        "across many files or web pages and synthesizes the result."
    ),
}
TIER_OF = {"scout": "flash_lite", "worker": "flash"}

_REDACTIONS = [
    (re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abp])[-_][A-Za-z0-9_-]{8,}"), "<secret>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<jwt>"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "<secret>"),
    (re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@"), "<cred>@"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "<hex>"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}"), "<blob>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "<email>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"(?:/home/|/Users/)[^/\s]+"), "~"),
    (re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE), "~"),
]


def redact(text: str) -> str:
    """Mask secrets, e-mail and IP addresses and user home directories."""
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def build_request(prompt: str, model: str = DEFAULT_MODEL) -> Dict[str, Any]:
    return {
        "model": model,
        "state": redact(prompt)[:MAX_STATE_CHARS],
        "questions": {"tier": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": dict(CRITERIA)}},
    }


def parse_response(body: Any) -> Optional[Tuple[str, float]]:
    """Return (tier, confidence) from a Jev, gateway or Ollaya answer, or None."""
    if not isinstance(body, dict):
        return None
    answer = (body.get("answers") or {}).get("tier")
    if not isinstance(answer, dict):
        return None
    probs = answer.get("probabilities")
    if isinstance(probs, list) and len(probs) == len(CRITERIA):
        probs = dict(zip(CRITERIA, probs))
    if isinstance(probs, dict):
        clean = {k: float(v) for k, v in probs.items() if k in TIER_OF and isinstance(v, (int, float))}
        if clean:
            total = sum(clean.values()) or 1.0
            best = max(clean, key=clean.get)
            return TIER_OF[best], clean[best] / total
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    if choice in TIER_OF and isinstance(confidence, (int, float)):
        return TIER_OF[choice], float(confidence)
    return None


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _key_may_travel(parsed: urllib.parse.ParseResult) -> bool:
    """Send the bearer key over https, or over plain http only inside the LAN.

    MITOSIS_JUDGE_ALLOW_HTTP_KEY=1 lifts the restriction for other http hosts.
    """
    if parsed.scheme == "https" or os.environ.get("MITOSIS_JUDGE_ALLOW_HTTP_KEY", "").strip() == "1":
        return True
    host = parsed.hostname or ""
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


def judge_tier(prompt: str, opener: Any = None) -> Optional[Tuple[str, float]]:
    """Ask the configured judge for a tier. None when unset, failing or unsure."""
    url = os.environ.get("MITOSIS_JUDGE_URL", "").strip()
    if not url:
        return None
    payload = build_request(prompt, os.environ.get("MITOSIS_JUDGE_MODEL", "").strip() or DEFAULT_MODEL)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("MITOSIS_JUDGE_KEY", "").strip()
    if key and _key_may_travel(parsed):
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with (opener or urllib.request.urlopen)(request, timeout=_float_env("MITOSIS_JUDGE_TIMEOUT", 1.5)) as resp:
            body = json.load(resp)
    except Exception:
        return None
    verdict = parse_response(body)
    if verdict is None or verdict[1] < _float_env("MITOSIS_JUDGE_MIN_CONFIDENCE", 0.7):
        return None
    return verdict
