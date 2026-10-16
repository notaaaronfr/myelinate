"""Guards applied to everything that becomes durable knowledge.

Learned skills are injected into future prompts, so a poisoned skill is a persistent prompt injection.
Every proposal is scanned before it can be approved, and PII is redacted from stored episodes.
"""

from __future__ import annotations

import re

_PII = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("CARD", re.compile(r"\b\d(?:[ -]?\d){12,18}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PHONE", re.compile(r"(?<!\w)\+?\d{1,3}[ .-]?\(?\d{2,4}\)?[ .-]?\d{3,4}[ .-]?\d{3,4}(?!\w)")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
]

_SECRETS = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("password_assignment", re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key)\s*[:=]\s*(?!\[)\S{6,}")),
]

_INJECTION = [
    re.compile(p, re.I)
    for p in (
        r"ignore (all |any )?(previous|prior|above) (instructions|rules)",
        r"disregard (the |your )?(system|previous) (prompt|instructions)",
        r"you are now (?!able)",
        r"(reveal|print|exfiltrate|send) (the |your )?(system prompt|secrets?|credentials|api keys?)",
        r"curl [^\n]*\|\s*(ba)?sh",
        r"rm -rf /(?!\w)",
        r"(disable|bypass|turn off) (the )?(safety|guardrails?|security|audit)",
        r"do not (tell|inform) the user",
    )
]


def redact(text: str) -> str:
    for label, pat in _PII:
        text = pat.sub(f"[{label}]", text)
    for label, pat in _SECRETS:
        text = pat.sub(f"[REDACTED {label}]", text)
    return text


def scan(text: str, max_chars: int) -> list[str]:
    """Returns findings; any finding blocks auto-approval and is shown to human reviewers."""
    findings = [f"secret:{label}" for label, pat in _SECRETS if pat.search(text)]
    findings += [f"injection:{pat.pattern}" for pat in _INJECTION if pat.search(text)]
    if len(text) > max_chars:
        findings.append(f"size:{len(text)}>{max_chars}")
    return findings
