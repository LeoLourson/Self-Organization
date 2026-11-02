"""Голосование: каждая пара решается дважды, и один агент повторяет голос в контрольной паре.

Голос цифрой о причине не спрашивает: регистрируются логпробы токенов «1» и «2», поэтому
виден перевес предпочтения. Голос с причиной — JSON с полями choice и reason; цифра идёт
первой, и причина исходит из уже названной стороны, поэтому мотивом не считается. Вопросы
задаются от одного состояния памяти агента после debrief и в историю не попадают, поэтому
ответы не влияют друг на друга. Кто из пары показан первым, чередуется по хешу запуска,
в обоих форматах одинаково, чтобы позиция «1» не получала постоянного преимущества.

Контрольная пара задана хешем заранее и только описывает устойчивость выбора: порогов
и исключений по ней нет.
"""
from . import llm, run as runner, store

DIGIT_ATTEMPTS = 3
REASON_ATTEMPTS = 3     # обычный вопрос
CLARIFIED_ATTEMPTS = 2  # уточнённый вопрос: модель ответила не 1 и не 2


def shown_order(ident, agent, x, y):
    """Кто из пары показан первым: один порядок на оба формата голоса."""
    return (y, x) if int(store.digest([ident, agent, x, y]), 16) % 2 else (x, y)


def messages(state, agent, content):
    system = runner.system_prompt(state["spec"]["condition"])
    return [{"role": "system", "content": system}, *state["histories"][agent],
            {"role": "user", "content": content}]


def question(first, second, clarified=False):
    """Тот же вопрос пары, но в ответ требуются выбор и причина одним JSON.

    clarified добавляет прямое указание: пара состоит ровно из двух агентов. Без него
    модель иногда отвечает «выбор 3» — как будто в паре есть третий вариант.
    """
    fields = {"first": first, "second": second, "words": store.config()["reason_words"]}
    parts = runner.text("pair").split("\n\n")
    text = (f"{parts[0]}\n\n{parts[1].format(**fields)}\n\n"
            + runner.text("pair_reason").format(**fields))
    return f"{text}\n\n{runner.text('pair_clarify').format(**fields)}" if clarified else text


def parse(value):
    """Выбор и причина из ответа; ValueError — если поля нет, цифра не 1/2, причина раньше выбора или длиннее лимита."""
    content = value["content"]
    data = runner.parse_json(content)
    choice, reason = data.get("choice"), str(data.get("reason", "")).strip()
    if isinstance(choice, str) and choice.strip() in ("1", "2"):
        choice = int(choice.strip())
    if type(choice) is not int or choice not in (1, 2):
        raise ValueError(f"нужен choice 1 или 2; ответ: {content[:200]}")
    if not reason:
        raise ValueError(f"нужна причина выбора; ответ: {content[:200]}")
    if content.index('"reason"') < content.index('"choice"'):
        raise ValueError(f"причина должна идти после выбора; ответ: {content[:200]}")
    if len(reason.split()) > store.config()["reason_words"]:
        raise ValueError(f"причина длиннее лимита; ответ: {content[:200]}")
    return {"choice": choice, "reason": reason}


def digit_vote(state, agent, x, y):
    """Голос цифрой: логпробы обоих вариантов и провайдер."""
    ident, profile = state["spec"]["id"], state["spec"]["vote"]
    one, two = shown_order(ident, agent, x, y)
    asked = messages(state, agent, runner.text("pair").format(first=one, second=two))
    values = {"1": None, "2": None}
    for attempt in range(DIGIT_ATTEMPTS):
        scope = store.digest([ident, "vote", agent, x, y, attempt])
        values = llm.choice(profile, scope, asked, ("1", "2"))
        if values["1"] is not None and values["2"] is not None:
            break
    if values["1"] is None or values["2"] is None:
        raise RuntimeError(f"{agent}: нет логпроба одного из вариантов в паре {one}–{two}; "
                           f"проверьте эндпоинт командой check-vote")
    return {"pair": [x, y], "one": one, "logprob": {one: values["1"], two: values["2"]},
            "provider": values.get("provider")}


def reason_vote(state, agent, x, y, tag="reason"):
    """Голос с причиной. Неверный или слишком длинный ответ не принимается: вопрос задаётся заново."""
    ident, profile = state["spec"]["id"], state["spec"]["vote"]
    one, two = shown_order(ident, agent, x, y)
    for attempt in range(REASON_ATTEMPTS + CLARIFIED_ATTEMPTS):
        asked = messages(state, agent, question(one, two, clarified=attempt >= REASON_ATTEMPTS))
        value = llm.ballot(profile, store.digest([ident, tag, agent, x, y, attempt]), asked)
        try:
            parsed = parse(value)
        except ValueError:
            continue
        return {"pair": [x, y], "one": one, "picked": one if parsed["choice"] == 1 else two,
                "reason": parsed["reason"],
                "logprob": {one: value["logprob"]["1"], two: value["logprob"]["2"]},
                "provider": value.get("provider"), "format": value.get("format"), "attempts": attempt + 1}
    raise RuntimeError(f"{agent} не дал выбора с причиной в паре {one}–{two}")


def control_pair(ident):
    """Агент и пара контроля устойчивости: заданы хешем запуска до всех ответов."""
    agents = runner.names()
    agent = agents[int(store.digest([ident, "control-agent"]), 16) % len(agents)]
    options = runner.pairs([a for a in agents if a != agent])
    x, y = options[int(store.digest([ident, "control-pair"]), 16) % len(options)]
    return agent, x, y


def control(state):
    """Повторы голоса с причиной в контрольной паре: отдельные вызовы, а не копии основного голоса."""
    ident = state["spec"]["id"]
    agent, x, y = control_pair(ident)
    calls = [reason_vote(state, agent, x, y, tag=f"control-{repeat}")
             for repeat in range(store.config()["control_repeats"])]
    return {"agent": agent, "pair": [x, y], "one": shown_order(ident, agent, x, y)[0], "calls": calls}


def collect(state, save):
    """Оба голоса каждого агента и контроль; после обрыва оплаченные ответы берутся из кэша."""
    agents = runner.names()
    for agent in agents:
        options = runner.pairs([a for a in agents if a != agent])
        state["votes"][agent] = [digit_vote(state, agent, x, y) for x, y in options]
        state["reasons"][agent] = [reason_vote(state, agent, x, y) for x, y in options]
        save(state)
    state["control"] = control(state)
    save(state)


def sign_votes(reasons):
    """Голоса с причиной в форме голосов цифрой: перевес — только знак названной стороны.

    Предпочтение в этом голосе — то, кого агент назвал, а логпробы цифры внутри JSON
    служат отдельному сравнению форматов. Форма та же, поэтому все метрики считаются одним кодом.
    """
    votes = {}
    for voter, ballots in reasons.items():
        votes[voter] = []
        for ballot in ballots:
            other = next(a for a in ballot["pair"] if a != ballot["picked"])
            votes[voter].append({"pair": ballot["pair"], "one": ballot["one"],
                                 "logprob": {ballot["picked"]: 0.0, other: -1.0}})
    return votes
