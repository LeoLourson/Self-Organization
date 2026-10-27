"""Судья: оценивает распределение ролей у каждого организатора и каждый ответ исполнителя.

Вызывается после голосования, поэтому агенты его оценок не видят. Судья видит
ряд, который видел участник, но не реальный исход раунда: оценивается работа,
а не удача. Имя агента судье не показывается. Каждая оценка сохраняется сразу:
после обрыва прогон продолжит с первой неоценённой.
"""
from . import llm, run as runner, store

ATTEMPTS = 3


def items(state):
    """Все оценки прогона по порядку: распределение раунда, затем ответы его исполнителей."""
    catalog = runner.roles()
    for index, current in enumerate(state["rounds"]):
        ctx = runner.round_context(state, index)
        yield ({"kind": "allocation", "round": index, "organizer": current["organizer"]},
               runner.text("judge_allocation").format(
                   roles=runner.role_menu(state["spec"]["role_order"]), series=ctx["series"],
                   labels=ctx["labels"], assignments=runner.assignment_lines(current["assignments"])),
               {"allocation"})
        for agent, item in current["assignments"].items():
            yield ({"kind": "answer", "round": index, "agent": agent, "role": item["role"]},
                   runner.text("judge_answer").format(
                       role=item["role"], role_description=catalog[item["role"]], task=item["task"],
                       series=ctx["series"], labels=ctx["labels"], answer=current["answers"][agent]),
                   {"role_fit", "usefulness"})


def score(answer, fields):
    value = runner.parse_json(answer)
    if not all(type(value.get(f)) is int and 1 <= value[f] <= 5 for f in fields):
        raise ValueError(f"нужны целые {', '.join(sorted(fields))} от 1 до 5")
    return {**{f: value[f] for f in fields}, "comment": str(value.get("comment", ""))}


def evaluate(state, save):
    profile = state["spec"]["judge"]
    system = runner.text("judge_system")
    for index, (meta, prompt, fields) in enumerate(items(state)):
        if index < len(state["judgments"]):
            continue
        store.check_stop()
        result = {**meta, "error": None}
        for attempt in range(ATTEMPTS):
            scope = store.digest([state["spec"]["id"], "judge", index, attempt])
            answer = llm.call(profile, scope, [{"role": "system", "content": system},
                                               {"role": "user", "content": prompt}])
            try:
                result.update(score(answer, fields), error=None)
                break
            except ValueError as error:
                result["error"] = f"{error}; ответ: {answer[:200]}"
        state["judgments"].append(result)
        save(state)
