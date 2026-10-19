"""模型可提出模糊候选；执行前由代码要求用户确认完整计划。"""
import re


def normalized(text):
    return re.sub(r'[\s，,。！？!?、的]', '', text)


def vocabulary(snapshot):
    words = []
    for value in snapshot.values():
        if value.get('status') == 'success':
            words.extend(word for word in value.get('vocabulary', []) if isinstance(word, str) and word.strip())
    return list(dict.fromkeys(words))


def confirmation_for(plan, registry, text, snapshot):
    questions = []
    for task in plan['tasks']:
        guard = getattr(registry.load(task['skill']), 'confirmation', None) if task['action'] != 'script' else None
        question = guard(task['action'], task['args'], text, snapshot.get(task['skill'], {})) if guard else None
        if question is not None and not isinstance(question, str):
            raise ValueError('技能确认内容须为文本或空值。')
        questions.append(question)
    if not any(questions):
        return None
    # 多技能整轮等待，不提前播放音乐或执行其他副作用。
    descriptions = []
    for task, question in zip(plan['tasks'], questions):
        description = question or registry.load(task['skill']).describe(task['action'], task['args'])
        description = description.strip().removeprefix('请确认').lstrip('：: ').removeprefix('正在')
        descriptions.append(description.rstrip('。！？!? '))
    return '请确认：' + '，并且'.join(descriptions) + '？请说“确认”或“取消”。'


def confirmation_answer(text):
    answer = normalized(text)
    if answer in {'确认', '是', '是的', '对', '对的', '没错', '是这盏灯', '好', '好的', '嗯', '可以'}:
        return True
    if answer in {'取消', '不', '不是', '不对', '不要', '算了'}:
        return False
    return None
