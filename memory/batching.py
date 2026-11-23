"""Whole-line acknowledgement with bounded, source-backed encoding fragments."""
import json
import os
from io import StringIO
from core.model_errors import ModelCallError


FORMAT_REVISION = 'serialized-prefix-v5'
SOURCE_RULES = '\n本批记忆的 source_ids 只能引用 log 中的 message_id；其余上下文仅供理解。拆分来源仅表示原文片段，不能补写缺失事实。'


def estimated_tokens(text):
    # Conservative byte upper bound for unknown provider tokenizers. Report as
    # an estimate, never as the provider's measured usage.
    return len(text.encode('utf-8'))


def split_context(context):
    """Shrink only the current fragment; parent acknowledgement is unchanged."""
    lines = list(StringIO(context['log'], newline='\n'))
    if len(lines) > 1:
        middle = len(lines)//2
        return [{'log': ''.join(lines[:middle])}, {'log': ''.join(lines[middle:])}]
    try:
        row = json.loads(context['log'])
    except ValueError:
        row = None
    if isinstance(row, dict) and isinstance(row.get('content'), str):
        content = row['content']
        if len(content) <= 128:
            return None
        middle = len(content)//2
        return [{'log': json.dumps({**row, 'content': part, 'source_fragment': {
            'parent': row.get('source_fragment'), 'start': start, 'end': start+len(part), 'total_length': len(content)}},
            ensure_ascii=False)+'\n'} for start, part in ((0, content[:middle]), (middle, content[middle:]))]
    if len(context['log']) <= 128:
        return None
    middle = len(context['log'])//2
    return [{'log': context['log'][:middle]}, {'log': context['log'][middle:]}]


def make_batch(text, prompt, extras):
    budget = max(4000, int(os.getenv('EMBER_MEMORY_INPUT_BUDGET', '16000')))
    # Reserve room for the message envelope and a possible correction request.
    context_budget = budget - min(1024, budget // 8)
    remaining = context_budget - estimated_tokens(prompt)
    empty = {key: [] for key in extras}

    def envelope(log):
        return {'log': log, **empty}

    def fits(context):
        return estimated_tokens(json.dumps(context, ensure_ascii=False)) <= remaining

    # JSON escaping is additive within the log string. Count the full empty
    # envelope once and then each escaped line without its surrounding quotes.
    room = remaining - estimated_tokens(json.dumps(envelope(''), ensure_ascii=False))
    lines = []
    journal = StringIO(text, newline='\n')
    first_line = journal.readline()
    journal.seek(0)
    for line in journal:
        amount = estimated_tokens(json.dumps(line, ensure_ascii=False)) - 2
        if amount > room:
            break
        lines.append(line)
        room -= amount
    if lines:
        original = ''.join(lines)
        contexts = [envelope(original)]
    elif text:
        # The first physical source is too large by itself. Acknowledge its
        # original bytes only after all separately encoded fragments commit.
        original = first_line
        try:
            row = json.loads(original)
        except ValueError:
            row = None
        content = row.get('content') if isinstance(row, dict) else None
        source = content if isinstance(content, str) else original
        if not source or room <= 0:
            raise ModelCallError('input_budget')
        contexts = []
        start = 0
        while start < len(source):
            low, high = start + 1, min(len(source), start + room)
            best = None
            while low <= high:
                end = (low + high) // 2
                piece = source[start:end]
                if isinstance(content, str):
                    fragment = {**row, 'content': piece, 'source_fragment': {
                        'start': start, 'end': end, 'total_length': len(source)}}
                    piece = json.dumps(fragment, ensure_ascii=False) + '\n'
                candidate = envelope(piece)
                if fits(candidate):
                    best = (end, candidate)
                    low = end + 1
                else:
                    high = end - 1
            if not best:
                raise ModelCallError('input_budget')
            start, candidate = best
            contexts.append(candidate)
    else:
        return '', []
    for context in contexts:
        has_message_sources = False
        for line in context['log'].split('\n'):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (isinstance(row, dict) and row.get('role') in ('user', 'assistant', 'system_event')
                    and isinstance(row.get('message_id'), str)):
                has_message_sources = True
                break
        # Backfilling old state snapshots must not repeatedly summarize today's
        # unrelated buffered conversation and notes. They keep their own logs.
        if has_message_sources:
            for key, records in extras.items():
                for record in reversed(records):
                    candidate = {**context, key: [record] + context[key]}
                    if not fits(candidate):
                        continue
                    context[key] = candidate[key]
    return original, contexts
