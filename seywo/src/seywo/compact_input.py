"""Lossless native-turn scheduling within a bounded model request.

Only oversized turns are sliced. Native identity stays runtime-owned; each slice
carries its original source ranges. Budgeting never decides semantic relevance.
"""
from .compact_model import MAX_ASSISTANT_CHARS, MAX_INPUT_CHARS, MAX_TURNS, MAX_USER_CHARS


def _role_spans(turn, role):
    refs = [source for source in turn['sources'] if source['role'] == role]
    if '\n'.join(ref['text'] for ref in refs) != turn[role]:
        raise ValueError('compact_source_text_mismatch')
    spans, cursor = [], 0
    for ref in refs:
        spans.append((cursor, cursor + len(ref['text']), ref))
        cursor += len(ref['text']) + 1
    return spans


def _sources_between(spans, start, end):
    result = []
    for left, right, source in spans:
        a, b = max(left, start), min(right, end)
        if a < b:
            offset = source['start']
            result.append({**source, 'text': source['text'][a - left:b - left],
                           'start': offset + a - left, 'end': offset + b - left})
    return result


def _end(text, start, limit):
    end = min(start + limit, len(text))
    if end < len(text):
        for position in range(end, start + limit // 2, -1):
            if text[position - 1].isspace() or text[position - 1] in "。！？!?；;.":
                return position
    return end


def _slices(turn):
    user, assistant = turn['user'], turn['assistant']
    if len(user) <= MAX_USER_CHARS and len(assistant) <= MAX_ASSISTANT_CHARS:
        return [turn]
    spans = {role: _role_spans(turn, role) for role in ('user', 'assistant')}
    parts, u, a = [], 0, 0
    while u < len(user) or a < len(assistant):
        ue = _end(user, u, MAX_USER_CHARS)
        # All of the request precedes its answer, even over batch boundaries.
        ae = _end(assistant, a, MAX_ASSISTANT_CHARS) if ue == len(user) else a
        parts.append({**turn, 'user': user[u:ue], 'assistant': assistant[a:ae],
                      'sources': _sources_between(spans['user'], u, ue)
                      + _sources_between(spans['assistant'], a, ae)})
        u, a = ue, ae
    for index, part in enumerate(parts):
        part['part'] = [index + 1, len(parts)]
    return parts


def batch_turns(turns):
    batches, current, chars = [], [], 0
    for turn in turns:
        parts = _slices(turn)
        total = len(turn['user']) + len(turn['assistant'])
        # Do not introduce a cross-request context boundary within a native
        # turn when its full text and slices can fit one bounded request.
        if (current and len(parts) <= MAX_TURNS and total <= MAX_INPUT_CHARS
                and (len(current) + len(parts) > MAX_TURNS or chars + total > MAX_INPUT_CHARS)):
            batches.append(current)
            current, chars = [], 0
        for part in parts:
            cost = len(part['user']) + len(part['assistant'])
            if current and (len(current) >= MAX_TURNS or chars + cost > MAX_INPUT_CHARS):
                batches.append(current)
                current, chars = [], 0
            current.append(part)
            chars += cost
    if current:
        batches.append(current)
    return batches
