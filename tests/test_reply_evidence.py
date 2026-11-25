import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from brain.llm_client import LLMClient
from brain.reply_evidence import explicit_name, recall_candidates, resolve_selection
from config.settings import settings
from core.experience import Experience
from core.service_policy import current_source
from core.time_utils import to_epoch
from core.turns import Turn, TurnCancelled
from persona import continuity
from tests.test_persistence_scene import make_state_manager
from tests.test_turn_cancellation import make_brain
from tools.base import ToolResult
from tools.processor import ToolCallProcessor


QUERY = '请说出昨天我给铜钥匙起的名字，按原话。'
QUOTE = '这把铜钥匙叫“微光二号”。'
NOW = to_epoch('2026-11-02 00:03:00')


def message(identifier, text, when=None, role='user', status='completed'):
    return {'message_id': identifier, 'content': text, 'role': role, 'completion_status': status,
            'timestamp': to_epoch(when) * 1000 if when else None}


def test_selects_only_completed_dated_user_originals_across_midnight():
    history = [
        message('far', '前一天的同名物', '2026-10-31 23:58:00'),
        message('old', QUOTE, '2026-11-01 23:58:00'),
        message('legacy', '无时刻的记录'),
        message('assistant', '误记成“雨伞”。', '2026-11-01 23:59:00', role='assistant'),
        message('partial', '没说完的片段。', '2026-11-01 23:59:40', status='interrupted'),
        message('future', '时钟回退后尚未发生。', '2026-11-03 00:01:00'),
        message('current', '我今天给铜钥匙起的名字是什么？', '2026-11-02 00:02:50'),
    ]
    assert not recall_candidates(history, NOW)[1]
    history[-1]['content'] = QUERY
    query, candidates = recall_candidates(history, NOW)
    assert query == QUERY
    assert list(candidates) == ['h01']
    assert resolve_selection('{"ids":["h01"]}', candidates) == [
        {'消息ID': 'old', '说话者': '用户', '时间': '2026-11-01T23:58:00.000+08:00', '原话': QUOTE}]


def test_delayed_reply_keeps_yesterday_relative_to_the_received_question():
    history = [
        message('original', QUOTE, '2026-10-31 23:59:00'),
        message('not-yesterday', '我还是叫它“露水”。', '2026-11-01 22:00:00'),
        message('asked', QUERY, '2026-11-01 23:59:00'),
    ]
    assert recall_candidates(history, NOW)[1]['h01']['消息ID'] == 'original'


@pytest.mark.parametrize('query', [
    '傍晚好，我昨天去看了会儿雨。', '你昨天怎么说的？我给了什么建议来着？',
    '昨天和前天我给钥匙取的名字是什么？', '我三百五十二天前的原话是什么？',
    '昨天我把钥匙改成的现名是什么？', '我上周三给铜钥匙起的名字是什么？',
    '我问昨天到明天关于钥匙的原话都是什么？',
])
def test_out_of_scope_query_does_not_trigger_selection(query):
    assert not recall_candidates([message('old', QUOTE, '2026-11-01 14:00:00'), message('now', query)], NOW)[1]


@pytest.mark.parametrize('text', [
    '{"ids":["h77"]}', '{"ids":["h01","h01"]}', '{"ids":[1]}', '{"ids":[],"quote":"改写"}',
    '{"ids":[],"ids":["h01"]}', '{"ids":["h01","h02","h03"]}', 'broken',
])
def test_selection_rejects_untrusted_or_unbounded_payload(text):
    with pytest.raises(ValueError):
        resolve_selection(text, {'h01': {'原话': QUOTE}})


def seeded_brain():
    brain, bus, memory, state = make_brain()
    bus.restore_logical_time(NOW)
    bus.pause_world(True)
    memory.add_message('user', QUOTE, timestamp=int(to_epoch('2026-11-01 23:58:00') * 1000), message_id='selected')
    return brain, bus, memory, state


def test_dialogue_uses_validated_source_before_reply_and_after_empty_tool(monkeypatch):
    monkeypatch.setattr(settings, 'DIALOGUE_RECALL_EVIDENCE', True)
    brain, bus, memory, _ = seeded_brain()
    hippocampus = SimpleNamespace(query_memory=lambda **_: {'found': False})
    brain.tool_processor = ToolCallProcessor.create_with_memory_tool(hippocampus)
    brain.llm_client.select_recall_sources.return_value = '{"ids":["h01"]}'
    brain.llm_client.stream_chat.side_effect = [
        iter(['<tool>{"name":"memory_query","parameters":{"query":"铜钥匙","keywords":["钥匙"]}}</tool>']),
        iter(['微光二号。']),
    ]
    finished = []
    bus.subscribe('llm.finished', lambda event: finished.append(event.data))
    compound_query = QUERY + '再告诉我有没有相近的记录。'
    brain.process_dialogue(compound_query)
    assert bus.turns.current.status == 'completed'
    brain.llm_client.select_recall_sources.assert_called_once()
    selector_input = brain.llm_client.select_recall_sources.call_args.args[0]
    assert QUOTE in selector_input[-1]['content'] and 'selected' not in selector_input[-1]['content']
    first, continuation = [call.kwargs['messages'] for call in brain.llm_client.stream_chat.call_args_list]
    assert first[-1] == {'role': 'user', 'content': compound_query}
    assert '2026-11-01T23:58:00.000+08:00' in first[0]['content'] and 'selected' not in first[0]['content']
    assert '一次记忆检索未提供命中资料' in continuation[-1]['content']
    assert QUOTE in continuation[-1]['content']
    assert len(finished) == 1
    assert [row['content'] for row in memory.get_full_messages()[1:]] == [QUOTE, compound_query, '微光二号。']


def test_standalone_name_and_requested_quote_skip_freely_generated_extras(monkeypatch):
    monkeypatch.setattr(settings, 'DIALOGUE_RECALL_EVIDENCE', True)
    for query, expected in (
        ('昨天我给铜钥匙定的代号叫什么？别把铁的算进去。', '“微光二号”。'),
        ('昨天我提到铜钥匙时用了什么说法？按原话。', f'“{QUOTE}”'),
    ):
        brain, _, memory, _ = seeded_brain()
        brain.llm_client.select_recall_sources.return_value = '{"ids":["h01"]}'
        brain.process_dialogue(query)
        brain.llm_client.select_recall_sources.assert_called_once()
        brain.llm_client.stream_chat.assert_not_called()
        assert memory.get_full_messages()[-1]['content'] == expected


@pytest.mark.parametrize('enabled,query,response', [
    (False, QUERY, None), (True, '我回来了。', None),
    (True, QUERY + '再说说相近的例子。', '{"ids":["h90"]}'),
    (True, QUERY + '再说说相近的例子。', TimeoutError('synthetic timeout')),
])
def test_disabled_unneeded_or_failed_selection_keeps_main_dialogue(monkeypatch, enabled, query, response):
    monkeypatch.setattr(settings, 'DIALOGUE_RECALL_EVIDENCE', enabled)
    brain, bus, memory, _ = seeded_brain()
    if isinstance(response, Exception):
        brain.llm_client.select_recall_sources.side_effect = response
    else:
        brain.llm_client.select_recall_sources.return_value = response
    brain.llm_client.stream_chat.return_value = iter(['这一轮照常回复。'])
    brain.process_dialogue(query)
    assert bus.turns.current.status == 'completed'
    assert brain.llm_client.select_recall_sources.call_count == int(response is not None)
    messages = brain.llm_client.stream_chat.call_args.kwargs['messages']
    assert '【本轮已定位的历史原话】' not in messages[0]['content']
    assert memory.get_full_messages()[-1]['content'] == '这一轮照常回复。'


def test_literal_abbreviation_is_available_when_selector_times_out(monkeypatch):
    monkeypatch.setattr(settings, 'DIALOGUE_RECALL_EVIDENCE', True)
    brain, _, memory, _ = seeded_brain()
    memory.add_message('user', '这块纸牌的简称就叫“格律五”。', timestamp=int(to_epoch('2026-11-01 19:00:00') * 1000))
    brain.llm_client.select_recall_sources.side_effect = TimeoutError('synthetic timeout')
    brain.process_dialogue('昨天我给这块纸牌起的简称是什么？只给名字。')
    brain.llm_client.select_recall_sources.assert_called_once()
    brain.llm_client.stream_chat.assert_not_called()
    assert memory.get_full_messages()[-1]['content'] == '“格律五”。'


def test_cancel_during_selection_does_not_start_reply(monkeypatch):
    monkeypatch.setattr(settings, 'DIALOGUE_RECALL_EVIDENCE', True)
    brain, bus, memory, _ = seeded_brain()
    def cancel(_messages, turn):
        turn.request_cancel()
        return '{"ids":["h01"]}'
    brain.llm_client.select_recall_sources.side_effect = cancel
    brain.process_dialogue(QUERY)
    assert bus.turns.current.status == 'interrupted'
    brain.llm_client.stream_chat.assert_not_called()
    assert memory.get_full_messages()[-1]['content'] == QUERY


def test_wrong_day_has_no_guessed_name_or_generated_chunk(monkeypatch):
    monkeypatch.setattr(settings, 'DIALOGUE_RECALL_EVIDENCE', True)
    brain, bus, memory, _ = seeded_brain()
    older = message('other', QUOTE, '2026-11-01 23:58:00')
    brain.tool_processor.execute_tool_calls.return_value = [
        {'tool_name': 'memory_query', 'result': ToolResult.ok({'found': True, 'episodic_records': [{'evidence': [older]}]})}]
    chunks = []
    bus.subscribe('llm.chunk', lambda event: chunks.append(event.data['text']))
    brain.process_dialogue('前天我给铜钥匙取的名字是什么？')
    brain.llm_client.select_recall_sources.assert_not_called()
    brain.llm_client.stream_chat.assert_not_called()
    assert len(chunks) == 1 and '微光二号' not in chunks[0] and '那天' in chunks[0]
    assert memory.get_full_messages()[-1]['content'] == chunks[0]


def test_exact_original_from_archive_checks_individual_date_and_foreground_scope(monkeypatch):
    monkeypatch.setattr(settings, 'DIALOGUE_RECALL_EVIDENCE', True)
    brain, _, memory, _ = seeded_brain()
    actual = message('right', '小铜匣今后就叫“雾邮局”。', '2026-10-31 11:00:00')
    mistaken = message('wrong', '小铜匣叫“错日灯”。', '2026-11-01 11:00:00')
    origins = []
    def query_memory(**_):
        origins.append(current_source()[0])
        return {'found': True, 'episodic_records': [{'time': '2026-11-01 11:00:00',
                                                     'evidence': [mistaken, actual]}]}
    brain.tool_processor = ToolCallProcessor.create_with_memory_tool(SimpleNamespace(query_memory=query_memory))
    brain.process_dialogue('前天我给小铜匣起的名字是什么？只报名字。')
    assert origins == ['foreground']
    brain.llm_client.stream_chat.assert_not_called()
    assert memory.get_full_messages()[-1]['content'] == '“雾邮局”。'


@pytest.mark.parametrize('quote', [
    '如果白封手册用这个字，就叫“候选”。',
    '红白封手册叫“不同物件”。',
    '白封手册已经放下，红盒子叫“同日错物”。',
    '白封手册叫“第一名”，白封手册叫“第二名”。',
    '白封手册不叫“错误名字”。',
])
def test_archive_does_not_infer_the_name_from_an_unbound_or_ambiguous_phrase(quote):
    assert explicit_name([{'原话': quote}], '白封手册') is None


@pytest.mark.parametrize('on_date,expected,tool_calls', [
    ('别把浅棕信封收进去。', '那天你给它起的名字，我没找到能核对的原话。', 1),
    ('浅棕信封就叫“星门”。', '“星门”。', 0),
])
def test_empty_selection_checks_on_date_text_and_does_not_reuse_old_name(monkeypatch, on_date, expected, tool_calls):
    monkeypatch.setattr(settings, 'DIALOGUE_RECALL_EVIDENCE', True)
    brain, _, memory, _ = seeded_brain()
    memory.add_message('user', '浅棕信封就叫“错日灯”。', timestamp=int(to_epoch('2026-10-31 11:00:00') * 1000))
    memory.add_message('user', on_date, timestamp=int(to_epoch('2026-11-01 11:00:00') * 1000))
    brain.llm_client.select_recall_sources.return_value = '{"ids":[]}'
    brain.tool_processor.execute_tool_calls.return_value = []
    brain.process_dialogue('昨天我给浅棕信封起的名字是什么？')
    brain.llm_client.stream_chat.assert_not_called()
    assert brain.tool_processor.execute_tool_calls.call_count == tool_calls
    assert memory.get_full_messages()[-1]['content'] == expected


def test_failed_archive_lookup_still_finishes_with_scoped_unknown(monkeypatch):
    monkeypatch.setattr(settings, 'DIALOGUE_RECALL_EVIDENCE', True)
    brain, bus, memory, _ = seeded_brain()
    brain.tool_processor.execute_tool_calls.side_effect = TimeoutError('unavailable')
    brain.process_dialogue('三天前我给方盒定的代号叫什么？')
    assert bus.turns.current.status == 'completed'
    brain.llm_client.stream_chat.assert_not_called()
    assert '没找到能核对的原话' in memory.get_full_messages()[-1]['content']


@pytest.mark.parametrize('text,expected,with_choice', [
    ('蓝夹里放进去纸了吗？就问夹子里有没有纸。', '蓝夹里现在有没有纸，我还不确定。', False),
    ('你那蓝夹里面现在放了几页纸？', '夹里已经放了几页，我还不清楚。', False),
    ('米白还是橄榄色，挑一个。另一个问题，你那蓝夹有没有收好？',
     '我选橄榄色。蓝夹收没收好，我还不确定。', True),
    ('浅灰和深蓝你选哪个？还有，蓝夹已经收好了吗？',
     '我选深蓝。蓝夹收没收好，我还不确定。', True),
    ('谢谢你。米白和墨绿你帮我选一个，然后说说蓝夹到底收完没有。',
     '我选墨绿。蓝夹收没收好，我还不确定。', True),
    ('多谢啦，奶白还是天蓝？顺便问一下你那个蓝夹收完没有？',
     '我选天蓝。蓝夹收没收好，我还不确定。', True),
    ('先谢啦。浅灰和米白你选一个，然后再说下蓝夹现在整理完没有。',
     '我选米白。蓝夹收没收好，我还不确定。', True),
    ('墨绿还是卡其？然后说下蓝夹收完没有，不确定就直说。',
     '我选卡其。蓝夹收没收好，我还不确定。', True),
    ('先问一句，你那蓝夹到底收完没有？不确定就直说。', '蓝夹收没收好，我还不确定。', False),
    ('谢谢你。请问你那个蓝夹收完没有？', '蓝夹收没收好，我还不确定。', False),
    ('谢谢。蓝夹收完没有，墨绿和米白你帮我选一个。', '我选米白。蓝夹收没收好，我还不确定。', True),
    ('谢谢你。然后说说蓝文件夹现在收完没有？', '蓝夹收没收好，我还不确定。', False),
])
def test_scoped_activity_only_emits_and_saves_verified_text(monkeypatch, text, expected, with_choice):
    monkeypatch.setattr(settings, 'DIALOGUE_ACTIVITY_EVIDENCE', True)
    brain, bus, memory, _ = seeded_brain()
    state = make_state_manager()
    state.event_bus, state.short_term_memory, state.prepare_dialogue = bus, memory, Mock()
    brain.state_manager = state
    brain.llm_client.select_activity_color.return_value = '{"choice":"B"}'
    chunks, finals = [], []
    bus.subscribe('llm.chunk', lambda e: chunks.append(e.data['text']))
    bus.subscribe('llm.finished', lambda e: finals.append(e.data['text']))
    brain.process_dialogue(text)
    assert chunks == finals == [expected]
    assert [row['content'] for row in memory.get_full_messages()[-2:]] == [text, expected]
    assert brain.llm_client.select_activity_color.call_count == int(with_choice)
    brain.llm_client.stream_chat.assert_not_called()


@pytest.mark.parametrize('text', [
    '我的蓝夹收好了吗？', '蓝夹里有没有纸？也给我讲一下算法。',
    '蓝夹已经收好了，接下来看哪页？', '蓝夹和透明夹子挑一个。', '蓝夹里面放了几页和什么时候能收好？',
    '谢谢你。米白还是墨绿？然后说说她的蓝夹收完没有。',
    '谢谢你。米白还是墨绿？然后说说你的蓝夹收完没有。帮我把笔记也收好。',
    '我想安静待会儿。先帮我在银灰和米白里选一个，然后说说蓝夹到底收完没有。',
    '蓝夹收完没有，如果收完就告诉我桌上的位置。',
    '你别回答蓝夹收完没有。',
    '把“蓝夹收完没有”翻译成英文。',
    '“谢谢你。蓝夹收完没有”是别人刚说的话。',
    '谢谢你。银灰还是米白？蓝夹收完没有？',
    '米白和米白你帮我选一个，然后说说蓝夹收完没有。',
    '谢谢。然后说说蓝夹收完没有？不用回复。',
])
def test_activity_does_not_intercept_other_subjects_or_compound_requests(text):
    from brain.activity_answer import supported_question
    assert supported_question(text) is None


@pytest.mark.parametrize('enabled,text', [
    (True, '谢谢你。米白还是墨绿？然后说说你的蓝夹收完没有。顺便解释下怎么修改目录。'),
    (False, '谢谢你。米白还是墨绿？然后说说你的蓝夹收完没有。'),
])
def test_casual_activity_question_keeps_original_input_when_direct_answer_is_skipped(monkeypatch, enabled, text):
    monkeypatch.setattr(settings, 'DIALOGUE_ACTIVITY_EVIDENCE', enabled)
    brain, bus, memory, _ = seeded_brain()
    state = make_state_manager()
    state.event_bus, state.short_term_memory, state.prepare_dialogue = bus, memory, Mock()
    brain.state_manager = state
    brain.llm_client.stream_chat.return_value = iter(['完整生成的合成回复。'])
    brain.process_dialogue(text)
    brain.llm_client.select_activity_color.assert_not_called()
    brain.llm_client.stream_chat.assert_called_once()
    assert brain.llm_client.stream_chat.call_args.kwargs['messages'][-1] == {'role': 'user', 'content': text}
    assert [row['content'] for row in memory.get_full_messages()[-2:]] == [text, '完整生成的合成回复。']


def test_selector_request_has_one_cancelable_slot_and_no_retry(monkeypatch):
    client = object.__new__(LLMClient)
    cancel = Turn('selector', 0)
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    slot = threading.BoundedSemaphore(1)
    monkeypatch.setattr(client, '_recall_slot', slot)
    def block(**_):
        entered.set()
        cancel.request_cancel()
        release.wait(2)
        exited.set()
    send = Mock(side_effect=block)
    client.small_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send)))
    try:
        with pytest.raises(TurnCancelled):
            client.select_recall_sources([], cancel)
        assert entered.is_set() and send.call_count == 1
        assert not slot.acquire(blocking=False)
    finally:
        release.set()
        assert exited.wait(2)


def test_receipt_statuses_keep_positive_results_and_unknown_separate():
    from brain.reply_evidence import tool_followup
    build = lambda value: tool_followup('实际回执', [{'tool_name': 'memory_query', 'result': value}], '')
    assert '没有完成' in build(ToolResult.fail('暂时不可用'))
    assert '返回了资料' in build(ToolResult.ok(data={'found': True}))
    assert '未提供命中资料' in build(ToolResult.ok(data={'found': False}))


def test_notes_completion_is_not_a_specific_object_completion(monkeypatch):
    manager = make_state_manager()
    previous = manager.current_state
    candidate = {**previous, 'scene': {**previous['scene'], 'character_location': '宿舍'},
                 'role_activity_request': {'action': 'start', 'kind': 'notes'}}
    active = continuity.commit(previous, candidate, NOW, duration=20)
    query = '青色和米色挑一个，蓝夹收好没有？'
    manager.current_state = continuity.advance(active, NOW + 25)
    monkeypatch.setattr(settings, 'DIALOGUE_ACTIVITY_EVIDENCE', False)
    assert '【本轮可核实的活动字段】' not in manager.build_reply_context(NOW + 25, user_message=query)
    monkeypatch.setattr(settings, 'DIALOGUE_ACTIVITY_EVIDENCE', True)
    text = manager.build_reply_context(NOW + 25, user_message=query)
    fields = json.loads(text.split('【本轮可核实的活动字段】')[1].splitlines()[0])
    assert fields['流程状态'] == 'completed'
    assert fields['蓝夹是否已收好'] is None
    assert fields['已核实放入页数'] is None
    assert '【本轮可核实的活动字段】' not in manager.build_reply_context(NOW, user_message='傍晚好！')
    future = continuity.notes_progress_context(active, NOW - 1, query)
    assert json.loads(future.split('】')[1].splitlines()[0])['流程状态'] is None


def test_program_progress_preserves_known_completion_and_unverified_learning():
    data = {'activities': [{'id': 'test-timer', 'status': 'completed', 'elapsed_seconds': 60, 'target_seconds': 600}],
            'projects': [{'id': 'test-project', 'status': 'completed', 'phase': 'finished'}]}
    text = Experience._activity_evidence(data, '自习计时结束了吗，共同手册走到哪一步？')
    fields = json.loads(text.split('】')[1].splitlines()[0])
    assert fields['共同手册']['阶段'] == 'finished'
    assert fields['自习计时']['状态'] == 'completed'
    assert fields['自习计时']['已到目标时长'] is False
    assert fields['自习计时']['实际学习内容是否完成'] is None
