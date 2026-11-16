"""Provider adapters with explicit identities; transports are injected, never discovered."""

from dataclasses import dataclass

from .contracts import validate_judgment
from .model_contract import jev_envelope, validate_wire


@dataclass
class FunctionBackend:
    """General-model adapter: supplied transport returns output/usage/finish envelope.

    The caller owns protocol transport, credential handling and model selection.
    No source path, secret or installed legacy provider configuration is discovered.
    """
    identity: str
    transport: object

    def complete(self, request):
        validate_wire(request)
        if request["backend"] != self.identity:
            raise ValueError("backend_identity_mismatch")
        result = self.transport(request)
        if not isinstance(result, dict) or not set(result) <= {"output", "usage", "finish"}:
            raise ValueError("invalid_backend_envelope")
        return result


@dataclass
class JevBackend:
    """Adapter for the project's observed state/questions/criteria Jev protocol.

    evaluate is explicitly supplied; this package neither imports SessionGraph nor
    opens a network connection itself. Availability and quality need live evaluation.
    """
    model: str
    evaluate: object

    @property
    def identity(self):
        return "jev:" + self.model

    def complete(self, request):
        if request["phase"] != "judgment" or request["backend"] != self.identity:
            raise ValueError("jev_accepts_explicit_bounded_judgments_only")
        envelope = jev_envelope(request)
        state, questions = envelope["state"], envelope["questions"]
        response = self.evaluate(state, questions)
        if response.get("model") != self.model:
            raise ValueError("unexpected_jev_model")
        answer = response["answers"]["q"]
        output = {"input_fingerprint": "bound-by-attempt", "choice": answer["choice"],
                  "scores": answer["probabilities"], "score_kind": "probabilities",
                  "reason": answer.get("reason", "bounded provider choice; not human confirmation")}
        validate_judgment(output, {**request, "input_fingerprint": "bound-by-attempt"})
        del output["input_fingerprint"]
        if output["scores"][output["choice"]] < max(output["scores"].values()):
            raise ValueError("jev_choice_not_maximum")
        usage = response.get("usage")
        if usage is not None and set(usage) != {"input_tokens", "output_tokens"}:
            # Unknown upstream usage vocabulary is not invented as exact counts.
            usage = None
        return {"output": output, "usage": usage, "finish": "complete"}
