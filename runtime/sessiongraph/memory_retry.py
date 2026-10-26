"""Write-side only: bounded retries. Retrieval never instantiates this budget."""

import time

TRANSIENT_CODES = frozenset(
    {
        "provider_timeout",
        "provider_transport",
        "provider_http_408",
        "provider_http_425",
        "provider_http_429",
        "provider_http_500",
        "provider_http_502",
        "provider_http_503",
        "provider_http_504",
        "provider_empty_output",
        "provider_malformed_response",
    }
)
QUALITY_ERRORS = (
    "no assistant output_text",
    "malformed",
    "quote exceeds",
    "not an exact source substring",
    "outside input batch",
    "invalid memory content",
    "invalid memory title",
)


def retryable(exc):
    code = getattr(exc, "code", None)
    return code in TRANSIENT_CODES if code else any(term in str(exc) for term in QUALITY_ERRORS)


class RetryBudget:
    """At most 3 attempts per call and 8 extra attempts per worker invocation."""

    def __init__(self, max_calls=None):
        self.max_calls = max_calls
        self.attempts = 0
        self.retries = 0

    def call(self, operation):
        for attempt in range(3):
            if self.max_calls is not None and self.attempts >= self.max_calls:
                from .extraction import ProviderError
                raise ProviderError("automatic call budget exhausted", "graph_call_budget_pending")
            self.attempts += 1
            try:
                return operation()
            except ValueError as exc:
                if not retryable(exc) or attempt == 2 or self.retries >= 8:
                    raise
                self.retries += 1
                time.sleep(float(2**attempt))
