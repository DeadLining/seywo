"""Explicit worker launcher: reuse only the matching loopback Codex provider credential."""

import argparse
import os
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .cli import main as cli_main
from .core import load_config
from .provider_credentials import load_key, runtime_state, validate_endpoint


@contextmanager
def gateway_credentials(config: dict, codex_config: Path):
    provider = config["provider"]
    endpoint = provider.get("endpoint", "")
    url = urlsplit(endpoint)
    name = provider.get("api_key_env", "GRAPH_LLM_API_KEY")
    existing = os.environ.get(name)
    if existing:
        yield
        return
    if provider.get("protocol") == "responses":
        validate_endpoint(endpoint)
        saved = load_key(runtime_state(), endpoint)
        if saved:
            os.environ[name] = saved
            try:
                yield
            finally:
                os.environ.pop(name, None)
            return
    if provider.get("protocol") != "responses" or url.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError(
            "credential reuse is restricted to an explicitly configured loopback Responses gateway"
        )
    base = endpoint.rstrip("/").rsplit("/", 1)[0]
    providers = tomllib.loads(codex_config.read_text()).get("model_providers", {})
    matches = [p for p in providers.values() if p.get("base_url", "").rstrip("/") == base]
    if len(matches) != 1:
        raise ValueError("expected exactly one matching Codex Gateway provider")
    matched = matches[0]
    key = os.environ.get(matched.get("env_key", ""), "") or matched.get("experimental_bearer_token")
    if not isinstance(key, str) or not key:
        raise ValueError("matching Gateway provider has no available credential")
    os.environ[name] = key
    try:
        yield
    finally:
        if existing is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = existing


def main():
    parser = argparse.ArgumentParser(
        description="Explicit Gateway worker; selected queued transcripts may leave this machine via Gateway."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--request-run-id", default="")
    parser.add_argument("--respect-graph-schedule", action="store_true")
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--max-jobs", type=int, default=1)
    parser.add_argument("--allow-network", action="store_true", required=True)
    parser.add_argument("--memories-pending-only", action="store_true",
                        help="retry only durable unfinished windows for this chat")
    parser.add_argument("--memories-repair-oversize", action="store_true",
                        help="explicitly repair legacy long-message candidates for selected chat")
    parser.add_argument("--memories-call-budget", type=int, default=None)
    parser.add_argument("--memories-step-budget", type=int, default=None)
    parser.add_argument("--memories-through-rowid", type=int, default=None)
    parser.add_argument("--memories-since", default=None,
                        help="ISO timestamp filter passed to the memories extractor")
    parser.add_argument("--memories-max-chars", type=int, default=None,
                        help="batch size passed to the memories extractor")
    parser.add_argument("--memories-transcript-path", default=None,
                        help="restrict extraction to the segment for this transcript")
    parser.add_argument("--memories-lookback-chars", type=int, default=None,
                        help="first-run scan budget passed to the memories extractor")
    parser.add_argument(
        "--memories",
        metavar="CHAT_ID",
        help="run the incremental work-memory extractor instead of the records worker",
    )
    args = parser.parse_args()
    if args.request_run_id:
        if not args.request_run_id.isalnum() or len(args.request_run_id) > 64:
            parser.error("invalid request run id")
        os.environ["SESSIONGRAPH_RUN_ID"] = args.request_run_id
    if args.respect_graph_schedule:
        os.environ["SESSIONGRAPH_RESPECT_SCHEDULE"] = "1"
    try:
        config = load_config(args.config)
        argv = ["--config", str(args.config)]
        if args.memories:
            argv += ["memories", "--chat-id", args.memories, "--allow-network"]
            if args.memories_pending_only:
                argv += ["--pending-only"]
            if args.memories_repair_oversize:
                argv += ["--repair-oversize"]
            if args.memories_call_budget is not None:
                argv += ["--call-budget", str(args.memories_call_budget)]
            if args.memories_step_budget is not None:
                argv += ["--step-budget", str(args.memories_step_budget)]
            if args.memories_through_rowid is not None:
                argv += ["--through-rowid", str(args.memories_through_rowid)]
            if args.memories_since:
                argv += ["--since-time", args.memories_since]
            if args.memories_max_chars:
                argv += ["--max-chars", str(args.memories_max_chars)]
            if args.memories_transcript_path:
                argv += ["--transcript-path", args.memories_transcript_path]
            if args.memories_lookback_chars:
                argv += ["--lookback-chars", str(args.memories_lookback_chars)]
        else:
            argv += ["worker", "--allow-network", "--max-jobs", str(args.max_jobs)]
        with gateway_credentials(config, args.codex_config):
            return cli_main(argv)
    except (OSError, ValueError) as exc:
        print(f"Gateway worker unavailable: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
