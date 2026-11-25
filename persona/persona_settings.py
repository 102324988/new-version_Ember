"""Installation-level persona versions, separate from world saves."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from config.dialogue_policy import adapt_stock_text, compile_persona
from config.settings import settings

HISTORY_SIZE = 12


@dataclass(frozen=True)
class PersonaVersion:
    revision: int
    core_persona: str
    speaking_style: str = ''
    created_at: str | None = None
    restored_from: int | None = None


@dataclass(frozen=True)
class PersonaPrompts:
    revision: int
    core: str
    system: str
    reply: str
    adaptations: int
    speaking_preference: str = ''

    @property
    def proactive(self):
        return self.system + self.speaking_preference


def default_prompts():
    return PersonaPrompts(0, settings.CORE_PERSONA, settings.SYSTEM_PROMPT,
                          settings.DIALOGUE_REPLY_PROMPT, settings.DIALOGUE_ADAPTATIONS)


def compile_version(version):
    if version.core_persona == settings.RAW_CORE_PERSONA and not version.speaking_style:
        base = default_prompts()
        return PersonaPrompts(version.revision, base.core, base.system, base.reply, base.adaptations)
    raw = version.core_persona
    if raw and not raw.endswith('\n') and raw != settings.RAW_CORE_PERSONA:
        raw += '\n'
    policy, style = settings.DIALOGUE_POLICY, settings.DIALOGUE_STYLE
    rules = settings.PROMPTS.get('system_prompt', '')
    system, adaptations = compile_persona(raw, rules, policy, style)
    reply = system if settings.DIALOGUE_RHYTHM == 'baseline' else compile_persona(
        raw, rules, policy, style, rhythm=settings.DIALOGUE_RHYTHM)[0]
    core = adapt_stock_text(raw)[0] if policy == 'expressive_v1' else raw
    preference = ''
    if version.speaking_style:
        preference = ('\n【用户设置的说话方式】\n以下偏好优先于默认表达习惯，只影响语气、措辞和节奏。'
                      '世界事实、当前时空、能力边界和输出格式仍适用。\n' + version.speaking_style + '\n')
    return PersonaPrompts(version.revision, core, system, reply + preference, adaptations, preference)


class PersonaSettings:
    def __init__(self, path=Path('data/state/persona-settings.json')):
        self.path = Path(path)
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            payload = [asdict(PersonaVersion(0, settings.RAW_CORE_PERSONA))]
        versions = tuple(PersonaVersion(**entry) for entry in payload)
        revisions = tuple(entry.revision for entry in versions)
        if not revisions or revisions[0] != 0 or revisions != tuple(sorted(set(revisions))):
            raise ValueError('人设版本文件内容不完整')
        self._state = versions, compile_version(versions[-1])

    @property
    def prompts(self):
        return self._state[1]

    def snapshot(self):
        versions, _ = self._state
        return {'active': asdict(versions[-1]), 'versions': [
            {'revision': entry.revision, 'created_at': entry.created_at, 'restored_from': entry.restored_from}
            for entry in reversed(versions)]}

    def version(self, revision):
        return next((entry for entry in self._state[0] if entry.revision == revision), None)

    def apply(self, core, style, restored_from=None):
        versions, current = self._state
        if (core, style) == (versions[-1].core_persona, versions[-1].speaking_style):
            return self.snapshot()
        added = PersonaVersion(current.revision + 1, core, style, datetime.now(timezone.utc).isoformat(), restored_from)
        compiled = compile_version(added)
        updated = (versions[0], *versions[1:][-(HISTORY_SIZE - 1):], added)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent, suffix='.tmp', delete=False) as file:
                temporary = Path(file.name)
                json.dump([asdict(entry) for entry in updated], file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._state = updated, compiled
        return self.snapshot()


def persona_prompts(event_bus):
    personas = getattr(event_bus, 'persona_settings', None)
    return personas.prompts if personas is not None else default_prompts()
