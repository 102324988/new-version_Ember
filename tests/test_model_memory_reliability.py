"""Budgets, provenance and durable memory stages, with synthetic providers only."""
import asyncio
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch, AsyncMock

import psycopg2
import httpcore
import httpx
import pytest

from brain.llm_client import LLMClient
from brain.tts import TTSManager
from config.settings import settings
from core.event_bus import Event
from core.model_errors import ModelCallError, classify
from core.service_policy import call_scope, current_source, install
from memory.batching import make_batch, estimated_tokens, SOURCE_RULES
from memory.retry_policy import next_retry, retry_ready
from memory.working_memory import WorkingMemory
from memory.postgres import connection
from service.store import BudgetExceeded, ServiceStore
from tests.test_memory_pipeline import harness, add
from tests.test_service import store
from tests.test_turn_cancellation import make_brain
from tests.test_restore_experience import make_server
from fastapi.testclient import TestClient


def drain(hippo, consumer):
    assert hippo.wait_idle(5)
    if consumer:
        consumer.store_queue.join()


def test_foreground_exceeds_300_and_does_not_spend_background_budget(store, monkeypatch):
    monkeypatch.setenv('EMBER_BACKGROUND_CHAT_LIMIT', '1')
    for _ in range(305):
        store.reserve('chat', call_type='state_update', origin='foreground', task_id='synthetic-turn')
    store.reserve('chat', call_type='state_update')
    with pytest.raises(BudgetExceeded):
        store.reserve('chat', call_type='dialogue')  # A purpose name is not provenance.
    snapshot = store.snapshot()
    assert snapshot['foreground_unlimited'] and snapshot['background_usage']['chat']['requests'] == 1
    assert snapshot['usage']['chat']['requests'] == 306
    with store.cursor() as cursor:
        cursor.execute(f'SELECT count(*) AS n FROM {store.s}.usage_blocks')
        assert cursor.fetchone()['n'] == 1


def test_live_usage_snapshot_is_json_serializable_including_database_aggregates(store):
    key = store.reserve('chat', origin='foreground')
    store.finish_usage(key, usage=SimpleNamespace(prompt_tokens=12,completion_tokens=3))
    payload = json.loads(json.dumps(store.snapshot()))
    assert payload['usage']['chat']['input_tokens'] == 12
    assert payload['breakdown'][0]['duration_ms'] is not None


def test_not_sent_request_is_audited_without_spending_budget(store):
    key = store.reserve('chat')
    store.finish_usage(key, 'not_sent', error_code='background_busy')
    store.reserve('chat')
    assert store.snapshot()['usage']['chat']['requests'] == 1


def test_background_rate_limit_does_not_throttle_foreground(store, monkeypatch):
    monkeypatch.setenv('EMBER_BACKGROUND_CHAT_LIMIT', '20')
    monkeypatch.setenv('EMBER_BACKGROUND_PER_MINUTE', '1')
    store.reserve('chat')
    with pytest.raises(BudgetExceeded) as failure: store.reserve('chat')
    assert failure.value.retry_at < time.time()+70
    store.reserve('chat', origin='foreground')


def test_scope_is_nested_and_ordinary_threads_do_not_inherit_it(store):
    install(store)
    with call_scope('foreground', 'user-turn'):
        assert current_source() == ('foreground', 'user-turn')
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(current_source).result()[0] == 'background'
        with call_scope('background', 'encode'):
            assert current_source()[0] == 'background'
        assert current_source()[0] == 'foreground'
    assert current_source()[0] == 'background'


def test_foreground_query_embedding_and_speech_ignore_background_zero(store, monkeypatch):
    install(store)
    for kind in ('CHAT', 'EMBEDDING', 'TTS'):
        monkeypatch.setenv('EMBER_BACKGROUND_' + kind + '_LIMIT', '0')
    client = object.__new__(LLMClient)
    client.embedding_client = Mock()
    client.embedding_client.embeddings.create.return_value = SimpleNamespace(data=[SimpleNamespace(embedding=[0.1]*1536)], usage=None)
    voice = TTSManager()
    voice._do_tts = AsyncMock(return_value=b'synthetic-audio')
    with call_scope('foreground', 'user-query'):
        assert client.get_embedding(settings.EMBEDDING_MODEL, 'synthetic query')
        assert asyncio.run(voice.generate_base64('合成回复'))
    assert store.snapshot()['background_usage'] == {}


def test_recall_executor_preserves_foreground_origin(store, monkeypatch):
    bus, memory, consumer, hippo, _ = harness()
    try:
        monkeypatch.setenv('EMBER_BACKGROUND_EMBEDDING_LIMIT', '0')
        client = object.__new__(LLMClient)
        client.embedding_client = Mock()
        client.embedding_client.embeddings.create.return_value = SimpleNamespace(data=[SimpleNamespace(embedding=[.1]*1536)], usage=None)
        consumer.llm_client = client
        install(store)
        with call_scope('foreground', 'query-turn'):
            hippo.query_memory('合成检索', ['检索'])
        assert store.snapshot()['usage']['embedding']['requests'] == 1
        assert not store.snapshot()['background_usage']
    finally:
        consumer.stop()


def test_background_circuit_is_persistent_and_has_one_probe(store, monkeypatch):
    monkeypatch.setenv('EMBER_BACKGROUND_CHAT_LIMIT', '30')
    for _ in range(3):
        key = store.reserve('chat', 'synthetic-model')
        store.finish_usage(key, 'failed', error_code='timeout')
    recovered = ServiceStore(schema=store.s, limits=store.limits)
    with pytest.raises(BudgetExceeded) as failure:
        recovered.reserve('chat', 'synthetic-model')
    assert failure.value.code == 'circuit_wait'
    store.reserve('chat', 'synthetic-model', origin='foreground')
    with store.cursor() as cursor:
        cursor.execute(f"UPDATE {store.s}.circuits SET blocked_until=now()-interval '1 second'")
    probe = store.reserve('chat', 'synthetic-model')
    with pytest.raises(BudgetExceeded):
        store.reserve('chat', 'synthetic-model')
    store.finish_usage(probe)
    store.reserve('chat', 'synthetic-model')


def test_provider_configuration_change_escapes_auth_circuit(store, monkeypatch):
    monkeypatch.setenv('EMBER_BACKGROUND_CHAT_LIMIT', '10')
    key = store.reserve('chat', 'model', provider='old-configuration')
    store.finish_usage(key, 'failed', error_code='authentication')
    with pytest.raises(BudgetExceeded): store.reserve('chat', 'model', provider='old-configuration')
    store.reserve('chat', 'model', provider='new-configuration')


def test_memory_subbudget_is_independent(store, monkeypatch):
    monkeypatch.setenv('EMBER_BACKGROUND_CHAT_LIMIT', '10')
    monkeypatch.setenv('EMBER_MEMORY_DAILY_LIMIT', '1')
    store.reserve('chat', call_type='memory_encode')
    with pytest.raises(BudgetExceeded): store.reserve('chat', call_type='memory_encode')
    store.reserve('chat', call_type='proactive')


def test_legacy_background_is_counted_without_rewriting_usage(store, monkeypatch):
    monkeypatch.setenv('EMBER_BACKGROUND_CHAT_LIMIT', '1')
    with store.cursor() as cursor:
        cursor.execute(f"INSERT INTO {store.s}.usage(id,kind,model,call_type) VALUES('legacy','chat','model','memory_encode')")
    with pytest.raises(BudgetExceeded): store.reserve('chat', call_type='idle_evolve')
    store.reserve('chat', origin='foreground')
    assert store.snapshot()['background_usage']['chat']['requests'] == 1


def test_budget_day_is_shanghai_even_if_database_session_uses_utc(store):
    start, end = store.day_window()
    assert start.hour == 0 and start.utcoffset().total_seconds() == 8*3600
    assert (end-start).total_seconds() == 86400
    with store.cursor() as cursor:
        cursor.execute(f"INSERT INTO {store.s}.usage(id,kind,model,call_type,created_at) VALUES('yesterday','chat','m','memory_encode',%s)", (start - __import__('datetime').timedelta(seconds=1),))
    assert store.snapshot()['usage'] == {}


def test_memory_retries_stop_after_three_and_budget_wait_does_not_increment():
    retry = None
    for _ in range(3): retry = next_retry(retry, 'timeout', config='a')
    assert retry['needs_attention'] and not retry_ready(retry, 'a')
    assert retry_ready(retry, 'b')
    waiting = next_retry(None, 'budget_wait', time.time()+86400)
    assert waiting['attempts'] == 0 and not waiting['needs_attention']
    assert not next_retry({'attempts':20}, 'budget_wait')['needs_attention']


def test_typed_error_contract_never_exposes_provider_text():
    error = type('AuthenticationError', (Exception,), {})('private-key-and-endpoint')
    assert classify(error)[0] == 'authentication'
    client = object.__new__(LLMClient)
    client.large_client = Mock()
    client.large_client.chat.completions.create.side_effect = TimeoutError('private-payload')
    with pytest.raises(ModelCallError) as failure:
        client.one_chat(settings.LARGE_LLM, [], call_type='memory_encode', raise_errors=True)
    assert str(failure.value) == 'timeout' and client.large_client.chat.completions.create.call_count == 1


def test_encoding_network_does_not_hold_world_operation_lock():
    bus, memory, consumer, hippo, model = harness()
    started, release = threading.Event(), threading.Event()
    original = model.one_chat.side_effect
    def slow(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    model.one_chat.side_effect = slow
    try:
        add(memory, '慢模型测试')
        bus.publish(Event('memory.preprocess', {}))
        assert started.wait(3)
        def enter_world():
            acquired = bus.operation_lock.acquire(timeout=.5)
            if acquired:
                bus.operation_lock.release()
            return acquired
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(enter_world).result()
    finally:
        release.set()
        drain(hippo, consumer)
        consumer.stop()


def test_old_generation_result_cannot_commit_after_restore():
    bus, memory, consumer, hippo, model = harness()
    started, release = threading.Event(), threading.Event()
    original = model.one_chat.side_effect
    def slow(*args, **kwargs):
        started.set(); assert release.wait(5)
        return original(*args, **kwargs)
    model.one_chat.side_effect = slow
    try:
        add(memory, '旧世界内容')
        bus.publish(Event('memory.preprocess', {})); assert started.wait(3)
        hippo.after_restore()
        release.set(); drain(hippo, consumer)
        assert not memory.working.snapshot()['jobs']
        assert Path('config/chat_history.log').read_text(encoding='utf-8')
    finally:
        release.set(); drain(hippo, consumer); consumer.stop()


def test_database_retry_reuses_vectors_persisted_in_outbox():
    bus, memory, consumer, hippo, model = harness()
    original = consumer._add_memory
    consumer._add_memory = Mock(side_effect=psycopg2.OperationalError('synthetic database outage'))
    try:
        add(memory, '请记住，合成用户喜欢桂花茶')
        bus.publish(Event('memory.preprocess', {})); drain(hippo, consumer)
        job = memory.working.snapshot()['jobs'][0]
        key = job['memories'][0]['memory_id']
        assert job['memories'][0]['_vector_cache']['embedding']
        before = consumer.llm_client.get_embedding.call_count
        assert before == 1
        consumer._add_memory = original
        hippo.control('retry', job['id'])
        bus.publish(Event('memory.preprocess', {})); drain(hippo, consumer)
        assert not memory.working.snapshot()['jobs']
        assert consumer.llm_client.get_embedding.call_count == before
        assert not (consumer.spool_dir / (key+'.json')).exists()
    finally:
        consumer.stop()


def test_long_source_is_fragmented_but_original_acknowledged_whole(monkeypatch):
    monkeypatch.setenv('EMBER_MEMORY_INPUT_BUDGET', '4000')
    row = {'role':'user', 'message_id':'long-source', 'content':'甲乙丙丁'*3000, 'timestamp':1793618400000}
    original = json.dumps(row, ensure_ascii=False) + '\n'
    text, contexts = make_batch(original, 'prompt', {})
    assert text == original and len(contexts) > 1
    parts = [json.loads(x['log']) for x in contexts]
    assert ''.join(x['content'] for x in parts) == row['content']
    assert all(x['message_id'] == 'long-source' for x in parts)
    assert all(estimated_tokens(json.dumps(x, ensure_ascii=False)) < 4000 for x in contexts)


@pytest.mark.parametrize('width', [800, 4000])
def test_escaped_journal_packs_final_envelope_without_consuming_later_rows(monkeypatch, width):
    monkeypatch.setenv('EMBER_MEMORY_INPUT_BUDGET', '4000')
    prompt = '合成整理提示'
    extras = {'buffered_sources': [], 'previous_notes': [], 'recent_dialogue': []}
    content = ('"\\\n' * width) + '\u2028仍然属于同一条'
    rows = [dict(role='user', message_id='escaped', content=content),
            dict(role='assistant', message_id='next', content='下一条保留')]
    first, second = [json.dumps(row, ensure_ascii=False) + '\n' for row in rows]
    prefix, contexts = make_batch(first + second, prompt, extras)
    assert prefix == first
    assert all(estimated_tokens(prompt + json.dumps(c, ensure_ascii=False)) <= 3500 for c in contexts)
    output = [json.loads(c['log']) for c in contexts]
    assert ''.join(row['content'] for row in output) == content
    assert all(row['message_id'] == 'escaped' for row in output)
    assert make_batch((first + second)[len(prefix):], prompt, extras)[0] == second


def test_two_individually_fitting_escaped_lines_leave_second_for_next_batch(monkeypatch):
    monkeypatch.setenv('EMBER_MEMORY_INPUT_BUDGET', '4000')
    lines = [json.dumps({'role': 'user', 'message_id': key, 'content': '"' * 625}) + '\n'
             for key in ('first', 'second')]
    prefix, contexts = make_batch(''.join(lines), 'prompt', {})
    assert prefix == lines[0]
    assert [context['log'] for context in contexts] == lines[:1]
    assert make_batch(lines[1], 'prompt', {})[0] == lines[1]


def test_blocked_checkpoint_does_not_scan_journal_and_fixed_config_can_resume(monkeypatch):
    bus, memory, consumer, hippo, _ = harness()
    try:
        config = hippo._encoding_config(settings.MEMORY_ENCODING_PROMPT + SOURCE_RULES)
        hippo.working.request()
        hippo.working.encoding_retry(True, 'input_budget', None, config)
        with patch.object(hippo, '_load_experience') as read, patch.object(hippo, '_on_preprocess_request') as enqueue:
            hippo._on_tick(Event('system.tick', {}))
            read.assert_not_called()
            enqueue.assert_not_called()
            monkeypatch.setenv('EMBER_MEMORY_INPUT_BUDGET', '16001')
            hippo._on_tick(Event('system.tick', {}))
            read.assert_not_called()
            enqueue.assert_called_once()
    finally:
        consumer.stop()


def test_optional_context_counts_all_keys_before_filling_budget(monkeypatch):
    monkeypatch.setenv('EMBER_MEMORY_INPUT_BUDGET','16000')
    text = json.dumps({'role':'user','message_id':'budget-source','content':'合成偏好'},ensure_ascii=False)+'\n'
    prompt = 'prompt'
    record = {'content':''}
    initial = {'log':text,'buffered_sources':[record]}
    room = 16000 - estimated_tokens(prompt) - estimated_tokens(json.dumps(initial,ensure_ascii=False))
    record['content'] = 'x'*room
    prefix,contexts = make_batch(text,prompt,{'buffered_sources':[record],'previous_notes':[],'recent_dialogue':[]})
    assert prefix == text
    assert set(contexts[0]) == {'log','buffered_sources','previous_notes','recent_dialogue'}
    assert contexts[0]['buffered_sources'] == []
    assert all(estimated_tokens(prompt)+estimated_tokens(json.dumps(x,ensure_ascii=False)) <= 16000-1024 for x in contexts)
    assert len(record['content']) == room  # Optional omission never edits the source.


def test_legacy_state_backfill_keeps_current_conversation_out_of_the_batch():
    text = json.dumps({'source_type': 'role_state', 'content': '过去的角色状态'}, ensure_ascii=False) + '\n'
    current = {'message_id': 'current-source', 'role': 'user', 'content': '当前对话原文'}
    extras = {'buffered_sources': [current], 'previous_notes': [{'quote': '已有中期事项'}], 'recent_dialogue': [current]}
    prefix, contexts = make_batch(text, 'prompt', extras)
    assert prefix == text
    assert all(context[key] == [] for context in contexts for key in extras)
    assert extras['buffered_sources'] == [current]
    new_log = json.dumps(current, ensure_ascii=False) + '\n'
    _, new_contexts = make_batch(new_log, 'prompt', extras)
    assert new_contexts[0]['buffered_sources'] == [current]


def test_input_budget_failure_is_distinct_from_model_output_failure(monkeypatch):
    monkeypatch.setenv('EMBER_MEMORY_INPUT_BUDGET','4000')
    with pytest.raises(ModelCallError) as failure:
        make_batch('legacy source\n','x'*5000,{})
    assert classify(failure.value)[0] == 'input_budget'
    assert next_retry(None,'input_budget')['needs_attention']


def test_truncated_memory_response_keeps_actual_usage_and_has_typed_error(store):
    install(store)
    client=object.__new__(LLMClient)
    client.large_client=Mock()
    client.large_client.chat.completions.create.return_value=SimpleNamespace(
        choices=[SimpleNamespace(finish_reason='length',message=SimpleNamespace(content='{"memories":['))],
        usage=SimpleNamespace(prompt_tokens=7,completion_tokens=16))
    with pytest.raises(ModelCallError,match='output_limit'):
        client.one_chat(settings.LARGE_LLM,[],call_type='memory_encode',raise_errors=True)
    snapshot=store.snapshot()
    assert snapshot['usage']['chat']['output_tokens']==16
    assert snapshot['breakdown'][0]['status']=='incomplete'
    assert client.large_client.chat.completions.create.call_count==1


def test_output_limit_splits_pending_batch_without_losing_sources():
    bus,memory,consumer,hippo,model=harness()
    try:
        add(memory,'第一条合成偏好')
        add(memory,'第二条合成偏好')
        original_text=Path('config/chat_history.log').read_text(encoding='utf-8')
        encode=model.one_chat.side_effect
        model.one_chat.side_effect=ModelCallError('output_limit')
        bus.publish(Event('memory.preprocess',{})); drain(hippo,consumer)
        batch=memory.working.snapshot()['encoding']
        assert batch['text']==original_text and len(batch['contexts'])==2 and batch['index']==0
        assert Path('config/chat_history.log').read_text(encoding='utf-8')==original_text
        model.one_chat.side_effect=encode
        hippo.control('retry')
        bus.publish(Event('memory.preprocess',{})); drain(hippo,consumer)
        assert model.one_chat.call_count==3
        assert not memory.working.snapshot()['jobs']
        assert not Path('config/chat_history.log').read_text(encoding='utf-8')
    finally:
        consumer.stop()


def test_fixed_formatter_resumes_preflight_failure_from_previous_version():
    from memory.retry_policy import fingerprint
    bus,memory,consumer,hippo,model = harness()
    try:
        add(memory,'请记住，合成用户偏爱安静环境')
        for _ in range(3):
            memory.working.encoding_retry(True,'invalid_result',config=fingerprint(settings.LARGE_LLM))
        assert memory.working.snapshot()['encoding_retry']['needs_attention']
        bus.publish(Event('memory.preprocess',{})); drain(hippo,consumer)
        assert model.one_chat.call_count == 1
        assert not memory.working.snapshot().get('encoding_retry')
        assert not Path('config/chat_history.log').read_text(encoding='utf-8')
        assert SOURCE_RULES in model.one_chat.call_args.args[1][0]['content']
    finally:
        consumer.stop()


def test_restart_resumes_only_unencoded_fragment():
    bus, memory, _, hippo, model = harness(with_store=False)
    first = {'role':'user','message_id':'first-fragment','content':'合成偏好一','timestamp':1793618400000}
    second = {'role':'user','message_id':'second-fragment','content':'合成偏好二','timestamp':1793618401000}
    contexts = [{'log':json.dumps(row,ensure_ascii=False)+'\n'} for row in (first,second)]
    text = ''.join(x['log'] for x in contexts)
    Path('config/chat_history.log').write_text(text,encoding='utf-8')
    batch = memory.working.begin_encoding(text,contexts)
    encoded = hippo._validate_memories([{'content':first['content'],'source_ids':[first['message_id']]}],[first],contexts[0]['log'])
    memory.working.encoded_fragment(batch['id'],0,encoded,[])
    bus2, memory2, consumer, restarted, model2 = harness()
    try:
        bus2.publish(Event('memory.preprocess',{})); drain(restarted,consumer)
        assert model2.one_chat.call_count == 1
        assert first['message_id'] not in model2.one_chat.call_args.args[1][-1]['content']
        assert not memory2.working.snapshot()['jobs']
        assert not Path('config/chat_history.log').read_text(encoding='utf-8')
    finally:
        consumer.stop()


def test_empty_success_is_distinct_from_failure():
    bus, memory, consumer, hippo, model = harness()
    model.one_chat.side_effect = lambda *args,**kwargs: '{"memories":[],"working_notes":[]}'
    try:
        add(memory,'一条无需长期保存的合成闲聊')
        bus.publish(Event('memory.preprocess',{})); drain(hippo,consumer)
        assert hippo.status()['phase'] == 'completed_empty'
        assert not Path('config/chat_history.log').read_text(encoding='utf-8')
    finally:
        consumer.stop()


def test_remaining_journal_is_waiting_even_after_previous_batch_completed():
    bus, memory, consumer, hippo, model = harness(with_store=False)
    job = memory.working.add_job('', [], [], [])
    memory.working.finish(job['id'])
    text = '尚未领取到批次的原始记录\n'
    Path('config/chat_history.log').write_bytes(text.encode('utf-8'))
    status = hippo.status()
    assert status['last_success_at']
    assert status['phase'] == 'waiting'
    assert status['journal_bytes'] == len(text.encode('utf-8'))


def test_pause_between_fragments_stops_new_model_calls_and_resume_continues():
    bus, memory, _, hippo, model = harness(with_store=False)
    rows = [{'role':'user','message_id':str(uuid.uuid4()),'content':text} for text in ('第一片','第二片')]
    contexts = [{'log':json.dumps(row,ensure_ascii=False)+'\n'} for row in rows]
    text = ''.join(x['log'] for x in contexts)
    Path('config/chat_history.log').write_text(text,encoding='utf-8')
    memory.working.begin_encoding(text,contexts)
    original = model.one_chat.side_effect
    def pause_after_first(*args,**kwargs):
        hippo.control('pause')
        return original(*args,**kwargs)
    model.one_chat.side_effect = pause_after_first
    bus.publish(Event('memory.preprocess',{})); drain(hippo,None)
    assert model.one_chat.call_count == 1 and memory.working.snapshot()['encoding']['index'] == 1
    model.one_chat.side_effect = original
    hippo.control('resume')
    bus.publish(Event('memory.preprocess',{})); drain(hippo,None)
    assert model.one_chat.call_count == 2 and memory.working.snapshot()['jobs']
    assert Path('config/chat_history.log').read_text(encoding='utf-8') == text


def test_pause_retains_queued_vector_task_without_calling_provider():
    bus, memory, consumer, hippo, model = harness()
    try:
        item = {'memory_id':str(uuid.uuid4()),'content':'暂停队列合成记忆','source_ids':[]}
        memory.working.add_job('',[item],[],[])
        hippo.control('pause')
        bus.publish(Event('memory.store',item)); consumer.store_queue.join()
        consumer.llm_client.get_embedding.assert_not_called()
        assert (consumer.spool_dir/(item['memory_id']+'.json')).exists()
        hippo.control('resume')
        bus.publish(Event('memory.preprocess',{})); drain(hippo,consumer)
        assert not memory.working.snapshot()['jobs']
        assert consumer.llm_client.get_embedding.call_count == 1
    finally:
        consumer.stop()


def test_pause_and_manual_retry_are_durable_without_deleting_sources():
    working = WorkingMemory()
    working.begin_encoding('raw-prefix\n', [{'log':'raw-prefix\n'}])
    working.encoding_retry(True, 'authentication', config='original')
    working.control('pause')
    restored = WorkingMemory()
    assert restored.snapshot()['manual_paused'] and restored.snapshot()['encoding_retry']['needs_attention']
    restored.control('retry')
    assert restored.snapshot()['encoding']['text'] == 'raw-prefix\n'
    assert not restored.snapshot().get('encoding_retry')


def test_retry_reply_keeps_single_user_source_and_foreground_scope():
    brain, bus, memory, state = make_brain()
    key = 'synthetic-user-input'
    state.prepare_dialogue.side_effect = RuntimeError('synthetic failure')
    brain.process_dialogue('合成重试输入', {'message_id':key, 'turn_id':key})
    assert bus.turns.snapshot()['status'] == 'failed'
    seen = []
    state.prepare_dialogue.side_effect = lambda _: seen.append(current_source())
    brain._llm_speak = Mock()
    brain.process_dialogue('合成重试输入', {'message_id':key, 'turn_id':'new-attempt', 'retry_reply':True})
    assert len([x for x in memory.get_memory()['history'] if x['role']=='user']) == 1
    assert seen[0][0] == 'foreground'
    assert bus.turns.snapshot()['user_message_id'] == key


def test_memory_control_is_epoch_bound_and_rejects_unknown_actions():
    server = make_server()
    bus, memory, consumer, hippo, _ = harness(with_store=False)
    server.hippocampus = hippo
    hippo.event_bus = server.event_bus
    client = TestClient(server.app)
    try:
        epoch = server.protocol.epoch
        assert client.post('/memory/tasks', json={'action':'pause','epoch':'old'}).status_code == 409
        assert client.post('/memory/tasks', json={'action':'erase','epoch':epoch}).status_code == 422
        response = client.post('/memory/tasks', json={'action':'pause','epoch':epoch})
        assert response.status_code == 200 and response.json()['status']['phase'] == 'paused'
        assert client.get('/memory/tasks', params={'epoch':epoch}).json()['status']['phase'] == 'paused'
        assert client.post('/memory/tasks', json={'action':'resume','epoch':epoch}).status_code == 200
    finally:
        server.db_memory.stop()


def test_websocket_reply_retry_keeps_user_id_and_cannot_reexecute_while_running():
    server = make_server()
    key, stamp = str(uuid.uuid4()), 1793618400000
    accepted = []
    server._accepted_inputs[key] = {'content':'合成失败输入','timestamp':stamp,'message_id':key,'turn_id':key,'persisted':True}
    with connection() as db, db.cursor() as cursor:
        cursor.execute("INSERT INTO message_list(sender,text,message_id) VALUES('user',%s,%s)", ('合成失败输入',key))
    failed = server.event_bus.turns.begin(stamp, key)
    failed.user_message_id = key
    failed.set_phase('generating'); failed.finish('failed'); failed.release()
    def receive(event):
        accepted.append(event.data)
        running = server.event_bus.turns.begin(event.data['turn_timestamp'], event.data['turn_id'])
        running.user_message_id = key
    server.event_bus.subscribe('user.input', receive)
    try:
        client = TestClient(server.app)
        with client.websocket_connect('/ws/chat') as ws:
            payload = {'sender':'user','format':'text','content':'合成失败输入', 'client_request_id':key,
                'retry':True,'retry_reply':True,'epoch':server.protocol.epoch}
            def echo():
                for _ in range(20):
                    item = ws.receive_json()
                    if item.get('type') == 'message' and item.get('sender') == 'user' and item.get('message_id') == key:
                        return item
                raise AssertionError('No user receipt')
            ws.send_json(payload)
            receipt = echo()
            assert receipt['turn_id'] != key
            assert accepted[0]['retry_reply'] and accepted[0]['retry_state_prepared']
            ws.send_json(payload)
            assert echo()['replayed']
            assert len(accepted) == 1
        assert server.db_memory.flush()
        with connection() as db, db.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM message_list WHERE message_id=%s', (key,))
            assert cursor.fetchone()[0] == 1
    finally:
        server.db_memory.stop()


def test_overall_deadline_ignores_late_response_and_prevents_second_network_call():
    client = object.__new__(LLMClient)
    release = threading.Event()
    def slow(**_kwargs):
        release.wait(3)
        return 'late'
    try:
        with pytest.raises(ModelCallError, match='timeout'):
            client._memory_request(slow, .05)
        with pytest.raises(ModelCallError, match='background_busy'):
            client._memory_request(Mock(), .05)
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if client._background_slot.acquire(blocking=False):
                client._background_slot.release()
                break
            time.sleep(.01)


def test_tls_connect_retry_does_not_resend_model_request_body(monkeypatch):
    for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
        monkeypatch.delenv(key,raising=False)
    writes = []
    names = []
    class Stream:
        def __init__(self, attempt): self.attempt, self.reads = attempt, 0
        def start_tls(self, **kwargs):
            names.append(kwargs['server_hostname'])
            if self.attempt < 3: raise httpcore.ConnectTimeout('synthetic TLS node timeout')
            return self
        def write(self, buffer, timeout=None): writes.append(buffer)
        def read(self, max_bytes, timeout=None):
            self.reads += 1
            return b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}' if self.reads == 1 else b''
        def close(self): pass
        def get_extra_info(self, name): return False if name == 'is_readable' else None
    class Backend:
        attempts = 0
        def connect_tcp(self, **kwargs):
            self.attempts += 1
            return Stream(self.attempts)
        def sleep(self, seconds): pass
    backend = Backend()
    original_factory = httpx.HTTPTransport
    def transport(**kwargs):
        result = original_factory(**kwargs)
        result._pool._network_backend = backend
        return result
    monkeypatch.setattr('core.http_transport.httpx.HTTPTransport', transport)
    monkeypatch.setattr('core.http_transport.socket.getaddrinfo', lambda *a,**k: [
        (2,1,6,'',('198.51.100.1',443)),(2,1,6,'',('198.51.100.2',443))])
    with LLMClient._http_client() as client:
        response = client.post('https://synthetic.invalid/v1/chat/completions', json={'synthetic':'body'},timeout=LLMClient._timeouts(20))
    assert response.status_code == 200 and backend.attempts == 3
    sent = b''.join(writes)
    assert sent.count(b'POST /v1/chat/completions') == 1 and sent.count(b'"synthetic":"body"') == 1
    assert names == ['synthetic.invalid']*3
    assert b'Host: synthetic.invalid' in sent


def test_read_failure_after_sending_body_never_retries_another_address(monkeypatch):
    from core.http_transport import ConnectFailoverTransport
    transport = ConnectFailoverTransport()
    transport.direct = Mock()
    transport.direct.handle_request.side_effect = httpx.ReadTimeout('synthetic sent request failed')
    resolve = Mock()
    monkeypatch.setattr('core.http_transport.socket.getaddrinfo',resolve)
    with pytest.raises(httpx.ReadTimeout):
        transport.handle_request(httpx.Request('POST','https://synthetic.invalid/chat',content=b'body'))
    assert transport.direct.handle_request.call_count == 1
    resolve.assert_not_called()
    transport.close()
