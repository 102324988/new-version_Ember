"""Persistent, bounded stage retries. Waiting for a budget is not a failure."""
import hashlib
import random
import time
from core.service_policy import current_budget_revision


def fingerprint(model):
    return hashlib.sha256('|'.join(str(getattr(model, k, '')) for k in ('name', 'base_url', 'api_key')).encode()).hexdigest()


def next_retry(previous=None, code='unavailable', retry_at=None, config='', budget_revision=None):
    previous = previous or {}
    if previous.get('config') and config and previous['config'] != config:
        previous = {}
    waiting = code in ('budget_wait', 'background_busy', 'circuit_wait')
    attempts = previous.get('attempts', 0) + (0 if waiting else 1)
    attention = code in ('authentication', 'configuration', 'source_changed', 'input_budget') or attempts >= 3
    if waiting:
        attention = bool(previous.get('needs_attention', False))
    if code == 'invalid_result' and attempts >= 2:
        attention = True
    seconds = min(3600, 60 * 2 ** max(0, attempts - 1))
    until = max(time.time() + seconds + random.uniform(0, seconds * .1), retry_at or 0)
    retry = {'attempts': attempts, 'code': code, 'needs_attention': attention,
            'next_attempt_at': until, 'config': config, 'failed_at': time.time()}
    if code == 'budget_wait':
        retry['budget_revision'] = current_budget_revision() if budget_revision is None else budget_revision
    return retry


def retry_ready(retry, config=''):
    if not retry:
        return True
    if retry.get('config') and config and retry['config'] != config:
        return True
    if retry.get('code') == 'budget_wait' and not retry.get('needs_attention'):
        if current_budget_revision() > retry.get('budget_revision', 0):
            return True
    return not retry.get('needs_attention') and time.time() >= retry.get('next_attempt_at', 0)
