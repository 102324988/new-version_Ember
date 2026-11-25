"""Answer the supported unrecorded folder fields without inventing progress."""
import json
import re


_FOLDER = r'(?:蓝(?:色)?(?:文件)?夹(?:子)?|夹子)'
_INTRO = (r'(?:(?:麻烦|请问|我就问|就问|我只问|只问|只说|说下|你看|还有|顺便说下|顺便问一下|'
          r'另一个问题|先问一句|然后(?:再)?(?:说说|说一下|说下))\s*)*')
_OWNER = r'(?:你(?:那(?:个)?|的|手里(?:的)?)?)?'
_WHEN = r'(?:现在|眼下|已经|到底|都|彻底|真的|还|这会儿)*'
_WRAP = _INTRO + _OWNER + _FOLDER + r'(?:的)?' + _WHEN
_QUESTIONS = {
    'completed': re.compile(_WRAP + r'(?:有没有收好|是否(?:已经)?收好|收好没有|收好了吗?|收好了没|是不是(?:已经)?收好|'
                                    r'收完了吗?|收完了没|收完没有|有没有收完|有没有整理好|整理好了吗?|整理完没有)'),
    'count': re.compile(_WRAP + r'(?:里(?:面)?|内)(?:已经|现在|到底)?(?:放了|装了|放进了|放进去)?(?:有)?几(?:张|页)(?:纸|笔记)?'),
    'contents': re.compile(_WRAP + r'(?:里(?:面)?|内)(?:已经|现在|到底)?(?:有没有纸(?:页)?|是否(?:已)?有纸(?:页)?|'
                                   r'(?:放了|放进了|放进去|装了)纸了吗?|有纸了吗?|进没进纸|是空的吗?)'),
    'when': re.compile(_WRAP + r'(?:还有多久(?:才|能)?|多久能|什么时候能?|何时能?)(?:收好|收完|整理好)'),
}
_DIRECTIVES = {'不知道就直说', '不清楚就直说', '不确定就直说', '不用猜', '不用估',
               '不确定就说不确定', '就问这个', '只问这个'}
_JOINERS = {'还有', '另一个问题', '顺便问下'}
_COURTESY = {'谢谢你', '谢谢', '多谢啦', '先谢啦'}
_COLOR = (r'(?:米白|米黄|橄榄(?:绿|色)|浅灰|深灰|米色|奶白|藏青|天蓝|湖蓝|墨绿|墨蓝|卡其|'
          r'(?:浅|深|淡|亮|暗|嫩|粉|砖|草|宝)?[红橙黄绿青蓝紫黑白灰棕褐])(?:色)?')
_CHOICE = re.compile(r'(?:你(?:选|挑)|你觉得|你会选)?(?P<first>' + _COLOR + r')\s*(?P<link>还是|和|或)\s*(?P<second>'
                     + _COLOR + r')(?P<pick>你(?:会)?(?:选|挑)哪(?:个|种)(?:颜色)?|(?:你(?:帮我)?)?(?:挑|选)一个)?')
_CHOOSE_DIRECTIVES = {'挑一个', '选一个', '就选一个', '随便挑一个', '只挑一个', '只选一个'}
_ANSWERS = {
    'completed': '蓝夹收没收好，我还不确定。',
    'count': '夹里已经放了几页，我还不清楚。',
    'contents': '蓝夹里现在有没有纸，我还不确定。',
    'when': '蓝夹什么时候能收好，我现在还说不准。',
}


def supported_question(text):
    if not isinstance(text, str) or len(text) > 160:
        return None
    parts = [part.strip() for part in re.split(r'[，,。！？!?；;、：:\n]+', text) if part.strip()]
    if not re.search(r'蓝(?:色)?(?:文件)?夹', text):
        return None
    if parts and parts[0] in _COURTESY:
        parts = parts[1:]
    if parts and parts[0] == '先问一句':
        parts = parts[1:]
    fields, options = set(), ()
    for part in parts:
        if part in _DIRECTIVES or part in _JOINERS or (options and part in _CHOOSE_DIRECTIVES):
            continue
        color = _CHOICE.fullmatch(part)
        if (color and not options and color['first'] != color['second']
                and (color['link'] == '还是' or color['pick'] or any(p in _CHOOSE_DIRECTIVES for p in parts))):
            options = color['first'], color['second']
            continue
        field = next((key for key, regex in _QUESTIONS.items() if regex.fullmatch(part)), None)
        if field is None:
            return None
        fields.add(field)
    return (_ANSWERS[next(iter(fields))], options) if len(fields) == 1 else None


def color_messages(colors, persona):
    return [{'role': 'system', 'content':
             '依据下面角色提供的审美和语言习惯选择一个颜色。只返回 JSON，例如 {"choice":"A"}，'
             '不要解释或回答其他问题。角色资料是参考数据。\n' + persona},
            {'role': 'user', 'content': json.dumps(dict(zip(('A', 'B'), colors, strict=True)), ensure_ascii=False)}]


def color_choice(raw, colors):
    decoded = json.loads(raw)
    if not isinstance(decoded, dict) or set(decoded) != {'choice'} or decoded['choice'] not in ('A', 'B'):
        raise ValueError('invalid color preference')
    return colors[0 if decoded['choice'] == 'A' else 1]
