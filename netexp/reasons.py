"""Счёт причин выбора по голосам с причиной: запросов к моделям нет.

Причина пишется после выбора и мотивом не считается, поэтому здесь считается
только то, на что она ссылается. Признаки механические и понимают английский текст,
на котором идут промпты. Что счёт не разделил, отчёт приводит выборкой для чтения человеком.
"""
import random
import re

from . import store

SEED = 20261102

ROLE_WORDS = {"historian": r"historians?", "calendar": r"calendars?", "modeler": r"modell?ers?",
              "statistician": r"statisticians?", "scenarist": r"scenarists?", "critic": r"critics?"}
ROLE_STEMS = {"historian": r"histor\w+", "calendar": r"calendar\w*", "modeler": r"model\w*",
              "statistician": r"statistic\w*", "scenarist": r"scenario\w*", "critic": r"critic\w*|critique\w*"}
BEHAVIOUR = re.compile(r"\b(?:rounds?|real values?|actual\w*|earlier|previous\w*|past|so far|demonstrated|showed"
                       r"|shown|proved|proven|identified|captured|predicted|managed|navigated|underestimated"
                       r"|overestimated|missed)\b", re.I)
FORECAST = re.compile(r"\b(?:will|would|could|can|should|likely|suited|suitable|going forward)\b", re.I)


def matched_roles(text, patterns):
    return {role for role, pattern in patterns.items() if re.search(rf"\b(?:{pattern})\b", text, re.I)}


def named_roles(text):
    """Роли, названные словом или основой."""
    return matched_roles(text, ROLE_WORDS) | matched_roles(text, ROLE_STEMS)


def role_class(text):
    """word — слово роли; stem — только основа; agent — только имя агента; none — ничего из этого."""
    if matched_roles(text, ROLE_WORDS):
        return "word"
    if matched_roles(text, ROLE_STEMS):
        return "stem"
    return "agent" if re.search(r"\bA\d+\b", text) else "none"


def behaviour_class(text):
    """behaviour — ссылка на раунды и действия; forecast — предсказание; both или none."""
    behaviour, forecast = bool(BEHAVIOUR.search(text)), bool(FORECAST.search(text))
    if behaviour and forecast:
        return "both"
    return "behaviour" if behaviour else "forecast" if forecast else "none"


def seen_roles(data, voter):
    """Какие роли других и свою голосующий видел в журнале: {агент: роли}.

    В open сводки раунда показывают все назначения; в closed агент знает только
    те назначения, что раздал сам или получил.
    """
    seen = {}
    opened = data["spec"]["condition"] == "open"
    for current in data["rounds"]:
        for agent, item in current["assignments"].items():
            if opened or voter in (current["organizer"], agent):
                seen.setdefault(agent, set()).add(item["role"])
    return seen


def journal_check(data):
    """Причины с названной ролью: (голосующий видел её у кого-то из пары, не видел)."""
    known = unknown = 0
    for voter, ballots in data["reasons"].items():
        seen = seen_roles(data, voter)
        for ballot in ballots:
            named = named_roles(ballot["reason"])
            if named:
                visible = seen.get(ballot["pair"][0], set()) | seen.get(ballot["pair"][1], set())
                known += int(bool(named & visible))
                unknown += int(not named & visible)
    return known, unknown


def margin_for(ballot, agent):
    """Перевес агента в паре или None, если логпробов нет."""
    other = next(a for a in ballot["pair"] if a != agent)
    values = ballot["logprob"]
    return None if values[agent] is None or values[other] is None else values[agent] - values[other]


def format_shift(subset):
    """Сдвиг перевеса от вопроса о причине: сравниваются только ответы одного провайдера."""
    shifts, sign_differs, no_margin = [], 0, 0
    for data in subset:
        for voter, ballots in data.get("reasons", {}).items():
            digits = {tuple(ballot["pair"]): ballot for ballot in data["votes"].get(voter, [])}
            for ballot in ballots:
                digit = digits.get(tuple(ballot["pair"]))
                if digit is None:
                    continue
                digit_margin = margin_for(digit, ballot["picked"])
                sign_differs += int(digit_margin < 0)
                json_margin = margin_for(ballot, ballot["picked"])
                if json_margin is None:
                    no_margin += 1
                elif digit.get("provider") == ballot.get("provider"):
                    shifts.append(abs(json_margin - digit_margin))
    return {"pairs": len(shifts), "mean_shift": sum(shifts) / len(shifts) if shifts else None,
            "sign_differs": sign_differs, "no_margin": no_margin}


def all_ballots(subset):
    return [(data, ballot) for data in subset for ballots in data.get("reasons", {}).values() for ballot in ballots]


def counts_row(condition, subset):
    block = [ballot["reason"] for _, ballot in all_ballots(subset)]
    roles = [role_class(text) for text in block]
    kinds = [behaviour_class(text) for text in block]
    known, unknown = map(sum, zip(*(journal_check(data) for data in subset)))
    cells = [len(block), *(roles.count(name) for name in ("word", "stem", "agent", "none")), known, unknown,
             *(kinds.count(name) for name in ("behaviour", "forecast", "both", "none"))]
    return f"| `{condition}` | " + " | ".join(str(cell) for cell in cells) + " |"


def quotes(subset):
    """Выборка причин, которые механический счёт не разделил: число ограничено report_quotes."""
    unclear = sorted({ballot["reason"] for _, ballot in all_ballots(subset)
                      if behaviour_class(ballot["reason"]) in ("both", "none") or role_class(ballot["reason"]) == "none"})
    picked = random.Random(SEED).sample(unclear, min(len(unclear), store.config()["report_quotes"]))
    if not picked:
        return []
    return ["Для чтения человеком — причины, которые счёт не разделил:", "",
            *(f"- «{text}»" for text in picked), ""]


def control_row(data):
    record = data["control"]
    picks = [call["picked"] for call in record["calls"]]
    leader = max(sorted(set(picks)), key=picks.count)
    digit = next((b for b in data["votes"].get(record["agent"], []) if b["pair"] == record["pair"]), None)
    margin = "—" if digit is None else f"{abs(margin_for(digit, record['pair'][0])):.2f}"
    return (f"| {data['spec']['id'][:8]} | {record['agent']} | {record['pair'][0]}–{record['pair'][1]} | {margin} "
            f"| {leader}: {picks.count(leader)} из {len(picks)} |")


def section(subset):
    """Блок серии о причинах, влиянии вопроса о причине и устойчивости выбора; пуст без голосов с причиной."""
    voted = [data for data in subset if data.get("reasons")]
    if not voted:
        return []
    lines = ["### Причины выбора", "",
             "| Условие | Причин | Слово роли | Только основа | Только агент | Ни то ни другое "
             "| Роль видел | Роль не видел | Поведение | Прогноз | И то и другое | Ни то ни другое |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for condition in ("open", "closed"):
        block = [data for data in voted if data["spec"]["condition"] == condition]
        if block:
            lines.append(counts_row(condition, block))
    lines += ["", *quotes(voted)]
    shift = format_shift(voted)
    mean = "—" if shift["mean_shift"] is None else f"{shift['mean_shift']:.2f}"
    providers = {}
    for _, ballot in all_ballots(voted):
        name = ballot.get("provider") or "неизвестен"
        providers[name] = providers.get(name, 0) + 1
    lines += ["### Влияние вопроса о причине", "",
              f"Средний сдвиг перевеса между голосом цифрой и цифрой внутри JSON — {mean} ната на {shift['pairs']} парах "
              f"с одним провайдером. Названная сторона расходится со знаком перевеса голоса цифрой в "
              f"{shift['sign_differs']} парах; логпробов внутри JSON нет в {shift['no_margin']}. Провайдеры голосов "
              "с причиной: " + ", ".join(f"{name} {count}" for name, count in sorted(providers.items())) + ".", ""]
    controlled = [data for data in voted if data.get("control")]
    if controlled:
        lines += ["### Устойчивость выбора", "", "| Запуск | Агент | Пара | Перевес цифрой, абс. | Чаще названный |",
                  "|---|---|---|---|---|", *(control_row(data) for data in controlled), ""]
    return lines
