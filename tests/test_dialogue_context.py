"""Objective continuity checks; perceived warmth still needs human evaluation."""
import math
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from brain.dialogue_context import build_compact_reply_messages, build_dialogue_messages
from config.settings import settings
from core.time_utils import to_epoch
from tests.test_persistence_scene import make_state_manager
from tests.test_turn_cancellation import make_brain
from tools.processor import ToolCallProcessor


def message(role, text, when=None, **metadata):
    return {'role': role, 'content': text, 'timestamp': int(to_epoch(when) * 1000) if when else None, **metadata}


def build(history, when='2026-11-02 15:20:00', **kwargs):
    return build_dialogue_messages('合成角色设定', history, '合成场景', to_epoch(when), **kwargs)


def test_preserves_actual_roles_and_latest_input_without_task_wrapper():
    history = [message('user', '刚才那页我没看懂。', '2026-11-02 15:19:10'),
        message('assistant', '<thought>角色感受</thought>是哪一段？', '2026-11-02 15:19:20'),
        message('user', '第二段，为什么突然跳到结论？', '2026-11-02 15:20:00')]
    result = build(history)
    assert [x['role'] for x in result] == ['system', 'user', 'assistant', 'user']
    assert result[-1]['content'] == history[-1]['content']
    assert result[2]['content'] == '是哪一段？'
    assert '连续交谈' in result[0]['content']
    assert '角色感受' not in str(result)
    assert '合成场景' in result[0]['content'] and '2026-11-02 15:20:00' in result[0]['content']
    assert '以下是对话历史' not in result[-1]['content']


def test_old_relative_dates_stay_anchored_across_midnight():
    history = [message('user', '明天再看吧，晚安。', '2026-11-01 23:58:00'),
        message('assistant', '好，晚安。', '2026-11-01 23:58:03'),
        message('user', '接着看那一页。', '2026-11-02 15:20:00')]
    result = build(history)
    context = result[0]['content']
    assert '昨天' in context and '下午' in context and '跨越 1 个日期边界' in context
    assert '不证明此刻仍然如此' in context
    assert result[1]['content'] == '明天再看吧，晚安。'
    assert result[-1]['content'] == '接着看那一页。'


@pytest.mark.parametrize('stamp', [None, 'old date', True, math.nan, math.inf, 10**100])
def test_unknown_or_invalid_legacy_time_cannot_become_current_fact(stamp):
    result = build([{'role': 'user', 'content': '今天一起见面', 'timestamp': stamp}, message('user', '你好')])
    assert '未知，不能作为当日事实' in result[0]['content']
    assert '关系陌生或首次见面' in result[0]['content']


def test_rewound_world_does_not_call_future_history_just_now():
    result = build([message('user', '已经出门了', '2026-11-03 15:00:00'),
        message('user', '现在呢', '2026-11-02 15:20:00')])
    assert '时间回退' in result[0]['content']
    assert '未来时间' in result[0]['content']
    assert '连续交谈' not in result[0]['content']


def test_proactive_trigger_is_distinct_from_user_speech_and_context_not_duplicated():
    result = build([message('user', '我去忙一会', '2026-11-02 13:00:00')],
        proactive=True, proactive_instruction='合成主动发言规则')
    assert result[-1]['content'].startswith('【系统唤醒，非用户发言】')
    assert result[0]['content'].count('合成场景') == 1
    assert '合成主动发言规则' in result[0]['content']
    assert '上一条用户消息在 2026-11-02 13:00:00' in result[0]['content']


def test_real_brain_path_passes_native_turns_and_system_facts_to_model():
    brain, bus, memory, _state = make_brain()
    bus.restore_logical_time('2026-11-02 15:20:00')
    memory.add_message('user', '我在宿舍，用微信聊天。', timestamp=int(to_epoch('2026-11-02 15:19:00') * 1000))
    memory.add_message('assistant', '我这页还差一段。', timestamp=int(to_epoch('2026-11-02 15:19:10') * 1000))
    brain.llm_client.stream_chat.return_value = iter(['这段绕了点，我也卡了一会。'])
    brain.process_dialogue('哪一段？')
    messages = brain.llm_client.stream_chat.call_args.kwargs['messages']
    assert messages[-1] == {'role': 'user', 'content': '哪一段？'}
    assert messages[-2] == {'role': 'assistant', 'content': '我这页还差一段。'}
    assert '连续交谈' in messages[0]['content']


@pytest.mark.parametrize('text', ['行，那就这样吧。', '好哇。', '噢，懂啦。', '谢谢，够啦。', '我忙完回来啦。'])
def test_compact_reply_keeps_real_roles_and_confirmed_scene(text):
    history = [message('user', '很早以前的一条'), message('assistant', '旧答复'),
               message('user', '可以先围绕原句说。'), message('assistant', '好，保留原词。'), message('user', text)]
    state = {'scene': {'character_location': '合成地点', 'remote_contact_confirmed': True,
                      'communication_mode': 'remote'}, 'P': 6, 'A': 4, 'D': 7}
    messages = build_compact_reply_messages('合成依鸣', history, state, to_epoch('2026-11-24 15:20:00'))
    assert messages[1:] == [{'role': row['role'], 'content': row['content']} for row in history[-3:]]
    prompt = messages[0]['content']
    assert '合成依鸣' in prompt and '不能承诺改文件' in prompt
    assert '角色在合成地点，当前是远程交流，用户位置未知' in prompt
    assert 'P=6，A=4，D=7' in prompt and '2026-11-24 15:20:00' in prompt
    assert '很早以前的一条' not in str(messages)


@pytest.mark.parametrize('last,metadata', [
    ('好哇。之前你说的那个呢？', {}),
    ('好哇，但我还想问上下两行的关系。', {}),
    ('好哇。', {'completion_status': 'interrupted'}),
    ('好哇。', {'turn_source': 'proactive'}),
])
def test_compact_reply_requires_a_complete_unambiguous_exchange(last, metadata):
    history = [message('user', '先留一点空间'), message('assistant', '你看着留吧。', **metadata), message('user', last)]
    assert build_compact_reply_messages('合成依鸣', history, {'scene': {}}, to_epoch('2026-11-24 15:20:00')) is None


@pytest.mark.parametrize('rhythm,text,compact', [
    ('compact_v1', '好哇。', True),
    ('baseline', '好哇。', False),
    ('compact_v1', '好哇。你再解释一下排序原理。', False),
])
def test_compact_mode_on_real_reply_path(monkeypatch, rhythm, text, compact):
    monkeypatch.setattr(settings, 'DIALOGUE_POLICY', 'expressive_v1')
    monkeypatch.setattr(settings, 'DIALOGUE_RHYTHM', rhythm)
    brain, bus, memory, _ = make_brain()
    manager = make_state_manager()
    manager.event_bus, manager.short_term_memory, manager.prepare_dialogue = bus, memory, Mock()
    brain.state_manager = manager
    memory.add_message('user', '保留在完整历史的合成文字。')
    memory.add_message('assistant', '旧答复。')
    memory.add_message('user', '最后那个空行要删吗？')
    memory.add_message('assistant', '先留着吧。')
    brain.llm_client.stream_chat.return_value = iter(['收到这句了。'])
    brain.process_dialogue(text)
    request = brain.llm_client.stream_chat.call_args.kwargs['messages']
    assert request[-1] == {'role': 'user', 'content': text}
    assert ('不能承诺改文件' in request[0]['content']) is compact
    assert ('保留在完整历史的合成文字。' in str(request)) is not compact
    brain.llm_client.stream_chat.assert_called_once()
    assert [row['content'] for row in memory.get_full_messages()[-2:]] == [text, '收到这句了。']


def test_unexpected_tool_call_restores_full_context_after_compact_reply_prompt(monkeypatch):
    monkeypatch.setattr(settings, 'DIALOGUE_POLICY', 'expressive_v1')
    monkeypatch.setattr(settings, 'DIALOGUE_RHYTHM', 'compact_v1')
    brain, bus, memory, _ = make_brain()
    manager = make_state_manager()
    manager.event_bus, manager.short_term_memory, manager.prepare_dialogue = bus, memory, Mock()
    brain.state_manager = manager
    brain.tool_processor = ToolCallProcessor.create_with_memory_tool(SimpleNamespace(
        query_memory=lambda **_: {'found': False}
    ))
    memory.add_message('user', '合成的较早资料。')
    memory.add_message('assistant', '好。')
    memory.add_message('user', '空行留不留？')
    memory.add_message('assistant', '留着吧。')
    brain.llm_client.stream_chat.side_effect = [
        iter(['<tool>{"name":"memory_query","parameters":{"query":"合成"}} </tool>']),
        iter(['好。']),
    ]
    brain.process_dialogue('好哇。')
    first, followup = [call.kwargs['messages'] for call in brain.llm_client.stream_chat.call_args_list]
    assert '合成的较早资料。' not in str(first)
    assert '合成的较早资料。' in str(followup)
    assert '不能承诺改文件' not in followup[0]['content']
    assert memory.get_full_messages()[-1]['content'] == '好。'


def test_reply_time_uses_the_captured_instant_across_a_date_boundary():
    manager = make_state_manager()
    manager.event_bus.restore_logical_time('2026-11-03 00:00:01')
    captured = to_epoch('2026-11-02 23:59:59')
    state_context = manager.build_reply_context(captured)
    text = build_dialogue_messages('合成人物', [message('user', '明天再聊')], state_context, captured)[0]['content']
    assert '【当前准确时间】2026-11-02 23:59:59' in text
    assert '【本轮时间锚点】2026-11-02 23:59:59' in text
    assert '明天是 2026-11-03' in text


def test_preparation_failure_does_not_replace_character_intention_with_service_workflow():
    manager = make_state_manager()
    manager.current_state['客观情境'] = '宿舍里，整理笔记'
    manager.current_state['近期目标'] = '把这页最后一段弄清楚'
    manager._fallback(manager.event_bus.logical_now, 'synthetic failure')
    assert manager.current_state['近期目标'] == '把这页最后一段弄清楚'
    assert manager.current_state['scene_status'] == 'needs_confirmation'
    assert '未知位置' in manager.prompt_injection
    manager._fallback(manager.event_bus.logical_now, 'offline failure', offline=True)
    assert manager.current_state['近期目标'] == ''
