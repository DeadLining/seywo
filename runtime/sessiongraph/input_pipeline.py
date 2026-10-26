"""Deterministic evidence selection, work turns, and non-contiguous coverage.

No providers, no transcript copies, no substring paraphrasing. Selection excludes
known process noise only; unknown output retains its entire source range. Ledger
completion shares the graph transaction. Old cursor remains a contiguous frontier.
"""

import json
import re
import shlex
from .core import digest
from . import source_locator

VERSION = "input-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS sg_input_coverage(
 entry_id TEXT NOT NULL REFERENCES entries(id),revision TEXT NOT NULL,
 source_hash TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,
 disposition TEXT NOT NULL,reason TEXT NOT NULL,job TEXT NOT NULL,
 PRIMARY KEY(entry_id,revision,source_hash,start,end));
CREATE TABLE IF NOT EXISTS sg_work_units(
 id TEXT PRIMARY KEY,segment TEXT NOT NULL,first_line INTEGER NOT NULL,
 last_line INTEGER NOT NULL,UNIQUE(segment,first_line));
CREATE TABLE IF NOT EXISTS sg_input_schedule(segment TEXT PRIMARY KEY,completed INTEGER NOT NULL);
"""
NOISE = re.compile(
    r"^\s*(?:progress\s+\d+(?:\.\d+)?%|\[?\d+(?:\.\d+)?%\]?|[━─═█░▒▓▏▎▍▌▋▊▉\s]+)\s*$"
)
KEY = re.compile(
    r"(?i)(?:error|fail|passed|accuracy|loss\s*[:=]|metric|run[_ -]?id|artifact|checkpoint|output[_ -]?(?:path|dir)|sha256|exit.code|错误|失败|通过|结果|产物|路径)"
)
RETRIEVAL = (
    "session_recall",
    "session_search",
    "session_expand",
    "session_resume",
    "session_packet",
    "codegraph_explore",
    "codegraph_node",
)


def setup(store):
    source_locator.setup(store)
    store.db.executescript(SCHEMA)


def merge(spans):
    out = []
    for a, b in sorted(spans):
        if a >= b:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(b, out[-1][1])
        else:
            out.append([a, b])
    return out


def subtract(spans, done):
    out = []
    for a, b in spans:
        if a >= b:
            continue  # Empty text has no work and must not pin the cursor.
        pieces = [[a, b]]
        for x, y in done:
            pieces = [
                z
                for l, r in pieces
                for z in ([[l, min(r, x)]] if l < x else []) + ([[max(l, y), r]] if r > y else [])
                if z[0] < z[1]
            ]
        out += pieces
    return out


def code_read_command(text):
    """Only plain read-only commands targeting code; mixed scripts remain unknown."""
    try:
        args = json.loads(text.split("\n", 1)[1])
        command = args.get("cmd", "")
        if not isinstance(command, str) or not command:
            return False
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        chunks = [[]]
        for word in lex:
            if word in (";", "&&", "||", "|", "\n"):
                if chunks[-1]:
                    chunks.append([])
            else:
                chunks[-1].append(word)
        seen = False
        for words in filter(None, chunks):
            if words[:2] == ["git", "status"]:
                continue
            if words[0] not in ("cat", "sed", "rg", "head", "tail"):
                return False
            paths = [w for w in words[1:] if re.search(r"\.(?:py|rs|tsx?|jsx?|swift|sh)$", w)]
            if not paths:
                return False
            # Explicit non-code file arguments make the whole output ambiguous.
            if any(re.search(r"\.(?:md|json|jsonl|csv|txt|log|yaml|toml)$", w) for w in words[1:]):
                return False
            seen = True
        return seen
    except (ValueError, TypeError, IndexError, AttributeError):
        return False


def _text_projection(store, row):
    text = row["text"]
    meta = source_locator.metadata(store, row)
    full = [[0, len(text)]]
    if meta["channel"] in ("analysis", "reasoning") or row["kind"] == "reasoning":
        return [], "internal_reasoning"
    if row["kind"] == "message":
        # Only exact standalone injection envelopes; do not substring-match a user's discussion.
        envelope = re.fullmatch(r"\s*<(environment_context|turn_aborted)>.*</\1>\s*", text, re.S)
        memory_injection = row["role"] != "user" and text.startswith(
            "SessionGraph work memory for this project (read-only, source-backed)."
        )
        if envelope or memory_injection:
            return [], "runtime_context"
        ambient = list(re.finditer(r"<in-app-browser-context\b[^>]*>.*?</in-app-browser-context>", text, re.S))
        if ambient:
            return subtract(full, [[m.start(), m.end()] for m in ambient]), "ambient_browser_context"
        if row["role"] in ("system", "developer"):
            return [], "runtime_instructions"
        return full, "message"
    tool = meta.get("tool_name", "")
    call_text = row["text"] if row["kind"] == "tool_call" else ""
    if row["kind"] == "tool_result" and row.get("call_id"):
        call = store.db.execute(
            "SELECT * FROM entries WHERE session_id=? AND call_id=? AND kind='tool_call' AND line<? ORDER BY line DESC LIMIT 1",
            (row["session_id"], row["call_id"], row["line"]),
        ).fetchone()
        if call:
            call_text = call["text"]
            tool = (
                source_locator.metadata(store, dict(call)).get("tool_name", "")
                or call_text.split("\n", 1)[0]
            )
    if tool.endswith("js") and re.search(r"\bcua\.rewriteDocumentation\(", call_text):
        return [], "tool_documentation"
    if any(tool.endswith(name) for name in RETRIEVAL):
        return [], "retrieved_context"
    if tool.endswith("exec_command") and code_read_command(call_text):
        if row["kind"] == "tool_call":
            return [], "code_read_context"
        status = re.search(r"Process exited with code (\d+)", text)
        if status and status[1] == "0":
            return [[status.start(), status.end()]], "code_read_context"
    if row["kind"] == "tool_call":
        # Command/script bodies are intent, not execution results. Preserve tool identity;
        # short structured parameters (including paths/IDs) remain visible.
        if len(text) <= 2048:
            return full, "tool_intent"
        return [[0, len(text.split("\n", 1)[0])]], "large_tool_arguments"
    if row["kind"] == "tool_result":
        spans = []
        pos = 0
        for line in text.splitlines(keepends=True):
            if not NOISE.fullmatch(line.strip()):
                spans.append([pos, pos + len(line)])
            pos += len(line)
        # Identical consecutive lines are redundant, except quantitative/error evidence.
        out = []
        previous = None
        for a, b in merge(spans):
            p = a
            for line in text[a:b].splitlines(keepends=True):
                if line != previous or KEY.search(line):
                    out.append([p, p + len(line)])
                previous = line
                p += len(line)
        return merge(out), "tool_result_noise" if sum(b - a for a, b in out) < len(
            text
        ) else "unknown_output_preserved"
    return full, "unknown_preserved"


def projection(store, row):
    from .content_boundary import excluded_spans
    spans, reason = _text_projection(store, row)
    binary = excluded_spans(row['text'])
    if not binary:return spans, reason
    retained = subtract(spans, binary)
    if not re.search(r'\w', ''.join(row['text'][a:b] for a,b in retained)):
        retained=[]
    return retained, 'media_payload'


def batch_capacity(config, requested=None):
    """Conservative selected-character budget; not a tokenizer/context-window claim."""
    from .extraction import _provider_options
    output = _provider_options(config)["max_output_tokens"]
    return min(requested if requested is not None else 16000,
               config["extraction"]["max_input_chars"], max(512, output * 2))


def selected_fragments(row, spans, cap, start=0):
    """Exact, bounded original ranges; filters never fabricate evidence text."""
    eligible = subtract(spans, [[0, start]])
    for i, (a, b) in enumerate(eligible):
        while a < b:
            end = min(a + cap, b)
            final = end == b and i == len(eligible)-1
            # Keep short cross-boundary evidence intact within a retained range.
            next_start = (eligible[i+1][0] if end == b and not final else end if end == b else end-min(399,cap//4))
            yield dict(row, text=row["text"][a:end], source_start=a, source_end=end,
                       source_length=len(row["text"]), source_hash=digest(row["text"]),
                       next_start=next_start, fragment_final=final)
            a = next_start


def row_descriptor(row, spans):
    return {
        "id": row["id"],
        "source_start": spans[0][0],
        "source_end": spans[-1][1],
        "source_hash": digest(row["text"]),
        "source_spans": spans,
    }


def render(row, desc):
    from .graph_contract import clean

    item = dict(row)
    item.update(desc)
    spans = desc.get("source_spans", [[desc["source_start"], desc["source_end"]]])
    from .content_boundary import excluded_spans
    spans = subtract(spans, excluded_spans(row["text"]))
    if not re.search(r"\w", "".join(row["text"][a:b] for a,b in spans)):
        spans=[]
    item["source_spans"] = spans
    parts = [clean(row["text"][a:b]) for a, b in spans]
    item.update(text="\n".join(parts), evidence_fragments=parts)
    return item


def units(store, segment, through_rowid=None):
    # Lightweight boundary query; do not load historical tool bodies to find a turn.
    rows = store.db.execute(
        "SELECT line,role,kind,substr(text,1,32) AS preview FROM entries WHERE session_id=? AND (? IS NULL OR rowid<=?) ORDER BY line",
        (segment, through_rowid, through_rowid),
    )
    groups = []
    responded = False
    for r in rows:
        confirmation = re.fullmatch(
            r"(?:是的|可以|继续|确认|好的|我确认这个结果|yes|ok)[。！! .]*", r["preview"], re.I
        )
        if not groups or (
            r["role"] == "user" and r["kind"] == "message" and responded and not confirmation
        ):
            groups.append([r["line"], r["line"]])
            responded = False
        else:
            groups[-1][1] = r["line"]
        if r["role"] != "user":
            responded = True
    return groups


def pending_ranges(store, row, line, start, *, include_claims=True):
    beginning = start if row["line"] == line else 0
    if row["line"] < line:
        return []
    done = [
        list(r)
        for r in store.db.execute(
            "SELECT start,end FROM sg_input_coverage WHERE entry_id=? AND revision=? AND source_hash=?",
            (row["id"], VERSION, digest(row["text"])),
        )
    ]
    if include_claims and store.db.execute("SELECT 1 FROM sqlite_master WHERE name='sg2_claims'").fetchone():
        done.extend(list(r) for r in store.db.execute(
            "SELECT start,end FROM sg2_claims WHERE entry_id=? AND source_hash=?",(row["id"],digest(row["text"]))))
    return subtract([[beginning, len(row["text"])]], done)


def plan(store, segment, line, start, max_chars, through_rowid=None, *, probe=False):
    groups = units(store, segment, through_rowid)
    n = store.db.execute(
        "SELECT completed FROM sg_input_schedule WHERE segment=?", (segment,)
    ).fetchone()
    # One recent turn, then one oldest turn: history cannot starve.
    ordered = groups if n and n[0] % 2 else list(reversed(groups))
    for lo, hi in ordered:
        if hi < line:
            continue
        batch = []
        coverage = []
        used = 0
        raw_chars = excluded = 0
        for raw in store.db.execute(
            "SELECT * FROM entries WHERE session_id=? AND line BETWEEN ? AND ? AND (? IS NULL OR rowid<=?) ORDER BY line",
            (segment, max(lo, line), hi, through_rowid, through_rowid),
        ):
            row = dict(raw)
            remaining = pending_ranges(store, row, line, start)
            if not remaining:
                continue
            spans, reason = projection(store, row)
            selected = []
            for a, b in spans:
                for c, d in remaining:
                    if max(a, c) < min(b, d):
                        selected.append([max(a, c), min(b, d)])
            excluded_ranges = subtract(remaining, selected)
            for a, b in excluded_ranges:
                coverage.append([row["id"], digest(row["text"]), a, b, "excluded", reason])
                excluded += b - a
                raw_chars += b - a
            taken = []
            for a, b in selected:
                space = max_chars - used - (1 if taken else 0)
                if space <= 0:
                    break
                end = min(b, a + space)
                taken.append([a, end])
                used += end - a + (1 if len(taken) > 1 else 0)
                raw_chars += end - a
            if taken:
                desc = row_descriptor(row, taken)
                batch.append(render(row, desc))
                coverage.extend(
                    [row["id"], digest(row["text"]), a, b, "extracted", reason] for a, b in taken
                )
            if used >= max_chars or len(batch) >= 24:
                break
        if coverage:
            if probe:
                return True
            unit = "wu_" + digest(f"{segment}:{lo}")[:24]
            store.db.execute(
                "INSERT INTO sg_work_units VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_line=max(last_line,excluded.last_line)",
                (unit, segment, lo, hi),
            )
            # Just enough preceding human-facing context for a short confirmation or a split turn.
            seen = {e["id"] for e in batch}
            context = []
            if used < max_chars:
                for raw in store.db.execute(
                    "SELECT * FROM entries WHERE session_id=? AND line<? AND kind='message' ORDER BY line DESC LIMIT 4",
                    (segment, lo),
                ):
                    r = dict(raw)
                    if r["id"] in seen:
                        continue
                    spans, _ = projection(store, r)
                    if not spans or len(r["text"]) > min(1500, max_chars - used):
                        continue
                    d = row_descriptor(r, spans)
                    d["context_only"] = True
                    context.append(render(r, d))
                    used += len(r["text"])
            return {
                "entries": context[::-1] + batch,
                "end": [line, start],
                "input_revision": VERSION,
                "selection_revision": "typed-content-v2",
                "work_unit": unit,
                "coverage": coverage,
                "input_stats": {
                    "input_source_chars": raw_chars,
                    "input_selected_chars": used,
                    "input_excluded_chars": excluded,
                    "work_units": 1,
                },
            }
    return False if probe else None


def commit(store, desc, job, segment, project):
    for eid, h, a, b, state, reason in desc["coverage"]:
        store.db.execute(
            "INSERT OR IGNORE INTO sg_input_coverage VALUES(?,?,?,?,?,?,?,?)",
            (eid, VERSION, h, a, b, state, reason, job),
        )
    store.db.execute(
        "INSERT INTO sg_input_schedule VALUES(?,1) ON CONFLICT(segment) DO UPDATE SET completed=completed+1",
        (segment,),
    )
    line, start = desc["end"]
    for raw in store.db.execute(
        "SELECT * FROM entries WHERE session_id=? AND line>=? ORDER BY line", (segment, line)
    ):
        row = dict(raw)
        rem = pending_ranges(store, row, line, start, include_claims=False)
        if rem:
            line, start = row["line"], rem[0][0]
            break
        line, start = row["line"] + 1, 0
    store.db.execute(
        "INSERT INTO sg2_cursors VALUES(?,?,?,?) ON CONFLICT(project,segment) DO UPDATE SET line=excluded.line,start=excluded.start WHERE excluded.line>sg2_cursors.line OR (excluded.line=sg2_cursors.line AND excluded.start>sg2_cursors.start)",
        (project, segment, line, start),
    )


def describe(entry):
    return {
        k: entry[k]
        for k in ("id", "source_start", "source_end", "source_hash", "source_spans", "context_only")
        if k in entry
    }


def split_descriptor(e, budget):
    spans = e.get("source_spans", [[e["source_start"], e["source_end"]]])
    left = []
    right = []
    for a, b in spans:
        cut = min(b, a + max(0, budget))
        if cut > a:
            left.append([a, cut])
        if cut < b:
            right.append([cut, b])
        budget -= cut - a

    def pack(ss):
        return (
            {**e, "source_start": ss[0][0], "source_end": ss[-1][1], "source_spans": ss}
            if ss
            else None
        )

    return pack(left), pack(right)
