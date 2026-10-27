"""Один прогон: раунды с ротацией организатора и итоговое голосование.

Прогон — конечный автомат, состояние которого лежит в runs/state/<id>.json.
На каждом шаге среда отправляет одно сообщение одному агенту. У каждого агента
своя постоянная история: при API-транспорте она хранится в состоянии и целиком
уходит в запрос, при субагентах её держит сам субагент, а запрос несёт только
новое сообщение.

Фазы раунда: assign (организатор раздаёт задачи) → work (каждый исполнитель
отвечает организатору) → forecast (организатор даёт прогноз). После прогноза
итоги раунда по правилам условия кладутся во входящие всех агентов и
приклеиваются к следующему сообщению каждому. После последнего раунда — vote.
"""
import json
import re

from . import llm, series, store

CONDITIONS = ("full", "score", "roles", "blind")
TERMINAL = ("done", "failed")
MAX_INVALID = 3


# --- описание прогона ------------------------------------------------------

def names():
    return [f"A{i + 1}" for i in range(store.config()["agents"])]


def spec(profile, condition, repeat=0):
    """Полное описание прогона. ID — хеш описания, поэтому повторный start возвращает тот же прогон."""
    if condition not in CONDITIONS or type(repeat) is not int or repeat < 0:
        raise ValueError("Неизвестное условие или некорректный повтор")
    series.check()
    value = {"version": 1, "protocol": store.protocol_digest(), "profile": store.profile(profile),
             "condition": condition, "repeat": repeat}
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
    if not store.run_file("state", run["id"]).exists():
        agents = names()
        save({"spec": run, "phase": "assign", "round": 0, "step": 0, "seq": 0, "invalid": 0,
              "retry": None, "pending": None, "rounds": [], "votes": {},
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
    if phase in ("assign", "forecast"):
        return organizer(state, state["round"])
    if phase == "work":
        return workers(state)[state["step"]]
    return names()[state["step"]]


def system_prompt():
    agents = names()
    return text("system").format(agents=len(agents), names=", ".join(agents))


def round_context(state):
    cfg, values = store.config(), series.load()
    begin, origin, end = series.window(state["round"])
    return {"round": state["round"] + 1, "rounds": cfg["agents"], "horizon": cfg["horizon"],
            "series": series.table(values[begin:origin], begin),
            "labels": ", ".join(series.label(i) for i in range(origin, end))}


def step_message(state, agent):
    """Новое сообщение агенту на этом шаге, без входящих итогов."""
    if state["retry"]:
        return text("retry").format(error=state["retry"])
    phase, cfg = state["phase"], store.config()
    if phase == "vote":
        others = [a for a in names() if a != agent]
        return text("vote").format(rounds=cfg["agents"], others=", ".join(others), count=len(others))
    ctx, boss, team = round_context(state), organizer(state, state["round"]), workers(state)
    if phase == "assign":
        example = ", ".join(f'"{a}": "задача"' for a in team)
        return text("assign").format(workers=", ".join(team), example=example, **ctx)
    current = state["rounds"][state["round"]]
    if phase == "work":
        return text("work").format(organizer=boss, task=current["tasks"][agent],
                                   words=cfg["worker_words"], **ctx)
    answers = "\n\n".join(f"{a} (задача: «{current['tasks'][a]}»):\n{current['answers'][a]}" for a in team)
    return text("forecast").format(answers=answers, **ctx)


def make_pending(state):
    """Фиксирует запрос шага вместе с входящими итогами: повтор advance отправит тот же текст."""
    agent = current_agent(state)
    parts = [] if state["histories"][agent] else [text("intro").format(name=agent)]
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


def apply(state, agent, answer):
    phase, horizon = state["phase"], store.config()["horizon"]
    if phase == "assign":
        team = workers(state)
        tasks = parse_json(answer).get("tasks")
        if not isinstance(tasks, dict) or sorted(tasks) != sorted(team) \
                or not all(isinstance(t, str) and t.strip() for t in tasks.values()):
            raise ValueError(f"нужен объект tasks с непустой задачей для каждого из {', '.join(team)}")
        state["rounds"].append({"organizer": agent, "origin": series.window(state["round"])[1],
                                "tasks": tasks, "answers": {}})
        state["phase"], state["step"] = "work", 0
    elif phase == "work":
        if not answer:
            raise ValueError("пустой ответ")
        state["rounds"][state["round"]]["answers"][agent] = answer
        state["step"] += 1
        if state["step"] == len(workers(state)):
            state["phase"] = "forecast"
    elif phase == "forecast":
        forecast = parse_json(answer).get("forecast")
        if not isinstance(forecast, list) or len(forecast) != horizon \
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in forecast):
            raise ValueError(f"нужен список forecast из {horizon} чисел")
        close_round(state, [float(x) for x in forecast])
    elif phase == "vote":
        vote = parse_json(answer)
        others = [a for a in names() if a != agent]
        if not isinstance(vote.get("ranking"), list) or sorted(map(str, vote["ranking"])) != others \
                or not isinstance(vote.get("reason"), str):
            raise ValueError(f"ranking должен содержать ровно {', '.join(others)}, reason — строку")
        state["votes"][agent] = {"ranking": vote["ranking"], "reason": vote["reason"]}
        state["step"] += 1
        if state["step"] == len(names()):
            state["phase"] = "done"
            finish(state)


def close_round(state, forecast):
    """Оценивает прогноз и кладёт итоги раунда во входящие всех агентов по правилам условия."""
    values = series.load()
    begin, origin, end = series.window(state["round"])
    current = state["rounds"][state["round"]]
    current.update(forecast=forecast, actual=values[origin:end],
                   mase=series.mase(forecast, values[origin:end], values[begin:origin]))
    summary = store.read_json(store.ROOT / "prompts/summary.json")
    lines = [summary["header"].format(round=state["round"] + 1, organizer=current["organizer"])]
    for part in summary["conditions"][state["spec"]["condition"]]:
        if part == "tasks":
            tasks = "\n".join(f"{a}: «{t}»" for a, t in current["tasks"].items())
            lines.append(summary["tasks"].format(tasks=tasks))
        else:
            mase = "не определена" if current["mase"] is None else f"{current['mase']:.2f}"
            lines.append(summary["accuracy"].format(mase=mase))
    lines.append(summary["actual"].format(actual=series.table(values[origin:end], origin)))
    current["summary"] = "\n".join(lines)
    for agent in names():
        state["inbox"][agent].append(current["summary"])
    state["round"] += 1
    state["phase"], state["step"] = ("vote" if state["round"] == len(names()) else "assign"), 0


# --- исполнение ------------------------------------------------------------

def advance(ident):
    """Исполняет API-шаги до конца прогона; при субагентах останавливается на запросе оркестратору."""
    state = load_state(ident)
    profile = state["spec"]["profile"]
    while state["phase"] not in TERMINAL:
        store.check_stop()
        if not state["pending"]:
            make_pending(state)
        if profile["transport"] == "subagent":
            break
        pending = state["pending"]
        messages = [{"role": "system", "content": system_prompt()}, *state["histories"][pending["agent"]],
                    {"role": "user", "content": pending["message"]}]
        accept(state, llm.call(profile, pending["request_id"], messages))
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
    """Итог прогона: раунды с приватными ответами, голоса, согласие голосов с точностью, полные истории."""
    by_error = sorted((r for r in state["rounds"] if r.get("mase") is not None), key=lambda r: r["mase"])
    accuracy_rank = [r["organizer"] for r in by_error]
    agreement = {}
    for voter, vote in state["votes"].items():
        reference = [a for a in accuracy_rank if a != voter]
        if sorted(reference) == sorted(vote["ranking"]):
            agreement[voter] = kendall_tau(vote["ranking"], reference)
    store.write_json(store.run_file("results", state["spec"]["id"]), {
        "spec": state["spec"], "status": state["phase"], "rounds": state["rounds"],
        "accuracy_rank": accuracy_rank, "votes": state["votes"], "vote_accuracy_tau": agreement,
        "sessions": state["sessions"], "system": system_prompt(), "histories": state["histories"]})
