"""模型可提出模糊候选；执行前由代码要求用户确认完整计划。"""
import re


def normalized(text):
    return re.sub(r'[\s，,。！？!?、的]', '', text)


def vocabulary(snapshot):
    words = ['元仔', 'PrimeBuddy']
    for value in snapshot.values():
        if value.get('status') == 'success':
            words.extend(word for word in value.get('vocabulary', []) if isinstance(word, str) and word.strip())
    return list(dict.fromkeys(words))


def confirmation_for(plan, registry, text, snapshot):
    questions = []
    for task in plan['tasks']:
        if task['action'] == 'script':
            continue
        guard = getattr(registry.load(task['skill']), 'confirmation', None)
        if guard:
            question = guard(task['action'], task['args'], text, snapshot.get(task['skill'], {}))
            if question:
                questions.append(question)
    if not questions:
        return None
    # 多技能整轮等待，不提前播放音乐或执行其他副作用。
    descriptions = [registry.load(t['skill']).describe(t['action'], t['args']).removeprefix('正在')
                    for t in plan['tasks']]
    return '请确认：' + '，并且'.join(descriptions) + '？请说“确认”或“取消”。'


def confirmation_answer(text):
    answer = normalized(text)
    if answer in {'确认', '是', '是的', '对', '对的', '没错', '是这盏灯', '好', '好的', '嗯', '可以'}:
        return True
    if answer in {'取消', '不', '不是', '不对', '不要', '算了'}:
        return False
    return None
