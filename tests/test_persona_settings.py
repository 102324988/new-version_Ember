from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from core.event_bus import EventBus
from persona.persona_api import install_persona_api
from persona.persona_settings import PersonaSettings, persona_prompts
from tests.test_persistence_scene import make_state_manager
from tests.test_turn_cancellation import make_brain


def setup():
    bus = EventBus()
    bus.persona_settings = PersonaSettings()
    llm = Mock()
    llm.stream_chat.side_effect = lambda *args, **kwargs: iter(
        ['<thought>内部内容</thought><response>测试对白</response><tool>{"name":"test"}</tool>'])
    server = SimpleNamespace(app=FastAPI(), event_bus=bus, service_runtime=None,
                             archive_jobs=SimpleNamespace(busy=False), brain=SimpleNamespace(llm_client=llm))
    install_persona_api(server)
    return bus, llm, TestClient(server.app, base_url='http://127.0.0.1', client=('127.0.0.1', 12345))


def payload(revision=0, core='依鸣喜欢观察鸟类。', style='自然接一句。'):
    return {'expected_revision': revision, 'core_persona': core, 'speaking_style': style}


def test_first_open_keeps_exact_runtime_prompts_without_writing():
    bus, llm, client = setup()
    active = client.get('/api/persona').json()['active']
    assert active['core_persona'] == settings.RAW_CORE_PERSONA
    prompts = persona_prompts(bus)
    assert (prompts.core, prompts.system, prompts.reply, prompts.proactive) == (
        settings.CORE_PERSONA, settings.SYSTEM_PROMPT, settings.DIALOGUE_REPLY_PROMPT, settings.SYSTEM_PROMPT)
    original = payload(core=active['core_persona'], style='')
    preview = client.post('/api/persona/preview', json=original)
    assert preview.json() == {'effective': settings.DIALOGUE_REPLY_PROMPT, 'diff': ''}
    assert client.put('/api/persona', json=original).json()['active']['revision'] == 0
    assert not bus.persona_settings.path.exists()
    llm.stream_chat.assert_not_called()


def test_trial_uses_synthetic_history_and_leaves_formal_state_alone():
    bus, llm, client = setup()
    assert '自然接一句' in client.post('/api/persona/preview', json=payload()).json()['diff']
    trial = client.post('/api/persona/trial', json={**payload(), 'message': '就简单说说吧。'})
    assert trial.json() == {'current': '测试对白', 'draft': '测试对白', 'revision': 0}
    calls = llm.stream_chat.call_args_list
    assert len(calls) == 2
    before, after = (call.args[1] for call in calls)
    assert before[1:] == after[1:]
    assert before[-1] == {'role': 'user', 'content': '就简单说说吧。'}
    assert '自然接一句' not in before[0]['content'] and '自然接一句' in after[0]['content']
    assert all(call.kwargs['call_type'] == 'persona_preview' for call in calls)
    assert bus.turns.current is None
    assert not bus.persona_settings.path.exists()


def test_apply_conflict_and_history_survive_reload_and_restore_twice():
    bus, llm, client = setup()
    assert client.put('/api/persona', json=payload(core=' \n ')).status_code == 422
    assert client.put('/api/persona', json=payload(), headers={'Origin':'http://elsewhere.invalid'}).status_code == 403
    first = client.put('/api/persona', json=payload()).json()['active']
    assert first['revision'] == 1 and bus.world_revision == 1
    assert client.put('/api/persona', json=payload()).status_code == 409
    assert PersonaSettings().snapshot()['active'] == first
    for target in (0, 1):
        active = client.post('/api/persona/restore', json={'expected_revision': bus.persona_settings.prompts.revision,
                                                         'revision': target}).json()['active']
        assert active['restored_from'] == target
        assert PersonaSettings().snapshot()['active'] == active
    assert active['core_persona'] == first['core_persona']
    assert active['speaking_style'] == first['speaking_style']
    assert bus.world_revision == 3


def test_persona_change_waits_for_unreleased_turn_and_reports_disk_failure():
    bus, _llm, client = setup()
    turn = bus.turns.begin(1000, 'prior')
    assert client.put('/api/persona', json=payload()).status_code == 409
    turn.finish('completed')
    turn.release()
    with patch.object(Path, 'replace', side_effect=OSError('disk full')):
        assert client.put('/api/persona', json=payload()).status_code == 503
    assert bus.persona_settings.prompts.revision == 0
    assert not list(bus.persona_settings.path.parent.glob('*.tmp'))
    assert client.put('/api/persona', json=payload()).status_code == 200


def test_new_core_reaches_both_foreground_models_with_style_scoped_to_reply():
    bus, _llm, client = setup()
    assert client.put('/api/persona', json=payload()).status_code == 200
    brain, _, memory, _ = make_brain()
    state = make_state_manager()
    state.event_bus = bus
    state.short_term_memory = memory
    brain.state_manager = state
    brain.event_bus = bus
    state._ask_llm_with_tools = Mock(return_value='{}')
    state.llm_client = Mock()
    state.llm_client._extract_json.return_value = dict(
        state.current_state, 客观情境='图书馆', scene=state.current_state['scene'])
    brain.llm_client.stream_chat.return_value = iter(['测试对白'])
    brain.process_dialogue('你好')
    assert '依鸣喜欢观察鸟类。' in state._ask_llm_with_tools.call_args.args[0]
    assert '自然接一句' not in state._ask_llm_with_tools.call_args.args[0]
    actual = brain.llm_client.stream_chat.call_args.kwargs['messages'][0]['content']
    assert '依鸣喜欢观察鸟类。' in actual and '自然接一句' in actual
    assert bus.turns.snapshot()['persona_revision'] == 1
