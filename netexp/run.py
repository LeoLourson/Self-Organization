"""Один прогон: раунды с ротацией организатора, назначение ролей из меню, голосование, оценка судьи.

Прогон — конечный автомат, состояние которого лежит в runs/state/<id>.json.
На каждом шаге среда отправляет одно сообщение одному агенту. У каждого агента
своя постоянная история: при API-транспорте она хранится в состоянии и целиком
уходит в запрос, при субагентах её держит сам субагент, а запрос несёт только
новое сообщение.

Фазы раунда: assign (организатор назначает каждому исполнителю роль из меню
и задание) → work (каждый исполнитель отвечает организатору) → forecast
(организатор даёт прогноз). После прогноза итоги раунда по правилам условия
кладутся во входящие всех агентов и приклеиваются к следующему сообщению каждому.
После последнего раунда каждый агент получает итоги вместе с просьбой подвести
итог для себя (debrief). Затем голосование (vote): каждый агент отвечает на
попарные вопросы об остальных — по одному короткому вопросу на пару, заданные
от одного и того же состояния его памяти; ответы измеряются логпробами токенов
«1» и «2» (llm.choice) и в историю не попадают. Затем судья (judge.py) оценивает
распределение ролей и ответы; агентам его оценки не передаются.

Условие solo — контроль лестницы сравнения из обзора (гл. 13.2): те же агенты,
те же окна и та же ротация, но без общения. Фазы раунда сводятся к одной —
solo (агент прогнозирует один, без ролей и исполнителей), итоги раунда видит
только сам прогнозист, а голосование и судья не проводятся: делегировать
и оценивать некого и нечего.
"""
import json
import random
import re

from . import judge, llm, series, store

CONDITIONS = ("open", "closed", "solo")
TERMINAL = ("done", "failed")
MAX_INVALID = 3
EMPTY_RETRIES = 3
VOTE_ATTEMPTS = 3


# --- описание прогона ------------------------------------------------------

def names():
    return [f"A{i + 1}" for i in range(store.config()["agents"])]


def roles():
    return store.read_json(store.ROOT / "prompts/roles.json")


def vote_profile(profile):
    """Профиль, которым измеряются попарные голоса: у субагентов логпробов нет, нужен API-профиль."""
    if profile["transport"] == "api":
        return profile
    name = store.config().get("vote_profile")
    value = store.profile(name) if name else None
    if not value or value["transport"] != "api":
        raise ValueError("Для прогонов на субагентах укажите API-профиль vote_profile в config.json")
    return value


def spec(profile, condition, repeat=0):
    """Полное описание прогона. ID — хеш описания, поэтому повторный start возвращает тот же прогон."""
    if condition not in CONDITIONS or type(repeat) is not int or repeat < 0:
        raise ValueError("Неизвестное условие или некорректный повтор")
    series.check()
    value = {"version": 4, "protocol": store.protocol_digest(), "profile": store.profile(profile),
             "condition": condition, "repeat": repeat}
    if condition == "solo":
        # Контроль без общения: голосовать и оценивать некого и нечего.
        value["vote"], value["judge"] = None, None
    else:
        value["vote"] = vote_profile(store.profile(profile))
        value["judge"] = store.profile(store.config()["judge_profile"])
    # Порядок ролей в меню свой у каждого прогона, чтобы первая в списке роль не получала преимущества.
    order = list(roles())
    random.Random(store.digest(value)).shuffle(order)
    value["role_order"] = order
    return {"id": store.digest(value), **value}


def organizer(state, round_index):
    """Циклический латинский квадрат: за agents повторов каждый агент организует каждый период."""
    agents = names()
    return agents[(round_index + state["spec"]["repeat"]) % len(agents)]


# --- состояние -------------------------------------------------------------

def text(name):
    return (store.ROOT / "prompts" / f"{name}.txt").read_text(encoding="utf-8").strip()


def load_state(ident):
    state = store.read_json(store.run_file("state", ident))
    if state["spec"]["protocol"] != store.protocol_digest():
        raise ValueError("Протокол изменён после начала прогона; восстановите тексты, данные и параметры")
    return state


def save(state):
    store.write_json(store.run_file("state", state["spec"]["id"]), state)


def start(profile, condition, repeat=0):
    store.check_stop()
    run = spec(profile, condition, repeat)
    if condition != "solo" and store.profile(store.config()["judge_profile"])["transport"] != "api":
        raise ValueError("Судья вызывается только через API")
    if not store.run_file("state", run["id"]).exists():
        agents = names()
        save({"spec": run, "phase": "solo" if condition == "solo" else "assign", "round": 0, "step": 0, "seq": 0, "invalid": 0,
              "retry": None, "pending": None, "rounds": [], "debriefs": {}, "votes": {}, "judgments": [],
              "histories": {a: [] for a in agents}, "inbox": {a: [] for a in agents},
              "sessions": {a: None for a in agents}})
    return status(run["id"])


def status(ident):
    state = load_state(ident)
    done = state["phase"] in TERMINAL
    return {"id": ident, "phase": state["phase"], "round": state["round"], "done": done,
            "result_file": store.relative(store.run_file("results", ident)) if done else None,
            "request": request_view(state) if state["pending"] else None}


# --- текущий шаг -----------------------------------------------------------

def workers(state):
    boss = organizer(state, state["round"])
    return [a for a in names() if a != boss]


def current_agent(state):
    phase = state["phase"]
    if phase in ("assign", "forecast", "solo"):
        return organizer(state, state["round"])
    if phase == "work":
        return workers(state)[state["step"]]
    # debrief: агенты по порядку; голосование и судья не ходят по сообщениям
    return names()[state["step"]]


def system_prompt(condition=None):
    """Системный промпт: общее описание мира, у solo — без ролей и организатора."""
    agents = names()
    name = "solo_system" if condition == "solo" else "system"
    return text(name).format(agents=len(agents), names=", ".join(agents))


def role_menu(order):
    catalog = roles()
    return "\n".join(f"- {name}: {catalog[name]}" for name in order)


def round_context(state, round_index=None):
    cfg, values = store.config(), series.load()
    index = state["round"] if round_index is None else round_index
    begin, origin, end = series.window(index)
    return {"round": index + 1, "rounds": cfg["agents"], "horizon": cfg["horizon"],
            "series": series.table(values[begin:origin], begin),
            "labels": ", ".join(series.label(i) for i in range(origin, end))}


def assignment_lines(assignments):
    return "\n".join(f"{a} — {item['role']}: «{item['task']}»" for a, item in assignments.items())


def step_message(state, agent):
    """Новое сообщение агенту на этом шаге, без входящих итогов."""
    if state["retry"]:
        return text("retry").format(error=state["retry"])
    phase, cfg = state["phase"], store.config()
    if phase == "debrief":
        return text("debrief")
    ctx, boss, team = round_context(state), organizer(state, state["round"]), workers(state)
    if phase == "solo":
        return text("solo").format(**ctx)
    if phase == "assign":
        example = ", ".join(f'"{a}": {{"role": "роль", "task": "задание"}}' for a in team)
        return text("assign").format(workers=", ".join(team), example=example, **ctx)
    current = state["rounds"][state["round"]]
    if phase == "work":
        item = current["assignments"][agent]
        return text("work").format(organizer=boss, role=item["role"], task=item["task"],
                                   words=cfg["worker_words"], **ctx)
    answers = "\n\n".join(f"{a} (роль «{current['assignments'][a]['role']}», "
                          f"задание: «{current['assignments'][a]['task']}»):\n{current['answers'][a]}"
                          for a in team)
    return text("forecast").format(answers=answers, **ctx)


def make_pending(state):
    """Фиксирует запрос шага вместе с входящими итогами: повтор advance отправит тот же текст."""
    agent = current_agent(state)
    parts = []
    # В solo меню ролей не приходит: ролей нет и имена для задачи не нужны.
    if not state["histories"][agent] and state["spec"]["condition"] != "solo":
        parts.append(text("intro").format(name=agent, roles=role_menu(state["spec"]["role_order"])))
    parts += state["inbox"][agent] + [step_message(state, agent)]
    state["inbox"][agent] = []
    state["seq"] += 1
    state["pending"] = {"request_id": store.digest([state["spec"]["id"], state["seq"]]),
                        "agent": agent, "message": "\n\n".join(parts)}
    save(state)


def request_view(state):
    """Запрос субагенту: только новое сообщение и ID уже созданного субагента этого агента."""
    pending = state["pending"]
    agent = pending["agent"]
    return {"request_id": pending["request_id"], "agent": agent,
            "subagent_type": store.config()["subagent_type"], "model": state["spec"]["profile"]["model"],
            "agent_id": state["sessions"][agent], "message": pending["message"]}


# --- приём ответа ----------------------------------------------------------

def parse_json(answer):
    match = re.search(r"\{.*\}", answer, re.S)
    if not match:
        raise ValueError("нет JSON-объекта")
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError as error:
        raise ValueError(f"некорректный JSON ({error.msg})") from None
    if not isinstance(value, dict):
        raise ValueError("нужен JSON-объект")
    return value


def accept(state, answer):
    """Записывает ответ в историю агента и продвигает автомат; некорректный формат даёт повторный запрос."""
    pending = state["pending"]
    agent = pending["agent"]
    state["histories"][agent] += [{"role": "user", "content": pending["message"]},
                                  {"role": "assistant", "content": answer}]
    state["pending"] = None
    try:
        apply(state, agent, answer)
        state["retry"], state["invalid"] = None, 0
    except ValueError as error:
        state["invalid"] += 1
        state["retry"] = str(error)
        if state["invalid"] >= MAX_INVALID:
            state["phase"] = "failed"
            finish(state)
    save(state)


def check_assignments(value, team):
    menu = roles()
    if not isinstance(value, dict) or sorted(value) != sorted(team):
        raise ValueError(f"нужен объект assignments с записью для каждого из {', '.join(team)}")
    for item in value.values():
        if not isinstance(item, dict) or item.get("role") not in menu \
                or not isinstance(item.get("task"), str) or not item["task"].strip():
            raise ValueError(f"у каждого агента нужны role из списка ({', '.join(menu)}) и непустой task")
    return {a: {"role": value[a]["role"], "task": value[a]["task"].strip()} for a in team}


def forecast_values(answer, horizon):
    """Список forecast из ответа: ровно horizon чисел."""
    forecast = parse_json(answer).get("forecast")
    if not isinstance(forecast, list) or len(forecast) != horizon \
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in forecast):
        raise ValueError(f"нужен список forecast из {horizon} чисел")
    return [float(x) for x in forecast]


def apply(state, agent, answer):
    phase, horizon = state["phase"], store.config()["horizon"]
    if phase == "assign":
        team = workers(state)
        assignments = check_assignments(parse_json(answer).get("assignments"), team)
        state["rounds"].append({"organizer": agent, "origin": series.window(state["round"])[1],
                                "assignments": assignments, "answers": {}})
        state["phase"], state["step"] = "work", 0
    elif phase == "work":
        if not answer:
            raise ValueError("пустой ответ")
        state["rounds"][state["round"]]["answers"][agent] = answer
        state["step"] += 1
        if state["step"] == len(workers(state)):
            state["phase"] = "forecast"
    elif phase == "forecast":
        close_round(state, forecast_values(answer, horizon))
    elif phase == "solo":
        state["rounds"].append({"organizer": agent, "origin": series.window(state["round"])[1],
                                "assignments": {}, "answers": {}})
        close_round(state, forecast_values(answer, horizon))
    elif phase == "debrief":
        if not answer:
            raise ValueError("пустой ответ")
        state["debriefs"][agent] = answer
        state["step"] += 1
        if state["step"] == len(names()):
            state["phase"], state["step"] = "vote", 0


def close_round(state, forecast):
    """Оценивает прогноз и кладёт итоги раунда по правилам условия."""
    values = series.load()
    begin, origin, end = series.window(state["round"])
    current = state["rounds"][state["round"]]
    current.update(forecast=forecast, actual=values[origin:end],
                   mase=series.mase(forecast, values[origin:end], values[begin:origin]))
    summary = store.read_json(store.ROOT / "prompts/summary.json")
    solo = state["spec"]["condition"] == "solo"
    header = summary["solo_header"] if solo else summary["header"]
    lines = [header.format(round=state["round"] + 1, organizer=current["organizer"])]
    # Точность (MASE) агентам не показывается: она нужна только для анализа.
    for part in summary["conditions"][state["spec"]["condition"]]:
        if part == "assignments":
            lines.append(summary["assignments"].format(assignments=assignment_lines(current["assignments"])))
        else:
            template = summary["solo_forecast"] if solo else summary["forecast"]
            lines.append(template.format(forecast=series.table(forecast, origin)))
    lines.append(summary["actual"].format(actual=series.table(values[origin:end], origin)))
    current["summary"] = "\n".join(lines)
    if solo:
        # Свой раунд в solo — единственный: следующего сообщения, к которому итоги
        # приклеились бы, не будет, поэтому они попадают прямо в историю прогнозиста.
        # Остальные агенты ничего о раунде не узнают — они полностью независимы.
        state["histories"][current["organizer"]] += [{"role": "user", "content": current["summary"]}]
    else:
        for agent in names():
            state["inbox"][agent].append(current["summary"])
    state["round"] += 1
    finished = state["round"] == len(names())
    if solo:
        # Голосовать и оценивать в solo некого и нечего: прогон заканчивается прогнозами.
        state["phase"] = "done" if finished else "solo"
        if finished:
            finish(state)
    else:
        state["phase"] = "debrief" if finished else "assign"
    state["step"] = 0


# --- голосование ------------------------------------------------------------

def pairs(agents):
    return [(agents[i], agents[j]) for i in range(len(agents)) for j in range(i + 1, len(agents))]


def collect_votes(state):
    """Попарные голоса: по одному короткому вопросу на каждую пару остальных.

    Вопросы не попадают в историю агента и задаются от одного и того же состояния
    его памяти после подведения итогов, поэтому ответ на один вопрос не влияет на
    остальные. Регистрируются логпробы обоих вариантов ответа — не только выбор,
    но и перевес предпочтения. Кто из пары показан первым, чередуется по хешу,
    чтобы позиция «1» не получала постоянного преимущества.
    """
    profile, system = state["spec"]["vote"], system_prompt(state["spec"]["condition"])
    for agent in names():
        history = state["histories"][agent]
        state["votes"][agent] = []
        for x, y in pairs([a for a in names() if a != agent]):
            one, two = (y, x) if int(store.digest([state["spec"]["id"], agent, x, y]), 16) % 2 else (x, y)
            question = text("pair").format(first=one, second=two)
            messages = [{"role": "system", "content": system}, *history,
                        {"role": "user", "content": question}]
            values = {"1": None, "2": None}
            for attempt in range(VOTE_ATTEMPTS):
                scope = store.digest([state["spec"]["id"], "vote", agent, x, y, attempt])
                values = llm.choice(profile, scope, messages, ("1", "2"))
                if values["1"] is not None and values["2"] is not None:
                    break
            if values["1"] is None or values["2"] is None:
                raise RuntimeError(f"{agent}: нет логпроба одного из вариантов в паре {one}–{two}; "
                                   f"проверьте эндпоинт командой check-vote")
            state["votes"][agent].append({"pair": [x, y], "one": one,
                                          "logprob": {one: values["1"], two: values["2"]}})
        save(state)


def ballot_score(ballots):
    """Суммарный перевес агента над остальными по логпробам парных сравнений."""
    score = {agent: 0.0 for ballot in ballots for agent in ballot["pair"]}
    for ballot in ballots:
        x, y = ballot["pair"]
        lp = ballot["logprob"]
        score[x] += lp[x] - lp[y]
        score[y] += lp[y] - lp[x]
    return score


def ballot_ranking(ballots):
    """Порядок остальных по сумме перевесов; при равенстве — по имени."""
    score = ballot_score(ballots)
    return sorted(score, key=lambda a: (-score[a], a))


def check_vote(profile_name):
    """Один пробный попарный вопрос к API: проверяет, что эндпоинт отдаёт логпробы, до платного прогона."""
    profile = store.profile(profile_name)
    if profile["transport"] != "api":
        raise ValueError("Проверка нужна для API-профиля: у субагентов логпробов нет")
    agents = names()
    messages = [{"role": "system", "content": system_prompt()},
                {"role": "user", "content": text("pair").format(first=agents[0], second=agents[1])}]
    scope = store.digest(["check-vote", profile["engine"], profile["model"]])
    values = llm.choice(profile, scope, messages, ("1", "2"))
    if values["1"] is None or values["2"] is None:
        raise ValueError("Один из вариантов не попал в топ-20 логпробов; попробуйте другую модель")
    return {"profile": profile_name, "model": profile["model"], "logprob": values,
            "margin": values["1"] - values["2"]}


# --- исполнение ------------------------------------------------------------

def advance(ident):
    """Исполняет API-шаги до конца прогона; при субагентах останавливается на запросе оркестратору.

    Голосование и судья всегда идут через API, в том числе в прогоне на субагентах.
    """
    state = load_state(ident)
    profile = state["spec"]["profile"]
    while state["phase"] not in TERMINAL:
        store.check_stop()
        if state["phase"] == "vote":
            collect_votes(state)
            state["phase"] = "judge"
            save(state)
            continue
        if state["phase"] == "judge":
            judge.evaluate(state, save)
            state["phase"] = "done"
            finish(state)
            save(state)
            break
        if not state["pending"]:
            make_pending(state)
        if profile["transport"] == "subagent":
            break
        pending = state["pending"]
        messages = [{"role": "system", "content": system_prompt(state["spec"]["condition"])},
                    *state["histories"][pending["agent"]],
                    {"role": "user", "content": pending["message"]}]
        # DeepSeek изредка отдаёт пустой ответ после рассуждения; такой ответ запрашивается заново,
        # и агент о нём не узнаёт. Три пустых подряд идут в accept как некорректный ответ.
        for attempt in range(EMPTY_RETRIES):
            scope = pending["request_id"] if attempt == 0 else f"{pending['request_id']}#{attempt}"
            answer = llm.call(profile, scope, messages)
            if answer:
                break
        accept(state, answer)
    return status(ident)


def submit(ident, envelope):
    """Ответ субагента. Агент должен отвечать одним и тем же субагентом весь прогон."""
    state = load_state(ident)
    pending = state["pending"]
    if not pending or envelope.get("request_id") != pending["request_id"]:
        raise ValueError("Нет ожидающего запроса с таким request_id")
    execution, profile = envelope.get("execution") or {}, state["spec"]["profile"]
    if execution.get("engine") != profile["engine"] or execution.get("model") != profile["model"]:
        raise ValueError("Движок или модель ответа не совпадают с профилем")
    agent, agent_id = pending["agent"], execution.get("agent_id")
    known = state["sessions"][agent]
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("Нужен agent_id субагента")
    if known and agent_id != known:
        raise ValueError(f"{agent} закреплён за субагентом {known}")
    if not known and agent_id in state["sessions"].values():
        raise ValueError("Этот субагент уже закреплён за другим агентом")
    if not isinstance(envelope.get("content"), str):
        raise ValueError("content должен быть строкой")
    state["sessions"][agent] = agent_id
    accept(state, envelope["content"].strip())
    return status(ident)


# --- результат -------------------------------------------------------------

def kendall_tau(ranking, reference):
    """Согласие двух порядков одних и тех же элементов: 1 — совпадают, −1 — обратны."""
    position = {item: i for i, item in enumerate(reference)}
    order = [position[item] for item in ranking]
    pairs = [(i, j) for i in range(len(order)) for j in range(i + 1, len(order))]
    return sum(1 if order[i] < order[j] else -1 for i, j in pairs) / len(pairs)


def finish(state):
    """Итог прогона: раунды с приватными ответами, голоса, оценки судьи, полные истории."""
    by_error = sorted((r for r in state["rounds"] if r.get("mase") is not None), key=lambda r: r["mase"])
    accuracy_rank = [r["organizer"] for r in by_error]
    allocation = {j["organizer"]: j["allocation"] for j in state["judgments"]
                  if j["kind"] == "allocation" and isinstance(j.get("allocation"), int)}
    # При равных оценках судьи порядок задаёт номер агента; это только вспомогательная метрика.
    judge_rank = sorted(allocation, key=lambda a: -allocation[a])
    rankings = {voter: ballot_ranking(ballots) for voter, ballots in state["votes"].items()}
    agreement = {}
    for voter, ranking in rankings.items():
        row = {}
        for name, reference in (("accuracy", accuracy_rank), ("judge_allocation", judge_rank)):
            reference = [a for a in reference if a != voter]
            if sorted(reference) == sorted(ranking):
                row[name] = kendall_tau(ranking, reference)
        agreement[voter] = row
    store.write_json(store.run_file("results", state["spec"]["id"]), {
        "spec": state["spec"], "status": state["phase"], "rounds": state["rounds"],
        "accuracy_rank": accuracy_rank, "judge_allocation_rank": judge_rank,
        "debriefs": state["debriefs"], "votes": state["votes"], "vote_rankings": rankings,
        "vote_tau": agreement, "judgments": state["judgments"], "sessions": state["sessions"],
        "system": system_prompt(), "histories": state["histories"]})
