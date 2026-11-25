"""PostgreSQL control plane, separate from the three archived world tables."""
import hashlib
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
from threading import Lock
from zoneinfo import ZoneInfo
from psycopg2.extras import Json, RealDictCursor


def public_numbers(value):
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: public_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_numbers(item) for item in value]
    return value


class BudgetExceeded(RuntimeError):
    def __init__(self, message='后台调用预算已用完，用户对话不受影响', retry_at=None, code='budget_wait',
                 budget_revision=0):
        super().__init__(message)
        self.code = code
        self.retry_at = retry_at
        self.budget_revision = budget_revision


class ServiceStore:
    BACKGROUND_CHAT = ('idle_evolve', 'memory_encode', 'proactive', 'startup_scene', 'graph_consolidation')
    QUOTA_KEYS = ('chat', 'embedding', 'tts', 'memory', 'per_minute')

    def effective_limits(self):
        """Legacy totals are configuration inputs, never foreground caps."""
        return self.background_limits()

    @staticmethod
    def day_window():
        start = datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    def background_limits(self):
        return self.quota_status()['limits']

    def _default_quotas(self):
        defaults = {**self.limits, 'chat': self.background_chat_limit()}
        return {
            **{key: max(0, int(os.getenv(f'EMBER_BACKGROUND_{key.upper()}_LIMIT', str(value))))
               for key, value in defaults.items()},
            'memory': max(0, int(os.getenv('EMBER_MEMORY_DAILY_LIMIT', '50'))),
            'per_minute': max(1, int(os.getenv('EMBER_BACKGROUND_PER_MINUTE', '12'))),
        }

    def _quota_status(self, cursor):
        cursor.execute(f'SELECT quotas, revision FROM {self.s}.background_quotas WHERE id=1')
        record = cursor.fetchone()
        revision = record['revision'] if record else 0
        quotas = self._default_quotas()
        if record:
            quotas.update(record['quotas'])
        return {'background_quotas': quotas, 'quota_revision': revision,
                'limits': {key: quotas[key] for key in self.limits}, 'background_chat_limit': quotas['chat']}

    def quota_status(self):
        with self.cursor() as c:
            return self._quota_status(c)

    def update_background_quotas(self, changes):
        if not changes or changes.keys() - set(self.QUOTA_KEYS):
            raise ValueError('请选择有效的后台限额项目')
        if any(value is not None and (type(value) is not int or not 0 <= value <= 2147483647)
               for value in changes.values()):
            raise ValueError('限额必须是 0 至 2147483647 的整数，或设为不限额')
        with self.cursor() as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (self.s + '.usage',))
            c.execute(f'''INSERT INTO {self.s}.background_quotas AS saved (id,quotas,revision)
                VALUES(1,%s,1) ON CONFLICT(id) DO UPDATE
                SET quotas=saved.quotas || EXCLUDED.quotas, revision=saved.revision+1
                WHERE saved.quotas IS DISTINCT FROM saved.quotas || EXCLUDED.quotas''', (Json(changes),))
            current = self._quota_status(c)
        self._observe_quota_revision(current['quota_revision'])
        return current

    def _observe_quota_revision(self, revision):
        with self._quota_lock:
            self.budget_revision = max(self.budget_revision, revision)

    def background_chat_limit(self):
        base = self.limits.get('chat', 0)
        return base if base <= 1 else max(0, base - max(1, (base + 3) // 4))

    def __init__(self, connection=None, schema='ember_service', limits=None):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', schema):
            raise ValueError('Invalid service schema')
        if connection is None:
            from memory.postgres import connection
        self.connection, self.s = connection, schema
        self.limits = limits or {'chat': 200, 'embedding': 200, 'tts': 200}
        self._quota_lock = Lock()
        self.budget_revision = 0

    @contextmanager
    def cursor(self):
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur

    def initialize(self):
        with self.cursor() as c:
            c.execute(f'''CREATE SCHEMA IF NOT EXISTS {self.s};
                CREATE TABLE IF NOT EXISTS {self.s}.sessions (
                    id text PRIMARY KEY, token_hash text UNIQUE NOT NULL, credential text NOT NULL,
                    label text NOT NULL, created_at timestamptz DEFAULT now(), last_seen timestamptz DEFAULT now(),
                    expires_at timestamptz NOT NULL, revoked boolean NOT NULL DEFAULT false);
                CREATE TABLE IF NOT EXISTS {self.s}.login_attempts (
                    peer text PRIMARY KEY, started_at timestamptz NOT NULL DEFAULT now(), attempts int NOT NULL);
                CREATE TABLE IF NOT EXISTS {self.s}.jobs (
                    id text PRIMARY KEY, dedupe text UNIQUE NOT NULL, kind text NOT NULL, payload jsonb NOT NULL,
                    status text NOT NULL DEFAULT 'pending', run_after timestamptz NOT NULL DEFAULT now(),
                    lease_until timestamptz, owner text, attempts int NOT NULL DEFAULT 0,
                    result jsonb, updated_at timestamptz NOT NULL DEFAULT now());
                CREATE TABLE IF NOT EXISTS {self.s}.usage (
                    id text PRIMARY KEY, kind text NOT NULL, model text NOT NULL, call_type text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(), status text NOT NULL DEFAULT 'pending',
                    input_tokens bigint NOT NULL DEFAULT 0, output_tokens bigint NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS usage_created_at ON {self.s}.usage(created_at);
                CREATE INDEX IF NOT EXISTS jobs_due ON {self.s}.jobs(status,run_after);
                ALTER TABLE {self.s}.usage ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'legacy';
                ALTER TABLE {self.s}.usage ADD COLUMN IF NOT EXISTS task_id text NOT NULL DEFAULT '';
                ALTER TABLE {self.s}.usage ADD COLUMN IF NOT EXISTS error_code text NOT NULL DEFAULT '';
                ALTER TABLE {self.s}.usage ADD COLUMN IF NOT EXISTS finished_at timestamptz;
                ALTER TABLE {self.s}.usage ADD COLUMN IF NOT EXISTS tokens_known boolean NOT NULL DEFAULT false;
                ALTER TABLE {self.s}.usage ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT '';
                CREATE TABLE IF NOT EXISTS {self.s}.circuits (
                    kind text NOT NULL, model text NOT NULL, provider text NOT NULL,
                    failures int NOT NULL DEFAULT 0, blocked_until timestamptz,
                    probe_until timestamptz, PRIMARY KEY(kind,model,provider));
                CREATE TABLE IF NOT EXISTS {self.s}.usage_blocks (
                    id text PRIMARY KEY, kind text NOT NULL, call_type text NOT NULL,
                    task_id text NOT NULL, reason text NOT NULL, created_at timestamptz DEFAULT now());
                CREATE TABLE IF NOT EXISTS {self.s}.background_quotas (
                    id int PRIMARY KEY CHECK(id=1), quotas jsonb NOT NULL, revision bigint NOT NULL);''')
        self._observe_quota_revision(self.quota_status()['quota_revision'])

    def check_ready(self):
        """Probe a fresh transaction; initialization flags can outlive a DB outage."""
        with self.cursor() as c:
            c.execute('SELECT 1')
            c.fetchone()

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self, token, credential, label, lifetime):
        with self.cursor() as c:
            c.execute(f'''INSERT INTO {self.s}.sessions(id,token_hash,credential,label,expires_at)
                VALUES(%s,%s,%s,%s,now()+(%s * interval '1 second')) RETURNING id''',
                (str(uuid.uuid4()), self.digest(token), credential, label[:80], lifetime))
            return c.fetchone()['id']

    def session(self, token, credential):
        if not token or len(token) > 200:
            return None
        with self.cursor() as c:
            c.execute(f'''SELECT id,label,expires_at FROM {self.s}.sessions
                WHERE token_hash=%s AND credential=%s AND NOT revoked AND expires_at>now()''', (self.digest(token), credential))
            result = c.fetchone()
            if result:
                c.execute(f'''UPDATE {self.s}.sessions SET last_seen=now()
                    WHERE id=%s AND last_seen<now()-interval '1 minute' ''', (result['id'],))
            return dict(result) if result else None

    def devices(self, credential):
        with self.cursor() as c:
            c.execute(f'''SELECT id,label,created_at,last_seen,expires_at FROM {self.s}.sessions
                WHERE credential=%s AND NOT revoked AND expires_at>now() ORDER BY created_at DESC''', (credential,))
            return [dict(x) for x in c.fetchall()]

    def revoke(self, key):
        with self.cursor() as c:
            c.execute(f'UPDATE {self.s}.sessions SET revoked=true WHERE id=%s', (key,))

    def login_allowed(self, peer):
        key = self.digest(peer or 'unknown')
        with self.cursor() as c:
            c.execute(f'''INSERT INTO {self.s}.login_attempts(peer,attempts) VALUES(%s,1)
                ON CONFLICT(peer) DO UPDATE SET attempts=CASE
                    WHEN {self.s}.login_attempts.started_at<now()-interval '5 minutes' THEN 1
                    ELSE {self.s}.login_attempts.attempts+1 END,
                started_at=CASE WHEN {self.s}.login_attempts.started_at<now()-interval '5 minutes'
                    THEN now() ELSE {self.s}.login_attempts.started_at END RETURNING attempts''', (key,))
            return c.fetchone()['attempts'] <= 10

    def enqueue(self, kind, dedupe, payload=None, delay=0):
        with self.cursor() as c:
            c.execute(f'''INSERT INTO {self.s}.jobs(id,kind,dedupe,payload,run_after)
                VALUES(%s,%s,%s,%s,now()+(%s*interval '1 second')) ON CONFLICT(dedupe) DO NOTHING''',
                (str(uuid.uuid4()), kind, dedupe, Json(payload or {}), delay))
            c.execute(f'SELECT * FROM {self.s}.jobs WHERE dedupe=%s', (dedupe,))
            return dict(c.fetchone())

    def claim(self, owner, lease_seconds=600):
        with self.cursor() as c:
            c.execute(f'''UPDATE {self.s}.jobs SET status=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END,
                owner=NULL,lease_until=NULL WHERE status='running' AND lease_until<now()''')
            c.execute(f'''SELECT id FROM {self.s}.jobs WHERE status='pending' AND run_after<=now()
                ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1''')
            row = c.fetchone()
            if not row:
                return None
            c.execute(f'''UPDATE {self.s}.jobs SET status='running',owner=%s,attempts=attempts+1,
                lease_until=now()+(%s*interval '1 second'),updated_at=now() WHERE id=%s RETURNING *''',
                (owner, lease_seconds, row['id']))
            return dict(c.fetchone())

    def complete(self, key, owner, result):
        with self.cursor() as c:
            c.execute(f'''UPDATE {self.s}.jobs SET status='completed',result=%s,lease_until=NULL,updated_at=now()
                WHERE id=%s AND owner=%s AND status='running' AND lease_until>now()''', (Json(result), key, owner))
            return c.rowcount == 1

    def retry(self, key, owner, reason):
        with self.cursor() as c:
            c.execute(f'''UPDATE {self.s}.jobs SET status=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END,
                result=%s,run_after=now()+interval '1 minute',lease_until=NULL,owner=NULL,updated_at=now()
                WHERE id=%s AND owner=%s AND status='running' ''', (Json({'error': reason[:200]}), key, owner))

    def reserve(self, kind, model='', call_type='', *, origin='background', task_id='', provider=''):
        if kind not in self.limits:
            raise ValueError('Unknown external call kind')
        if origin not in ('foreground', 'background'):
            raise ValueError('Invalid call origin')
        start, end = self.day_window()
        blocked, retry_at, code = None, end.timestamp(), 'budget_wait'
        revision = 0
        with self.cursor() as c:
            # Reservations count even if the network fails: billing may be uncertain.
            if origin == 'background':
                c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (self.s + '.usage',))
                status = self._quota_status(c)
                quotas, revision = status['background_quotas'], status['quota_revision']
                if quotas[kind] is not None:
                    # Old chat provenance is recoverable by its known background
                    # types. Old embeddings/TTS stay conservatively background.
                    predicate = "(origin='background' OR (origin='legacy' AND (kind<>'chat' OR call_type=ANY(%s))))"
                    c.execute(f'''SELECT count(*) AS n FROM {self.s}.usage WHERE kind=%s
                        AND status<>'not_sent' AND created_at>=%s AND created_at<%s AND {predicate}''',
                        (kind, start, end, list(self.BACKGROUND_CHAT)))
                    if c.fetchone()['n'] >= quotas[kind]:
                        blocked = '后台调用预算已用完，用户对话不受影响'
                if kind == 'chat' and call_type == 'memory_encode' and not blocked and quotas['memory'] is not None:
                    c.execute(f'''SELECT count(*) AS n FROM {self.s}.usage WHERE kind='chat'
                        AND status<>'not_sent' AND call_type='memory_encode' AND origin<>'foreground' AND created_at>=%s AND created_at<%s''', (start, end))
                    if c.fetchone()['n'] >= quotas['memory']:
                        blocked = '记忆整理今日预算已用完，原始资料保留等待次日'
                if not blocked and quotas['per_minute'] is not None:
                    c.execute(f'''SELECT count(*) AS n FROM {self.s}.usage WHERE kind=%s
                        AND status<>'not_sent' AND origin='background' AND created_at>now()-interval '1 minute' ''', (kind,))
                    if c.fetchone()['n'] >= quotas['per_minute']:
                        blocked, retry_at = '后台请求过于频繁，稍后继续', datetime.now(timezone.utc).timestamp() + 60
                if not blocked:
                    c.execute(f'SELECT * FROM {self.s}.circuits WHERE kind=%s AND model=%s AND provider=%s FOR UPDATE', (kind, model, provider))
                    circuit = c.fetchone()
                    if circuit and circuit['failures'] >= 3:
                        until = max(x for x in (circuit['blocked_until'], circuit['probe_until'], datetime.now(timezone.utc)) if x is not None)
                        if until > datetime.now(timezone.utc):
                            blocked, retry_at, code = '后台模型连续失败，通道暂时暂停', until.timestamp(), 'circuit_wait'
                        else:
                            c.execute(f'''UPDATE {self.s}.circuits SET probe_until=now()+interval '120 seconds'
                                WHERE kind=%s AND model=%s AND provider=%s''', (kind, model, provider))
            if blocked:
                c.execute(f'''INSERT INTO {self.s}.usage_blocks(id,kind,call_type,task_id,reason)
                    VALUES(%s,%s,%s,%s,%s)''', (str(uuid.uuid4()), kind, call_type, task_id, code))
            else:
                key = str(uuid.uuid4())
                c.execute(f'''INSERT INTO {self.s}.usage(id,kind,model,call_type,origin,task_id,provider)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)''', (key, kind, model, call_type, origin, task_id, provider))
        if blocked:
            raise BudgetExceeded(blocked, retry_at, code, revision)
        return key

    def finish_usage(self, key, status='completed', usage=None, error_code=''):
        def tokens(name, alternative):
            value = getattr(usage, name, None) if usage is not None else None
            if value is None and usage is not None:
                value = getattr(usage, alternative, 0)
            try:
                return max(0, int(value or 0))
            except (ValueError, TypeError):
                return 0
        with self.cursor() as c:
            c.execute(f'''UPDATE {self.s}.usage SET status=%s,input_tokens=%s,output_tokens=%s,
                tokens_known=%s,error_code=%s,finished_at=now()
                WHERE id=%s RETURNING kind,model,provider,origin''', (status, tokens('prompt_tokens', 'prompt_token_count'),
                tokens('completion_tokens', 'candidates_token_count'), usage is not None, error_code, key))
            row = c.fetchone()
            if row and row['origin'] == 'background':
                values = (row['kind'], row['model'], row['provider'])
                if status in ('completed', 'incomplete'):
                    c.execute(f'DELETE FROM {self.s}.circuits WHERE kind=%s AND model=%s AND provider=%s', values)
                elif status == 'failed':
                    permanent = error_code in ('authentication', 'configuration')
                    c.execute(f'''INSERT INTO {self.s}.circuits(kind,model,provider,failures)
                        VALUES(%s,%s,%s,%s) ON CONFLICT(kind,model,provider) DO UPDATE
                        SET failures={self.s}.circuits.failures+%s''', (*values, 3 if permanent else 1, 3 if permanent else 1))
                    c.execute(f'''UPDATE {self.s}.circuits SET blocked_until=now()+(%s*interval '1 second'),probe_until=NULL
                        WHERE kind=%s AND model=%s AND provider=%s AND failures>=3''', (315360000 if permanent else 900, *values))

    def snapshot(self):
        start, end = self.day_window()
        with self.cursor() as c:
            c.execute(f'''SELECT kind,count(*) AS requests,sum(input_tokens) AS input_tokens,
                sum(output_tokens) AS output_tokens FROM {self.s}.usage
                WHERE status<>'not_sent' AND created_at>=%s AND created_at<%s GROUP BY kind''', (start, end))
            usage = {x['kind']: dict(x) for x in c.fetchall()}
            c.execute(f'''SELECT origin,kind,call_type,status,count(*) AS requests,
                sum(input_tokens) AS input_tokens,sum(output_tokens) AS output_tokens,
                count(*) FILTER (WHERE NOT tokens_known AND status<>'not_sent') AS unknown_usage,
                round(avg(extract(epoch FROM finished_at-created_at))*1000) AS duration_ms
                FROM {self.s}.usage WHERE created_at>=%s AND created_at<%s
                GROUP BY origin,kind,call_type,status ORDER BY origin,kind,call_type,status''', (start, end))
            breakdown = [dict(x) for x in c.fetchall()]
            c.execute(f'''SELECT kind,count(*) AS requests FROM {self.s}.usage WHERE created_at>=%s AND created_at<%s
                AND status<>'not_sent' AND (origin='background' OR (origin='legacy' AND (kind<>'chat' OR call_type=ANY(%s)))) GROUP BY kind''',
                (start, end, list(self.BACKGROUND_CHAT)))
            background_usage = {x['kind']: dict(x) for x in c.fetchall()}
            c.execute(f'''SELECT id,kind,status,updated_at,result FROM {self.s}.jobs
                ORDER BY updated_at DESC LIMIT 5''')
            jobs = [dict(x) for x in c.fetchall()]
            quotas = self._quota_status(c)
        self._observe_quota_revision(quotas['quota_revision'])
        return public_numbers({'enabled': True, 'foreground_unlimited': True, **quotas, 'background_usage': background_usage,
            'reset_at': end.isoformat(), 'breakdown': breakdown, 'usage': usage, 'jobs': jobs,
            'measured_at': datetime.now(timezone.utc).isoformat()})
