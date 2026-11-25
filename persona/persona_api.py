"""Owner-facing editor endpoints. Trial replies use an ephemeral synthetic conversation."""
from difflib import unified_diff
import logging
import re
from threading import Lock
import uuid

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.dialogue_context import build_dialogue_messages
from brain.tag_utils import extract_thought_and_speech
from config.settings import settings
from core.service_policy import call_scope
from core.turns import Turn
from persona.persona_settings import PersonaVersion, compile_version

logger = logging.getLogger(__name__)
CORE_LIMIT = 12000
STYLE_LIMIT = 1600
TRIAL_LIMIT = 800


class PersonaDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0, strict=True)
    core_persona: str = Field(max_length=CORE_LIMIT)
    speaking_style: str = Field(default='', max_length=STYLE_LIMIT)

    @field_validator('core_persona', 'speaking_style')
    @classmethod
    def check_text(cls, text, info):
        if info.field_name == 'core_persona' and not text.strip():
            raise ValueError('请填写核心人设')
        if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]', text):
            raise ValueError('提示词中含无法保存的字符')
        return text if text.strip() else ''


class PersonaTrial(PersonaDraft):
    message: str = Field(min_length=1, max_length=TRIAL_LIMIT)

    @field_validator('message')
    @classmethod
    def check_message(cls, text):
        if not text.strip():
            raise ValueError('请填写试聊消息')
        return text


class PersonaRestore(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0, strict=True)
    revision: int = Field(ge=0, strict=True)


def trial_messages(prompt, text, now):
    history = [
        {'role': 'user', 'content': '刚才那段先放一放，我们接着聊。', 'timestamp': int((now - 60) * 1000)},
        {'role': 'assistant', 'content': '好，接着说。', 'timestamp': int((now - 30) * 1000)},
        {'role': 'user', 'content': text, 'timestamp': int(now * 1000)},
    ]
    return build_dialogue_messages(prompt, history, '【试聊场景】正在通过网页聊天，双方位置未知。'
        '以上是合成前文，没有额外的共同经历、资料或工具结果。只给对白。', now)


def install_persona_api(server):
    bus = server.event_bus
    personas = bus.persona_settings
    trial_slot = Lock()

    def owner(request: Request):
        # The hosted edition already authenticates API requests in OwnerMiddleware.
        if server.service_runtime is not None:
            return
        if not request.client or request.client.host not in ('127.0.0.1', '::1'):
            raise HTTPException(403, '请在本机打开设置，或登录 Ember 常驻服务')
        origin = request.headers.get('origin')
        if origin and origin not in (str(request.base_url).rstrip('/'), 'http://localhost:5173', 'http://127.0.0.1:5173'):
            raise HTTPException(403, '请通过 Ember 本机页面编辑人设')

    guards = [Depends(owner)]

    def checked(expected_revision):
        current = personas.prompts
        if expected_revision != current.revision:
            raise HTTPException(409, '人设已在其他页面更新。请读取当前版本，核对草稿后再操作。')
        return current

    def publish(expected_revision, core, style, restored_from=None):
        if not bus.operation_lock.acquire(blocking=False):
            raise HTTPException(409, '请等当前对话或世界操作完成后再应用人设。草稿仍保留。')
        try:
            with bus.pending_condition:
                turn = bus.turns.snapshot()
                if bus.paused or bus.pending_dialogues or (turn and not turn['released']) or server.archive_jobs.busy:
                    raise HTTPException(409, '请等当前对话或存读档完成后再应用人设。草稿仍保留。')
                previous = checked(expected_revision)
                try:
                    result = personas.apply(core, style, restored_from)
                except OSError:
                    logger.exception('Could not persist persona version')
                    raise HTTPException(503, '人设保存失败，当前版本仍在使用。请重试。') from None
                if personas.prompts.revision != previous.revision:
                    bus.world_revision += 1
                return result
        finally:
            bus.operation_lock.release()

    @server.app.get('/api/persona', dependencies=guards)
    def get_persona(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return personas.snapshot()

    @server.app.put('/api/persona', dependencies=guards)
    def apply_persona(draft: PersonaDraft):
        return publish(draft.expected_revision, draft.core_persona, draft.speaking_style)

    @server.app.post('/api/persona/restore', dependencies=guards)
    def restore_persona(selection: PersonaRestore):
        target = personas.version(selection.revision)
        if target is None:
            raise HTTPException(404, '该人设版本已不在保留范围内，请读取当前版本。')
        return publish(selection.expected_revision, target.core_persona, target.speaking_style, target.revision)

    @server.app.post('/api/persona/preview', dependencies=guards)
    def preview_persona(draft: PersonaDraft):
        current = checked(draft.expected_revision)
        compiled = compile_version(PersonaVersion(current.revision, draft.core_persona, draft.speaking_style))
        diff = unified_diff(current.reply.splitlines(), compiled.reply.splitlines(),
                            fromfile='当前版', tofile='草稿版', lineterm='')
        return {'effective': compiled.reply, 'diff': '\n'.join(diff)}

    @server.app.post('/api/persona/trial', dependencies=guards)
    def try_persona(draft: PersonaTrial):
        current = checked(draft.expected_revision)
        compiled = compile_version(PersonaVersion(current.revision, draft.core_persona, draft.speaking_style))
        if not trial_slot.acquire(blocking=False):
            raise HTTPException(409, '已有试聊正在运行，请稍后再试。')
        def reply(prompt):
            turn = Turn(key, int(now * 1000))
            chunks = server.brain.llm_client.stream_chat(settings.LARGE_LLM, trial_messages(prompt, draft.message, now),
                                                         call_type='persona_preview', cancel_token=turn)
            try:
                raw = ''.join(chunks)
            finally:
                if hasattr(chunks, 'close'):
                    chunks.close()
            stripped = re.sub(r'<(tool|tool_call)>[\s\S]*?(?:</\1>|$)', '', raw, flags=re.I)
            return extract_thought_and_speech(stripped)[1]
        try:
            now = bus.logical_now
            key = str(uuid.uuid4())
            with call_scope('foreground', key):
                before = reply(current.reply)
                after = before if compiled.reply == current.reply else reply(compiled.reply)
            return {'current': before, 'draft': after, 'revision': current.revision}
        except Exception:
            logger.warning('Persona trial failed', exc_info=True)
            raise HTTPException(502, '试聊暂时失败，请稍后重试。') from None
        finally:
            trial_slot.release()
