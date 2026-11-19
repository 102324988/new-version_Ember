"""Whole-line acknowledgement with bounded, source-backed encoding fragments."""
import copy
import json
import os
from core.model_errors import ModelCallError


FORMAT_REVISION = 'complete-envelope-v4'
SOURCE_RULES = '\n本批记忆的 source_ids 只能引用 log 中的 message_id；其余上下文仅供理解。拆分来源仅表示原文片段，不能补写缺失事实。'


def estimated_tokens(text):
    # Conservative byte upper bound for unknown provider tokenizers. Report as
    # an estimate, never as the provider's measured usage.
    return len(text.encode('utf-8'))


def split_context(context):
    """Shrink only the current fragment; parent acknowledgement is unchanged."""
    lines = context['log'].splitlines(keepends=True)
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
    available = max(128, context_budget - estimated_tokens(prompt) - 1000)
    lines, size = [], 0
    for line in text.splitlines(keepends=True):
        amount = estimated_tokens(line)
        if lines and size + amount > available:
            break
        lines.append(line)
        size += amount
    original = ''.join(lines)
    fragments = []
    for line in lines:
        if estimated_tokens(line) <= available:
            fragments.append((line, False))
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        content = row.get('content') if isinstance(row, dict) else None
        # Splitting encoding input never consumes a partial log line. The raw
        # parent prefix is acknowledged only after every fragment is committed.
        if isinstance(content, str):
            metadata = copy.deepcopy(row)
            metadata['content'] = ''
            room = max(128, (available - estimated_tokens(json.dumps(metadata, ensure_ascii=False)) - 250) // 4)
            pieces = [content[i:i+room] for i in range(0, len(content), room)] or ['']
            for i, piece in enumerate(pieces):
                fragment = {**row, 'content': piece, 'source_fragment': {'index': i, 'total': len(pieces),
                    'start': i*room, 'end': i*room+len(piece), 'total_length': len(content)}}
                fragments.append((json.dumps(fragment, ensure_ascii=False) + '\n', True))
        else:
            room = max(128, available // 4)
            fragments.extend((line[i:i+room], True) for i in range(0, len(line), room))
    contexts = []
    # Pack ordinary lines into one small request, preserving long fragments.
    for fragment, isolated in fragments:
        # Parts share their original message ID. Keep each in its own context
        # so source lookup cannot replace an earlier part with the last one.
        if not isolated and contexts and estimated_tokens(contexts[-1]['log'] + fragment) <= available:
            contexts[-1]['log'] += fragment
        else:
            contexts.append({'log': fragment})
    for context in contexts:
        # Count every key before filling optional lists. Adding an empty list
        # afterwards used to invalidate a context that already filled the limit.
        context.update({key: [] for key in extras})
        has_message_sources = False
        for line in context['log'].splitlines():
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
                    if estimated_tokens(prompt) + estimated_tokens(json.dumps(candidate, ensure_ascii=False)) > context_budget:
                        continue
                    context[key].insert(0, record)
        if estimated_tokens(prompt) + estimated_tokens(json.dumps(context, ensure_ascii=False)) > context_budget:
            raise ModelCallError('input_budget')
    return original, contexts
