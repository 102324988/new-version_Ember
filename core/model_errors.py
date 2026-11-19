"""Small, public-safe failure contract shared by model and memory workers."""
import time


class ModelCallError(RuntimeError):
    def __init__(self, code, retry_at=None):
        super().__init__(code)
        self.code, self.retry_at = code, retry_at


def cause_types(error):
    names, seen = [], set()
    while error is not None and id(error) not in seen and len(names) < 6:
        seen.add(id(error))
        names.append(type(error).__name__)
        error = error.__cause__ or error.__context__
    return '/'.join(names)


def classify(error):
    if isinstance(error, ModelCallError) or type(error).__name__ == 'BudgetExceeded':
        return error.code, getattr(error, 'retry_at', None)
    name, status = type(error).__name__, getattr(error, 'status_code', None)
    code = 'unavailable'
    if isinstance(error, TimeoutError) or 'Timeout' in name:
        code = 'timeout'
    elif 'Connection' in name:
        code = 'connection'
    elif status == 429 or name == 'RateLimitError':
        code = 'rate_limit'
    elif status in (401, 403) or name in ('AuthenticationError', 'PermissionDeniedError'):
        code = 'authentication'
    elif status == 404 or name == 'NotFoundError':
        code = 'configuration'
    elif status in (400, 413, 422):
        # Inspect the provider's structured code, never expose its body.
        value = getattr(error, 'code', '')
        code = 'context_limit' if value in ('context_length_exceeded', 'max_tokens_exceeded') or status == 413 else 'configuration'
    elif isinstance(error, ValueError):
        code = 'invalid_result'
    elif name in ('OperationalError', 'InterfaceError', 'DatabaseError'):
        code = 'database'
    retry_at = None
    try:
        raw = getattr(getattr(error, 'response', None), 'headers', {}).get('retry-after')
        if raw is not None:
            retry_at = time.time() + min(86400, max(1, float(raw)))
    except (ValueError, TypeError, AttributeError):
        pass
    return code, retry_at
