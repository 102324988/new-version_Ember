"""Trusted task provenance survives nested calls; never read it from client JSON."""
from contextlib import contextmanager
from contextvars import ContextVar

_store = None
_source = ContextVar('ember_call_source', default=('background', ''))


def current_source():
    return _source.get()


@contextmanager
def call_scope(origin='background', task_id=''):
    if origin not in ('foreground', 'background'):
        raise ValueError('Invalid call origin')
    token = _source.set((origin, str(task_id)))
    try:
        yield
    finally:
        _source.reset(token)


def install(store):
    global _store
    _store = store


def current_budget_revision():
    revision = getattr(_store, 'budget_revision', 0)
    return revision if type(revision) is int else 0


def reserve(kind, model='', call_type='', provider=''):
    origin, task_id = current_source()
    return (_store, _store.reserve(kind, model, call_type, origin=origin, task_id=task_id, provider=provider)) if _store else None


def finish(ticket, usage=None, status='completed', error_code=''):
    if ticket:
        # A durable reservation already limits requests even if accounting fails.
        try:
            ticket[0].finish_usage(ticket[1], status, usage, error_code=error_code)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('服务用量回执保存失败；保留原始待核对记录')
