"""Read-only probe. Never import the app, emit exception text or call a model."""
import argparse
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
from collections import deque
from urllib.parse import urlsplit


def check(status, name, detail, fix=''):
    return dict(status=status, check=name, detail=detail, fix=fix)


def database_failure(error):
    code = getattr(error, 'pgcode', '') or ''
    # libpq connection authentication errors may have no pgcode. Inspect only
    # in memory; never include the exception or connection string in reports.
    text = str(error).lower()
    if code.startswith('28') or 'password authentication failed' in text:
        return check('FAIL', 'database', '数据库认证失败。', '核对实际数据库密码与配置；容器已有数据时，仅修改环境变量不会修改用户密码。')
    if code == '3D000' or 'does not exist' in text:
        return check('FAIL', 'database', '目标数据库不存在。', '核对 PG_DB；保留现有卷，不要通过删除数据库重新初始化。')
    return check('FAIL', 'database', '数据库连接或只读查询失败。', '检查 PostgreSQL 是否运行、地址端口及网络；查询有超时限制，查看服务端具体原因。')


def probe(network=False, local=False, config_file='.env'):
    results = []
    results.append(check('PASS' if sys.version_info >= (3, 11) else 'FAIL', 'python',
        'Python ' + '.'.join(map(str, sys.version_info[:3])), '需要 Python 3.11+；容器模式使用镜像内 Python。'))
    if local:
        try:
            from dotenv import load_dotenv
            load_dotenv(config_file, override=False)
        except ImportError:
            results.append(check('FAIL', 'dotenv', '缺少 python-dotenv。', '在实际启动的 Python 环境安装 requirements.txt。'))
    for module, distribution in [('fastapi', 'fastapi'), ('openai', 'openai'), ('psycopg2', 'psycopg2-binary'),
                                  ('pgvector', 'pgvector'), ('yaml', 'PyYAML'), ('edge_tts', 'edge-tts')]:
        try:
            available = importlib.util.find_spec(module) is not None
            version = importlib.metadata.version(distribution) if available else 'missing'
            valid = available
            if module == 'edge_tts' and available:
                numbers = tuple(int(x) for x in re.findall(r'\d+', version)[:3])
                valid = (7, 3, 1) <= numbers < (8,)
            results.append(check('PASS' if valid else 'FAIL', 'dependency:' + distribution, version,
                '在实际运行环境安装 requirements.txt；语音要求 edge-tts>=7.3.1,<8。' if not valid else ''))
        except Exception:
            results.append(check('FAIL', 'dependency:' + distribution, '依赖不可用或元数据损坏。', '修复实际启动环境的依赖，容器模式可重新构建应用镜像。'))
    tested = {}
    for prefix in ('LARGE_LLM', 'SMALL_LLM', 'EMBEDDING'):
        missing = [prefix + '_' + name for name in ('API_KEY', 'BASE_URL', 'MODEL')
                   if not os.getenv(prefix + '_' + name, '').strip() or os.getenv(prefix + '_' + name, '').lower().startswith('your_')]
        if missing:
            results.append(check('FAIL', prefix, '配置未填写：' + ', '.join(missing), '在实际使用的 .env 或 deploy/service.env 填写后重启后端。'))
            continue
        try:
            url = urlsplit(os.environ[prefix + '_BASE_URL'])
            if url.scheme not in ('http', 'https') or not url.hostname:
                raise ValueError()
            port = url.port or (443 if url.scheme == 'https' else 80)
            results.append(check('PASS', prefix, '配置字段齐全；未发送密钥或生成请求。'))
            if network:
                key = (url.scheme, url.hostname, port)
                if key not in tested:
                    try:
                        with socket.create_connection((url.hostname, port), timeout=5) as connection:
                            if url.scheme == 'https':
                                with ssl.create_default_context().wrap_socket(connection, server_hostname=url.hostname):
                                    pass
                        tested[key] = True
                    except Exception:
                        tested[key] = False
                results.append(check('PASS' if tested[key] else 'FAIL', prefix + ':network',
                    '端点直连 TCP/TLS 可达。' if tested[key] else '端点直连 DNS、连接或 TLS 检查失败。',
                    '检查网络、证书及端点配置；直连不使用 HTTP 代理，也不验证密钥、模型权限或额度。'))
        except (ValueError, TypeError):
            results.append(check('FAIL', prefix + ':url', '接口地址格式无效。', '使用完整 http:// 或 https:// 接口地址。'))
    for relative, expected in [('config/state.json', dict), ('config/chat_memory.json', list),
                               ('config/experience.json', dict), ('config/working_memory.json', dict),
                               ('data/state/proactive.json', dict)]:
        path = Path(relative)
        if not path.exists():
            results.append(check('WARN', relative, '尚无运行记录，首次启动可能正常。', '已有世界却缺失文件时，检查运行目录和数据卷；不要覆盖现有备份。'))
            continue
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, expected):
                raise ValueError()
            results.append(check('PASS', relative, 'JSON 可读取，未导出内容。'))
        except (ValueError, OSError):
            results.append(check('FAIL', relative, '文件损坏、类型错误或不可读。', '先保留原文件，再从已知有效存档恢复；不要直接清空聊天或状态。'))
    for folder in ('data/failed_writes', 'data/failed_memories'):
        try:
            count = len(list(Path(folder).glob('*.json')))
            results.append(check('WARN' if count else 'PASS', folder, '待处理记录数：' + str(count),
                '修复数据库或向量服务后等待自动重试；不要删除失败记录。' if count else ''))
        except OSError:
            results.append(check('FAIL', folder, '无法读取待办目录。', '检查数据卷访问权限。'))
    logfile = Path('data/logs/system.log')
    if logfile.exists():
        try:
            with logfile.open(encoding='utf-8', errors='replace') as stream:
                recent = ''.join(deque(stream, maxlen=300))
            for name, pattern, detail in (
                ('log:budget', r'BudgetExceeded|调用额度已用完|预算已用完|code=budget_wait', '应用日志出现预算拦截，结合今日后台 budget 检查结果判断。'),
                ('log:model-timeout', r'OneChat Error.*(?:timed out|timeout)|APITimeoutError|code=timeout', '应用日志出现模型请求超时。'),
                ('log:model-connection', r'code=connection|APIConnectionError', '应用日志出现模型连接失败。')):
                if re.search(pattern, recent, re.I):
                    results.append(check('WARN', name, detail, '可能是历史故障；不会导出日志原文。检查用量、后台重试或网络与代理。'))
        except OSError:
            results.append(check('WARN', 'application-log', '无法读取应用日志。', '检查日志目录权限。'))
    try:
        import psycopg2
        connection = psycopg2.connect(host=os.getenv('PG_HOST', '127.0.0.1'), port=os.getenv('PG_PORT', '5433'),
            user=os.getenv('PG_USER', 'postgres'), password=os.getenv('PG_PASSWORD', ''), dbname=os.getenv('PG_DB', 'ember_db'),
            connect_timeout=4, options='-c default_transaction_read_only=on -c statement_timeout=5000')
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')")
                vector = cursor.fetchone()[0]
                results.append(check('PASS' if vector else 'FAIL', 'pgvector', 'vector 扩展已安装。' if vector else '缺少 vector 扩展。',
                    '使用 pgvector 镜像；由管理员在目标库创建 vector 扩展后重新启动后端。' if not vector else ''))
                for table in ('message_list', 'state_list', 'episodic_memory'):
                    cursor.execute('SELECT to_regclass(%s) IS NOT NULL', ('public.' + table,))
                    exists = cursor.fetchone()[0]
                    results.append(check('PASS' if exists else 'FAIL', 'table:' + table,
                        '业务表存在。' if exists else '缺少业务表。', '检查是否连接正确数据库及初始化错误；不要清空现有表。' if not exists else ''))
                cursor.execute("SELECT to_regclass('ember_service.usage') IS NOT NULL")
                if cursor.fetchone()[0]:
                    cursor.execute("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='ember_service' AND table_name='usage' AND column_name='origin')")
                    unlimited = cursor.fetchone()[0]
                    cursor.execute("""SELECT kind,count(*) FROM ember_service.usage WHERE
                        created_at>=date_trunc('day',now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai' """ +
                        ("AND status<>'not_sent' AND (origin='background' OR (origin='legacy' AND (kind<>'chat' OR call_type IN ('idle_evolve','memory_encode','proactive','startup_scene','graph_consolidation')))) " if unlimited else '') + "GROUP BY kind")
                    usage = dict(cursor.fetchall())
                    results.append(check('PASS' if unlimited else 'WARN', 'foreground-budget',
                        '用户对话不设应用侧每日额度，用量继续记录。' if unlimited else '运行版本仍使用旧额度规则。',
                        '' if unlimited else '更新本机服务以启用前台不限额规则。'))
                    for kind in ('chat', 'embedding', 'tts'):
                        limit = int(os.getenv('EMBER_DAILY_' + kind.upper() + '_LIMIT', '200'))
                        if unlimited:
                            default = max(0, limit - max(1, (limit+3)//4)) if kind == 'chat' else limit
                            limit = int(os.getenv('EMBER_BACKGROUND_' + kind.upper() + '_LIMIT', str(default)))
                        count = usage.get(kind, 0)
                        results.append(check(('WARN' if unlimited else 'FAIL') if count >= limit else 'PASS', 'budget:' + kind,
                            ('后台今日调用：' if unlimited else '今日调用：') + str(count) + '/' + str(limit),
                            '后台预算暂停，用户对话继续；保留待办等待恢复。' if count >= limit and unlimited else
                            '应用每日额度已用完，请核对旧版配置。' if count >= limit else ''))
            results.append(check('PASS', 'database', '认证与只读查询成功；未读取消息正文。'))
        finally:
            connection.close()
    except ImportError:
        results.append(check('FAIL', 'database', '缺少数据库驱动，未连接。', '安装 requirements.txt。'))
    except Exception as error:
        results.append(database_failure(error))
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--local', action='store_true')
    parser.add_argument('--network', action='store_true')
    parser.add_argument('--config', default='.env')
    arguments = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        print(json.dumps(probe(arguments.network, arguments.local, arguments.config), ensure_ascii=False))
    except Exception:
        print(json.dumps([check('FAIL', 'probe', '诊断探针未完成。', '检查运行环境或使用容器模式。')], ensure_ascii=False))
        raise SystemExit(1)
