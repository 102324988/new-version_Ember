from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import Mock, patch
import pytest
from service.store import BudgetExceeded
from tests.test_service import store
from tests.test_memory_pipeline import harness, add
from tests.test_turn_cancellation import make_brain
from core.event_bus import Event
from memory.working_memory import WorkingMemory
from brain.llm_client import LLMClient
from config.settings import settings


def test_background_stops_but_foreground_has_no_daily_cap(store):
    store.limits['chat'] = 4
    for _ in range(3):
        store.reserve('chat', call_type='memory_encode')
    with pytest.raises(BudgetExceeded, match='后台'):
        store.reserve('chat', call_type='idle_evolve')
    for _ in range(5):
        store.reserve('chat', call_type='dialogue', origin='foreground')


def test_old_date_override_cannot_limit_foreground_or_expand_background(store, monkeypatch):
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    monkeypatch.setenv('EMBER_CHAT_OVERRIDE_DATE', today)
    monkeypatch.setenv('EMBER_CHAT_OVERRIDE_LIMIT', '3')
    for _ in range(3):
        store.reserve('chat', call_type='dialogue', origin='foreground')
    assert store.snapshot()['usage']['chat']['requests'] == 3
    monkeypatch.setenv('EMBER_CHAT_OVERRIDE_DATE', '2000-01-01')
    assert store.effective_limits()['chat'] == 1
    store.reserve('chat', call_type='dialogue', origin='foreground')
    assert store.snapshot()['usage']['chat']['requests'] == 4


def test_encoding_retry_survives_restart_and_backoff_increases():
    memory = WorkingMemory()
    with patch('time.time', return_value=1000), patch('memory.retry_policy.random.uniform', return_value=0):
        memory.encoding_retry(True)
        assert memory.snapshot()['encoding_retry']['next_attempt_at'] == 1060
    memory = WorkingMemory()
    with patch('time.time', return_value=1060), patch('memory.retry_policy.random.uniform', return_value=0):
        memory.encoding_retry(True)
        assert memory.snapshot()['encoding_retry']['next_attempt_at'] == 1180
    memory.encoding_retry(False)
    assert 'encoding_retry' not in memory.snapshot()


def test_background_repeated_events_do_not_repeat_failed_model_call():
    bus, memory, store, hippo, model = harness()
    try:
        add(memory, '合成记忆内容')
        model.one_chat.return_value = None
        model.one_chat.side_effect = None
        bus.publish(Event('memory.preprocess', {'reason': 'checkpoint'}))
        assert hippo.wait_idle(5)
        hippo._last_attempt = 0
        bus.publish(Event('memory.preprocess', {'reason': 'checkpoint'}))
        assert hippo.wait_idle(5)
        assert model.one_chat.call_count == 1
    finally:
        store.stop()


def test_memory_timeout_is_separate_and_has_no_immediate_retry(monkeypatch):
    client = object.__new__(LLMClient)
    client.large_client = Mock()
    client.large_client.chat.completions.create.side_effect = TimeoutError('synthetic')
    monkeypatch.setenv('EMBER_REQUEST_TIMEOUT', '20')
    assert client.one_chat(settings.LARGE_LLM, [], call_type='memory_encode') is None
    request = client.large_client.chat.completions.create
    assert request.call_count == 1 and request.call_args.kwargs['timeout'].read == 60
    assert request.call_args.kwargs['timeout'].connect == 5


def test_budget_failure_reaches_public_turn_without_provider_payload():
    brain, bus, memory, state = make_brain()
    state.prepare_dialogue.side_effect = BudgetExceeded('private provider details')
    brain.process_dialogue('合成用户输入')
    turn = bus.turns.snapshot()
    assert turn['status'] == 'failed' and turn['released']
    assert turn['error']['code'] == 'budget_exhausted'
    assert '额度' in turn['error']['message'] and 'private' not in turn['error']['message']
    assert memory.get_memory()['history'][0]['role'] == 'user'
