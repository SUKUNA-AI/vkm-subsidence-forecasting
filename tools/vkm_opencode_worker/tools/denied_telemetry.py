"""Bounded, typed refusal evidence for the native HookGate.

This module only builds and validates one denied-telemetry record. It performs no
I/O, no budget/ledger call and no admission decision. The caller must treat a
failed diagnostic as a missing record, never as permission or settlement.

Privacy rules: no exception text/repr/args are accepted; no raw body, headers,
credentials, reasoning, tool payload or source path is accepted. The only body
evidence is a SHA-256 digest and UTF-8 byte length of the trusted /before body
text, supplied by request_gate.py as bytes.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation

SCHEMA = "vkm.opencode.denied-telemetry/1"

STAGES = frozenset({
    "HOOK_HANDLER",
    "WIRE_POLICY",
    "BUDGET_BEFORE",
    "TOOL_SCOPE",
    "DISPATCH",
    "DISPATCH_AFTER",
    "UNCLASSIFIED",
})

REASON_CODES = frozenset({
    "UNCLASSIFIED",
    "HOOK_AUTHENTICATION_FAILED",
    "MALFORMED_REQUEST",
    "ENDPOINT_CONTEXT_LIMIT",
    "MODEL_PIN_MISMATCH",
    "PROVIDER_FALLBACK_PIN_MISMATCH",
    "REASONING_PIN_MISMATCH",
    "OUTPUT_CAP_INVALID",
    "SESSION_BUDGET_EXHAUSTED",
    "INFLIGHT_OR_UNKNOWN_ATTEMPT",
    "UNKNOWN_EXECUTION_COST",
    "RUNTIME_LEASE_EXPIRED",
    "RETRIES_DISABLED",
    "EXACT_REQUEST_BYTES_REQUIRED",
    "REQUEST_BODY_DIGEST_MISMATCH",
    "REQUEST_BODY_INVALID_JSON",
    "REQUEST_BODY_PIN_MISMATCH",
    "FINITE_OUTPUT_TOKEN_BOUND_REQUIRED",
    "POSITIVE_UPPER_BOUND_REQUIRED",
})

# Budget subset of the contract enum: only codes the actual shared_budget
# dependency raises plus the contract's budget-stage refusals. A BudgetError
# with any other code is UNCLASSIFIED.
BUDGET_REASON_CODES = frozenset({
    "SESSION_BUDGET_EXHAUSTED",
    "INFLIGHT_OR_UNKNOWN_ATTEMPT",
    "UNKNOWN_EXECUTION_COST",
    "RUNTIME_LEASE_EXPIRED",
    "RETRIES_DISABLED",
    "EXACT_REQUEST_BYTES_REQUIRED",
    "REQUEST_BODY_DIGEST_MISMATCH",
    "REQUEST_BODY_INVALID_JSON",
    "REQUEST_BODY_PIN_MISMATCH",
    "FINITE_OUTPUT_TOKEN_BOUND_REQUIRED",
    "POSITIVE_UPPER_BOUND_REQUIRED",
    "MODEL_PIN_MISMATCH",
    "PROVIDER_FALLBACK_PIN_MISMATCH",
    "REASONING_PIN_MISMATCH",
})

# Exact static WorkerError message mapping from DIAGNOSTIC_CONTRACT.json.
# Dynamic messages, unmapped values and non-str args are UNCLASSIFIED.
WORKER_ERROR_CODES = {
    "hook authentication failed": "HOOK_AUTHENTICATION_FAILED",
    "endpoint context bound exceeded": "ENDPOINT_CONTEXT_LIMIT",
    "request model differs from selected model": "MODEL_PIN_MISMATCH",
    "request provider policy differs": "PROVIDER_FALLBACK_PIN_MISMATCH",
    "request reasoning differs": "REASONING_PIN_MISMATCH",
    "missing/ambiguous/oversized output cap": "OUTPUT_CAP_INVALID",
    "unexpected hook request fields": "MALFORMED_REQUEST",
    "invalid JSON": "MALFORMED_REQUEST",
}

COUNT_METRICS = frozenset({
    "input_bound",
    "output_cap",
    "context_limit",
    "image_count",
    "messages_count",
})
MONEY_METRICS = frozenset({
    "estimated_upper_usd",
    "session_remaining_usd",
})

_MAX_COUNT = 2 ** 63 - 1
_MAX_MONEY_INTEGER_DIGITS = 10
_MAX_MONEY_FRACTION_DIGITS = 16
_MONEY_TEXT = re.compile(
    r"(?:0|[1-9][0-9]{0,%d})(?:\.[0-9]{1,%d})?\Z"
    % (_MAX_MONEY_INTEGER_DIGITS - 1, _MAX_MONEY_FRACTION_DIGITS)
)
# Canonical Decimal.__str__ may use exponent form for very small finite
# amounts. Accept it only when it is already the exact canonical rendering.
_MONEY_EXPONENT_TEXT = re.compile(
    r"(?:0|[1-9][0-9]{0,%d})(?:\.[0-9]{1,%d})?[eE][-+]?[0-9]{1,3}\Z"
    % (_MAX_MONEY_INTEGER_DIGITS - 1, _MAX_MONEY_FRACTION_DIGITS)
)
_MONEY_CEILING = Decimal("1e13")


def worker_reason(args):
    """Return an exact allowlisted reason for one static WorkerError arg.

    No str()/repr() is applied to the exception or its args. Anything except a
    single exact str argument that equals a contract message is UNCLASSIFIED.
    """
    if type(args) is not tuple or len(args) != 1:
        return "UNCLASSIFIED"
    message = args[0]
    if type(message) is not str:
        return "UNCLASSIFIED"
    return WORKER_ERROR_CODES.get(message, "UNCLASSIFIED")


def budget_reason(code):
    """Accept only an exact str code already present in the contract enum.

    The caller is responsible for verifying that the exception is the actual
    imported shared_budget.BudgetError. Any other exception, type, or value is
    UNCLASSIFIED; no prefix/regex normalization is performed.
    """
    if type(code) is str and code in BUDGET_REASON_CODES:
        return code
    return "UNCLASSIFIED"


def money_text(value):
    """Serialize a finite nonnegative amount as bounded decimal text.

    Only exact Decimal, int, or canonical decimal str inputs are accepted. No
    arbitrary object is ever formatted or coerced, so a malicious __str__,
    __repr__ or __format__ cannot be invoked.
    """
    if type(value) is Decimal:
        if not value.is_finite() or value < 0 or value >= _MONEY_CEILING:
            raise ValueError("invalid finite money")
        # Native diagnostic-sanitizer repair: reject unsupported precision and
        # exponent BEFORE str/format could expand an arbitrarily long coefficient
        # or fixed-point decimal. This bounds optional telemetry only; no budget
        # calculation, price, ledger amount or admission predicate is changed.
        if value.adjusted() < -999:
            raise ValueError("unbounded diagnostic exponent")
        representation = value.as_tuple()
        if len(representation.digits) > 64 or not -999 <= representation.exponent <= 12:
            raise ValueError("unbounded diagnostic decimal representation")
        text = str(value)
        if _MONEY_TEXT.fullmatch(text) is not None or _MONEY_EXPONENT_TEXT.fullmatch(text) is not None:
            return text
        text = format(value, "f")
        if _MONEY_TEXT.fullmatch(text) is None:
            raise ValueError("unbounded money text")
        if Decimal(text) != value:
            raise ValueError("lossy money text")
        return text
    if type(value) is int:
        if value < 0 or value > 10 ** _MAX_MONEY_INTEGER_DIGITS - 1:
            raise ValueError("invalid money integer")
        return str(value)
    if type(value) is str:
        if _MONEY_TEXT.fullmatch(value) is None:
            raise ValueError("invalid money text")
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("invalid money text") from exc
        if not amount.is_finite() or amount < 0 or amount >= _MONEY_CEILING:
            raise ValueError("invalid finite money")
        return value
    raise ValueError("unsupported money type")


def _safe_metrics(metrics):
    if metrics is None:
        return {}
    if type(metrics) is not dict:
        raise ValueError("invalid denial metrics")
    safe = {}
    for key, value in metrics.items():
        if type(key) is not str:
            raise ValueError("invalid denial metric name")
        if key in COUNT_METRICS:
            if type(value) is not int or not 0 <= value <= _MAX_COUNT:
                raise ValueError("invalid count metric")
            safe[key] = value
        elif key in MONEY_METRICS:
            safe[key] = money_text(value)
        else:
            raise ValueError("unknown denial metric")
    return safe


def build_record(*, stage, reason_code, body_bytes=None, metrics=None):
    """Build exactly one bounded denied-telemetry record.

    stage/reason_code must already be contract enums; body_bytes is either None
    or exact bytes from the trusted /before body_text. Invalid inputs raise
    ValueError and the caller must omit the record, never change its refusal.
    """
    if type(stage) is not str or stage not in STAGES:
        raise ValueError("invalid denial stage")
    if type(reason_code) is not str or reason_code not in REASON_CODES:
        raise ValueError("invalid denial reason")
    if body_bytes is not None and type(body_bytes) is not bytes:
        raise ValueError("invalid body evidence")
    safe_metrics = _safe_metrics(metrics)
    if body_bytes is None:
        body_sha256 = None
        body_utf8_bytes = None
    else:
        body_sha256 = hashlib.sha256(body_bytes).hexdigest()
        body_utf8_bytes = len(body_bytes)
    return {
        "schema": SCHEMA,
        "stage": stage,
        "reason_code": reason_code,
        "body_sha256": body_sha256,
        "body_utf8_bytes": body_utf8_bytes,
        "metrics": safe_metrics,
        "request_admitted": False,
        "content_saved": False,
        "accounting_changed": False,
    }
