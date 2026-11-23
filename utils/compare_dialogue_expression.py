"""Synthetic fixed-context comparison; default prepares prompts without API IO."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def chdir(path):
    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    cases = json.loads((ROOT/'utils/dialogue_expression_cases.json').read_text(encoding='utf-8'))
    parser = argparse.ArgumentParser()
    parser.add_argument('--real-model', action='store_true')
    parser.add_argument('--full-review', action='store_true', help=f'Explicitly allow all {len(cases)} pairs ({2*len(cases)} requests)')
    parser.add_argument('--resume', action='store_true', help='Resume only identical prepared contexts after a network failure')
    parser.add_argument('--tag', default='', help='Keep a separate named comparison report')
    parser.add_argument('--comparison', choices=['policy', 'rhythm', 'repeat'], default='policy',
        help='Compare full policies, only the rhythm passage (A/B), or identical baselines (A/A)')
    parser.add_argument('--sequences', action='store_true',
        help='Use scripted multi-turn sequences with model replies carried into the next turn')
    parser.add_argument('--max-tokens', type=int, help='Optional provider output cap for deliberately bounded trials')
    parser.add_argument('--case', action='append', choices=[case['id'] for case in cases])
    args = parser.parse_args()
    if args.tag and (not args.tag.replace('-', '').replace('_', '').isalnum() or len(args.tag) > 40):
        parser.error('Report tag must contain only letters, digits, dash and underscore')
    if args.max_tokens is not None and not 128 <= args.max_tokens <= 2048:
        parser.error('--max-tokens must be between 128 and 2048')
    available = [case['id'] for case in cases if bool(case.get('followups')) == args.sequences]
    selected = args.case or (available[:2] if args.real_model and not args.full_review else available)
    if len(set(selected)) != len(selected) or set(selected) - set(available) or args.real_model and len(selected) > (len(available) if args.full_review else 2):
        parser.error('Use --full-review for more than two real paired cases')
    count = sum(1 + len(case.get('followups', [])) for case in cases if case['id'] in selected) * 2
    if args.real_model and count > (32 if args.full_review else 12):
        parser.error('Selected sequences exceed the real request cap')
    from dotenv import dotenv_values
    values = dotenv_values(ROOT/'deploy/service.env' if (ROOT/'deploy/service.env').exists() else ROOT/'.env') if args.real_model else {}
    if args.real_model and not all(values.get(key) for key in ('LARGE_LLM_API_KEY', 'LARGE_LLM_BASE_URL', 'LARGE_LLM_MODEL')):
        parser.error('Missing model configuration')
    output = ROOT/'output/dialogue-expression'
    output.mkdir(parents=True, exist_ok=True)
    report, client = [], None
    report_name = 'full-real-comparison.json' if args.full_review else 'real-comparison.json'
    prepared_name = 'prepared-cases.json'
    if args.comparison != 'policy':
        report_name = report_name.replace('.json', '-' + args.comparison + '.json')
        prepared_name = prepared_name.replace('.json', '-' + args.comparison + '.json')
    if args.sequences:
        report_name = report_name.replace('.json', '-sequences.json')
        prepared_name = prepared_name.replace('.json', '-sequences.json')
    if args.tag:
        report_name = report_name.replace('.json', '-' + args.tag + '.json')
    if args.resume and args.real_model and (output/report_name).exists():
        report = json.loads((output/report_name).read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='ember-expression-', dir=output) as folder, chdir(folder):
        target = Path(folder)/'config'
        target.mkdir()
        shutil.copy2(ROOT/'config/prompts.yaml', target/'prompts.yaml')
        os.environ['EMBER_NO_DOTENV'] = '1'
        sys.path.insert(0, str(ROOT))
        from brain.dialogue_context import build_dialogue_messages
        from brain.tag_utils import extract_thought_and_speech
        from brain.reasoning_filter import remove_reasoning
        from config.dialogue_policy import compile_persona
        from config.settings import settings
        from core.event_bus import EventBus
        from core.time_utils import to_epoch
        from persona.state_manager import StateManager
        from persona.reply_view import make_reply_view
        if args.real_model:
            from openai import OpenAI
            client = OpenAI(api_key=values['LARGE_LLM_API_KEY'], base_url=values['LARGE_LLM_BASE_URL'], max_retries=0, timeout=25)
        try:
            for index, case in enumerate(case for case in cases if case['id'] in selected):
                bus = EventBus()
                bus.pause_world(True)
                bus.restore_logical_time(case['time'])
                now = to_epoch(case['time'])
                history = case['history']
                manager = StateManager(bus, SimpleNamespace(), SimpleNamespace(get_memory=lambda: {'history': history}), SimpleNamespace())
                manager.current_state = {'P': 5, 'A': 5, 'D': 5, '对应时间': case['time'], '客观情境': '宿舍里，整理笔记',
                    '近期目标': case['intent'], '内心活动': case['feeling'], '近期综合轨迹': '仅为合成评估',
                    'scene_status': 'ready', 'state_source': 'interaction', 'scene': {
                        'character_location': '宿舍', 'user_location': '宿舍' if case.get('mode') == 'face_to_face' else None,
                        'communication_mode': case.get('mode', 'unknown'), 'remote_contact_confirmed': False}}
                manager.current_state['reply_perspective'] = make_reply_view({'reply_perspective': case}, now, 'dialogue')
                # Alternate request order; same model, parameters and history in each pair.
                variants = {'policy': [('legacy', 'legacy', 'baseline'), ('expressive_v1', 'expressive_v1', 'baseline')],
                    'rhythm': [('baseline', 'expressive_v1', 'baseline'), ('turn_v1', 'expressive_v1', 'turn_v1')],
                    'repeat': [('baseline_1', 'expressive_v1', 'baseline'), ('baseline_2', 'expressive_v1', 'baseline')]}[args.comparison]
                reference = None
                for label, version, rhythm in (variants if index % 2 == 0 else variants[::-1]):
                    settings.DIALOGUE_POLICY = version
                    settings.ROLE_CONTINUITY_ENABLED = version == 'expressive_v1'
                    system, _ = compile_persona(settings.RAW_CORE_PERSONA, settings.PROMPTS.get('system_prompt', ''),
                        version, settings.DIALOGUE_STYLE, rhythm=rhythm)
                    messages = build_dialogue_messages(system, history, manager.prompt_injection, now, policy_version=version)
                    if reference is None:
                        reference = messages
                    elif args.comparison == 'repeat' and messages != reference:
                        raise SystemExit('Identical-baseline comparison got different prompts')
                    elif args.comparison == 'rhythm' and messages[1:] != reference[1:]:
                        raise SystemExit('Rhythm-only comparison changed dialogue history')
                    record = {'case': case['id'], 'version': label, 'messages': messages,
                        'max_tokens': args.max_tokens}
                    previous = next((row for row in report if row['case'] == case['id'] and row['version'] == label and 'reply' in row), None)
                    if previous:
                        if previous['messages'] != messages or previous.get('max_tokens', 450) != args.max_tokens:
                            raise SystemExit('Saved context changed; do not resume this report')
                        continue
                    if client:
                        transcript = list(history)
                        record['followups'] = []
                        for step, followup in enumerate([None, *case.get('followups', [])]):
                            if followup is not None:
                                at = now + followup['after_seconds']
                                bus.restore_logical_time(at)
                                transcript.append({'role': 'user', 'content': followup['content'], 'timestamp': int(at * 1000)})
                                messages = build_dialogue_messages(system, transcript, manager.prompt_injection, at, policy_version=version)
                            started = time.monotonic()
                            try:
                                limits = {} if args.max_tokens is None else {'max_tokens': args.max_tokens}
                                result = client.chat.completions.create(model=values['LARGE_LLM_MODEL'], messages=messages,
                                    temperature=.5, extra_body={'enable_thinking': False}, **limits)
                            except Exception as exc:
                                record['error'] = type(exc).__name__
                                (output/'failed-comparison.json').write_text(json.dumps([*report, record], ensure_ascii=False, indent=2), encoding='utf-8')
                                raise SystemExit(f'Model comparison stopped: {type(exc).__name__}; no automatic retries') from None
                            choice = result.choices[0]
                            reply = extract_thought_and_speech(remove_reasoning(choice.message.content or ''))[1]
                            stats = {'model_ms': round((time.monotonic() - started) * 1000), 'finish_reason': choice.finish_reason}
                            if step == 0:
                                record.update(reply=reply, **stats)
                            else:
                                record['followups'].append({'user': followup['content'], 'reply': reply, **stats})
                            transcript.append({'role': 'assistant', 'content': reply, 'timestamp': int(bus.logical_now * 1000),
                                'completion_status': 'completed' if choice.finish_reason == 'stop' else 'interrupted'})
                        bus.restore_logical_time(now)
                        print(json.dumps({'case': case['id'], 'version': label, 'generated': True}, ensure_ascii=False), flush=True)
                    report.append(record)
                    (output/(report_name if client else prepared_name)).write_text(
                        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            if client:
                client.close()
    print(json.dumps({'cases': len(selected),
        'completed_reply_samples': sum(1 + len(row['followups']) for row in report) if args.real_model else 0,
        'business_database_accessed': False, 'quality': 'requires human review; synthetic perspectives supplied'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
