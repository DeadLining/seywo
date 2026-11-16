"""Pure capacity/usage contract, with no network or model calls."""

from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True)
class Capacity:
    context_limit: int
    input_limit: int
    output_limit: int
    protocol_margin: int = 512

    def __post_init__(self):
        for name in ("context_limit", "input_limit", "output_limit"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.protocol_margin) is not int or self.protocol_margin < 0:
            raise ValueError("protocol_margin must be a non-negative integer")

    def input_cap(self, application_cap: int, output_reserve: int) -> int:
        if (type(application_cap) is not int or application_cap <= 0
                or type(output_reserve) is not int or output_reserve <= 0
                or output_reserve > self.output_limit):
            raise ValueError("invalid application/output allocation")
        cap = min(application_cap, self.input_limit,
                  self.context_limit - output_reserve - self.protocol_margin)
        if cap <= 0:
            raise ValueError("no input capacity after output/protocol reserve")
        return cap


def measure_request(request: dict, tokenizer=None) -> dict:
    import json
    # Includes full supplied JSON: prompts, metadata, bodies, schema and tool framing.
    wire = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    if tokenizer is None:
        # One UTF-8 byte per reserved token plus overhead; conservative estimate only.
        return {"tokens": len(wire.encode("utf-8")) + 512, "kind": "estimated",
                "method": "utf8_bytes_plus_512_v1", "provider_verified": False}
    tokens = tokenizer(wire)
    if type(tokens) is not int or tokens < 0:
        raise ValueError("tokenizer must return a non-negative integer")
    return {"tokens": ceil(tokens), "kind": "measured",
            "method": "injected_tokenizer", "provider_verified": False}


def reserve_request(request: dict, capacity: Capacity, *, application_cap: int,
                    output_reserve: int, tokenizer=None) -> dict:
    measurement = measure_request(request, tokenizer)
    cap = capacity.input_cap(application_cap, output_reserve)
    return {**measurement, "input_cap": cap, "output_reserve": output_reserve,
            "allowed": measurement["tokens"] <= cap,
            "action": "ready" if measurement["tokens"] <= cap else "split_or_wait"}
