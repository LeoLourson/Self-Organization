"""Анализ прогонов: какие роли раздают организаторы, закрепляются ли роли за агентами,
повторяется ли состав ролей у сменяющихся центров, какую иерархию выстраивает
голосование и взаимны ли предпочтения. Запросов к моделям нет.

Читает готовые прогоны из runs/results и незавершённые — из runs/state (те, где есть
все раунды и все голоса). Каждая статистика считается против случайного уровня:
закрепление ролей — перестановочный тест (те же наборы ролей, случайно розданные тем
же исполнителям), повтор состава — независимые выборы из эмпирических наборов
условия, линейность иерархии — сравнение с голосованием случайных voters.
Терминология — обзора «Социумы ИИ-агентов» (literature-review.up.railway.app):
закрепление ролей — элемент уклада, повтор состава — конвенция, голосование —
делегирование. Отчёт собирается в Markdown: python run.py analyze [--save].
Числа идут без выводов: какую гипотезу они поддерживают, решает человек.
"""
import itertools
import math
import random

from . import run, store

SIMULATIONS = 20000
SEED = 20261101


# --- данные -----------------------------------------------------------------

def runs():
    """Прогоны с полными раундами и голосами: готовые результаты и незавершённые состояния."""
    agents = store.config()["agents"]
    found = []
    for path in sorted((store.ROOT / "runs/results").glob("*.json")):
        found.append(store.read_json(path))
    for path in sorted((store.ROOT / "runs/state").glob("*.json")):
        if store.run_file("results", path.stem).exists():
            continue
        state = store.read_json(path)
        if len(state.get("rounds", [])) == agents and len(state.get("votes", {})) == agents:
            found.append(state)
    return found


def status(data):
    """Статус прогона словом: завершён, остановлен на судье, провален или имя фазы."""
    value = data.get("status") or data.get("phase") or "?"
    return {"done": "завершён", "judge": "остановлен на судье", "failed": "провален"}.get(value, value)


def vote_kind(data):
    """Форма голосования: логпробы (протокол 3) или ранжирование (протокол 2)."""
    first = next(iter(data["votes"].values()), None)
    return "логпробы" if isinstance(first, list) else "ранжирование"


# --- роли -------------------------------------------------------------------

def role_counts(rounds):
    """Сколько раз каждая роль назначалась исполнителям."""
    counts = {}
    for current in rounds:
        for item in current["assignments"].values():
            counts[item["role"]] = counts.get(item["role"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def role_sets(rounds):
    """Наборы ролей по раундам: повторяют ли организаторы схему друг друга."""
    return [frozenset(item["role"] for item in current["assignments"].values()) for current in rounds]


# --- закрепление и конвенция ------------------------------------------------

def repeat_count(rounds, shuffle=None):
    """Повторы роли: агент в следующий раз работает исполнителем и получает ту же роль.

    При shuffle (rng.shuffle) набор ролей каждого раунда раздаётся тем же исполнителям
    случайно — так получается перестановочный нулевой уровень.
    """
    last, count = {}, 0
    for current in rounds:
        workers = list(current["assignments"])
        roles = [current["assignments"][a]["role"] for a in workers]
        if shuffle:
            shuffle(roles)
        for agent, role in zip(workers, roles):
            if agent in last:
                count += int(last[agent] == role)
            last[agent] = role
    return count


def comparable_pairs(rounds):
    """Сколько раз агент выходит исполнителем не в первый раз — на скольких парах возможен повтор."""
    last, count = {}, 0
    for current in rounds:
        for agent in current["assignments"]:
            count += int(agent in last)
            last[agent] = True
    return count


def fixation(rounds):
    """Наблюдаемые повторы роли против случайной раздачи тех же наборов."""
    observed = repeat_count(rounds)
    rng = random.Random(SEED)
    null = sorted(repeat_count(rounds, rng.shuffle) for _ in range(SIMULATIONS))
    return {"observed": observed, "pairs": comparable_pairs(rounds),
            "expected": sum(null) / len(null),
            "low": null[math.floor(0.025 * SIMULATIONS)], "high": null[math.ceil(0.975 * SIMULATIONS) - 1],
            "p": (1 + sum(v >= observed for v in null)) / (SIMULATIONS + 1)}


def set_repeats(rounds, pool=None, rng=None):
    """Скольким сменам центра состав ролей повторил предыдущий.

    При pool и rng наборы раундов заменяются независимыми выборами из pool —
    так получается нулевой уровень конвенции: повторы, объясняющиеся общими
    предпочтениями центров, а не передачей состава между ними.
    """
    sets = role_sets(rounds)
    if rng is not None:
        sets = [rng.choice(pool) for _ in sets]
    return sum(1 for a, b in zip(sets, sets[1:]) if a == b)


def convention(rounds, pool):
    """Повтор состава ролей против независимого выбора из тех же предпочтений.

    Двусторонний p — доля розыгрышей, отклоняющихся от среднего нуля не меньше
    наблюдаемого: состав может повторяться и чаще, и реже случайного.
    """
    observed = set_repeats(rounds)
    rng = random.Random(SEED)
    null = sorted(set_repeats(rounds, pool, rng) for _ in range(SIMULATIONS))
    mean = sum(null) / len(null)
    distance = abs(observed - mean)
    return {"observed": observed, "transitions": len(role_sets(rounds)) - 1,
            "expected": mean,
            "low": null[math.floor(0.025 * SIMULATIONS)], "high": null[math.ceil(0.975 * SIMULATIONS) - 1],
            "p": (1 + sum(abs(v - mean) >= distance for v in null)) / (SIMULATIONS + 1)}


# --- голосование ------------------------------------------------------------

def margin(vote, x, y):
    """Перевес x над y в голосе одного агента: логпробы пары или знак места в ранжировании."""
    if isinstance(vote, list):
        for ballot in vote:
            if set(ballot["pair"]) == {x, y}:
                lp = ballot["logprob"]
                return lp[x] - lp[y]
        raise KeyError((x, y))
    ranking = vote["ranking"]
    return 1.0 if ranking.index(x) < ranking.index(y) else -1.0


def majority(votes, agents):
    """Победы каждого агента по парам: победителя выбирают остальные трое большинством."""
    wins = {a: 0 for a in agents}
    scores, undecided = {}, 0
    for x, y in itertools.combinations(agents, 2):
        voters = [v for v in agents if v not in (x, y)]
        margins = [margin(votes[v], x, y) for v in voters]
        for_x = sum(1 for m in margins if m > 0)
        for_y = sum(1 for m in margins if m < 0)
        scores[(x, y)] = (for_x, for_y)
        if for_x > for_y:
            wins[x] += 1
        elif for_y > for_x:
            wins[y] += 1
        else:
            undecided += 1
    return wins, scores, undecided


def linearity(wins):
    """Доля согласованных троек (h Ландау): 1 — полный порядок, ниже — примеси циклов.

    Согласованная тройка: в ней есть агент, стоящий выше двух других; в циклической
    такого нет. Считается как сумма C(побед, 2) по агентам, делённая на число троек.
    """
    agents = list(wins)
    return sum(math.comb(wins[a], 2) for a in agents) / math.comb(len(agents), 3)


def random_majority(agents, rng):
    """Победы при голосовании, где предпочтения каждого агента — случайный порядок."""
    orders = {a: rng.sample(agents, len(agents)) for a in agents}
    wins = {a: 0 for a in agents}
    for x, y in itertools.combinations(agents, 2):
        for_x = sum(1 for v in agents if v not in (x, y) and orders[v].index(x) < orders[v].index(y))
        wins[x if for_x > 1 else y] += 1
    return wins


def linearity_null(agents):
    """Линейность при случайных предпочтениях: среднее и 95%-интервал."""
    rng = random.Random(SEED)
    values = sorted(linearity(random_majority(agents, rng)) for _ in range(SIMULATIONS))
    return {"mean": sum(values) / len(values),
            "low": values[math.floor(0.025 * SIMULATIONS)], "high": values[math.ceil(0.975 * SIMULATIONS) - 1]}


def transitivity(vote):
    """Доля согласованных троек в предпочтениях одного голосующего (для логпробов).

    Тройка согласована, если в ней есть агент, предпочитаемый обоим остальным;
    в цикле (X>Y, Y>Z, Z>X) такого нет.
    """
    if not isinstance(vote, list):
        return None
    others = sorted({a for ballot in vote for a in ballot["pair"]})
    total = agree = 0
    for x, y, z in itertools.combinations(others, 3):
        s_xy, s_yz, s_xz = (math.copysign(1, margin(vote, a, b)) for a, b in ((x, y), (y, z), (x, z)))
        # победы внутри тройки: у кого-то две — тройка согласована
        wins = ((s_xy > 0) + (s_xz > 0), (s_xy < 0) + (s_yz > 0), (s_yz < 0) + (s_xz < 0))
        total += 1
        agree += int(max(wins) == 2)
    return agree / total if total else None


def margins_abs(votes):
    """Абсолютные перевесы всех парных сравнений (для логпробов)."""
    values = []
    for vote in votes.values():
        if not isinstance(vote, list):
            continue
        for ballot in vote:
            x, y = ballot["pair"]
            values.append(abs(ballot["logprob"][x] - ballot["logprob"][y]))
    return values


def voter_ranking(vote):
    """Ранжирование остальных данным голосующим: по сумме перевесов или как записано."""
    return run.ballot_ranking(vote) if isinstance(vote, list) else vote["ranking"]


def reciprocity(votes):
    """Взаимность делегирования: согласованность мест, которые агенты дают друг другу.

    Для каждой пары агентов берётся точка (место j у i, место i у j); τ — конкордация
    этих точек со связями: +1 — предпочтения возвращаются (высокое место взаимно),
    −1 — односторонние, 0 — независимые.
    """
    rankings = {voter: voter_ranking(vote) for voter, vote in votes.items()}
    points = [(rankings[x].index(y), rankings[y].index(x))
              for x, y in itertools.combinations(rankings, 2)
              if y in rankings[x] and x in rankings[y]]
    if len(points) < 2:
        return None
    concordant = discordant = 0
    for (a, b), (c, d) in itertools.combinations(points, 2):
        concordant += int((a - c) * (b - d) > 0)
        discordant += int((a - c) * (b - d) < 0)
    return (concordant - discordant) / math.comb(len(points), 2)


# --- согласие с точностью и судьёй -------------------------------------------

def organizer_orders(data):
    """Порядок организаторов по точности прогнозов и по оценке судьи за распределение.

    Порядок по судьёй пуст, если все оценки одинаковы: он задавался бы номером агента
    и не нёс бы информации.
    """
    rounds = data["rounds"]
    accuracy = [r["organizer"] for r in sorted((r for r in rounds if r.get("mase") is not None),
                                               key=lambda r: r["mase"])]
    allocation = {j["organizer"]: j["allocation"] for j in data.get("judgments", [])
                  if j["kind"] == "allocation" and isinstance(j.get("allocation"), int)}
    judge = sorted(allocation, key=lambda a: (-allocation[a], a)) if len(set(allocation.values())) > 1 else []
    return accuracy, judge


def agreement(data):
    """Согласие голосов (τ Кендалла) с точностью и оценкой судьи: по голосующим и группе."""
    agents = run.names()
    accuracy, judge = organizer_orders(data)
    rows = {}
    for voter, vote in data["votes"].items():
        ranking = voter_ranking(vote)
        row = {}
        for name, reference in (("accuracy", accuracy), ("judge", judge)):
            reference = [a for a in reference if a != voter]
            if sorted(reference) == sorted(ranking):
                row[name] = run.kendall_tau(ranking, reference)
        rows[voter] = row
    wins, _, _ = majority(data["votes"], agents)
    group = sorted(agents, key=lambda a: (-wins[a], a))
    tau_group = {}
    for name, reference in (("accuracy", accuracy), ("judge", judge)):
        common = [a for a in reference if a in group]
        tau_group[name] = run.kendall_tau(common, [a for a in group if a in common]) if len(common) >= 2 else None
    return rows, group, tau_group


def fmt(value):
    """Число со знаком или прочерк, если значения нет."""
    return "—" if value is None else f"{value:+.2f}"


def tau_range(values):
    """Размах τ по голосующим или прочерк, если ни одного значения."""
    values = [v for v in values if v is not None]
    return "—" if not values else f"от {min(values):+.2f} до {max(values):+.2f}"


# --- отчёт -------------------------------------------------------------------

def condition_section(condition, subset):
    agents, lines = run.names(), [f"## Условие `{condition}`", ""]
    lines += ["| Прогон | Повтор | Статус | Голосование |", "|---|---|---|---|"]
    for data in subset:
        lines.append(f"| {data['spec']['id'][:8]} | {data['spec']['repeat']} | {status(data)} | {vote_kind(data)} |")
    lines.append("")

    # Роли: что центры раздают; пространство выбора задано меню
    counts, catalog = {}, run.roles()
    for data in subset:
        for role, count in role_counts(data["rounds"]).items():
            counts[role] = counts.get(role, 0) + count
    lines += ["### Роли (пространство выбора задано меню)", "", "| Роль | Назначений |", "|---|---|"]
    for role in sorted(catalog, key=lambda r: (-counts.get(r, 0), r)):
        lines.append(f"| {role} | {counts.get(role, 0)} |")
    unused = [role for role in catalog if role not in counts]
    if unused:
        lines += ["", f"Не использована ни разу: {', '.join(unused)}."]
    lines += [""]

    # Конвенция: повторяется ли состав ролей у сменяющихся центров
    pool = [item for data in subset for item in role_sets(data["rounds"])]
    lines += ["### Конвенция состава ролей", "",
              "Повтор — следующему центру достался тот же состав ролей, что предыдущему. Случайный уровень — "
              f"составы прогона разыграны как независимые выборы из эмпирических составов условия ({SIMULATIONS} розыгрышей): "
              "повторы, объясняющиеся общими предпочтениями, не считаются передачей состава.",
              "", "| Прогон | Повторы | Случайно (95%) | p |", "|---|---|---|---|"]
    for data in subset:
        value = convention(data["rounds"], pool)
        p_value = "<0.001" if value["p"] < 0.001 else f"{value['p']:.3f}"
        lines.append(f"| {data['spec']['id'][:8]} | {value['observed']} из {value['transitions']} "
                     f"| {value['expected']:.1f} ({value['low']}–{value['high']}) | {p_value} |")
    lines += [""]

    # Закрепление: держатся ли роли за агентами
    lines += ["### Закрепление ролей (уклад)", "",
              "Повтор — агент в следующий раз работает исполнителем и получает ту же роль, что в прошлый. "
              f"Случайный уровень — те же наборы ролей, случайно розданные тем же исполнителям ({SIMULATIONS} розыгрышей).",
              "", "| Прогон | Повторы | к случайному | Случайно (95%) | p |", "|---|---|---|---|---|"]
    for data in subset:
        value = fixation(data["rounds"])
        p_value = "<0.001" if value["p"] < 0.001 else f"{value['p']:.3f}"
        effect = f"×{value['observed'] / value['expected']:.1f}" if value["expected"] else "—"
        lines.append(f"| {data['spec']['id'][:8]} | {value['observed']} из {value['pairs']} | {effect} "
                     f"| {value['expected']:.1f} ({value['low']}–{value['high']}) | {p_value} |")
    lines.append("")

    # Голосование: какая иерархия выстроилась
    lines += ["### Голосование (делегирование)", "",
              "По каждой паре голосуют остальные трое; победитель — большинство. Линейность h — доля "
              "согласованных троек в итоговом порядке (1 — полный порядок без циклов). Случайный уровень — "
              "те же голосующие со случайными порядками предпочтений. Взаимность τ — согласованность мест, "
              "которые два агента дают друг другу в своих ранжированиях: +1 — предпочтения возвращаются, "
              "−1 — односторонние.",
              "", "| Прогон | Порядок по победам | h | Случайно (95%) | Единодушных пар | Взаимность τ | τ группы с точностью | τ группы с судьёй |",
              "|---|---|---|---|---|---|---|---|"]
    chance = linearity_null(agents)
    for data in subset:
        wins, scores, undecided = majority(data["votes"], agents)
        rows, group, tau_group = agreement(data)
        unanimous = sum(1 for score in scores.values() if max(score) == len(agents) - 2)
        split = f" (+{undecided} без большинства)" if undecided else ""
        lines.append(f"| {data['spec']['id'][:8]} | {' > '.join(group)} | {linearity(wins):.2f} "
                     f"| {chance['mean']:.2f} ({chance['low']:.2f}–{chance['high']:.2f}) "
                     f"| {unanimous} из {len(scores)}{split} "
                     f"| {fmt(reciprocity(data['votes']))} "
                     f"| {fmt(tau_group['accuracy'])} | {fmt(tau_group['judge'])} |")
    lines.append("")
    details = []
    for data in subset:
        rows = agreement(data)[0]
        accuracy = tau_range([row.get("accuracy") for row in rows.values()])
        judge = tau_range([row.get("judge") for row in rows.values()])
        note = (f"Прогон {data['spec']['id'][:8]}: отдельные голосующие согласны с точностью "
                f"τ {accuracy}, с судьёй — τ {judge}.")
        if vote_kind(data) == "логпробы":
            values = sorted(margins_abs(data["votes"]))
            note += f" Медианный перевес {values[len(values) // 2]:.1f} логита."
            transitive = [t for t in (transitivity(v) for v in data["votes"].values()) if t is not None]
            if transitive:
                note += (f" Согласованные тройки у отдельных голосующих: {min(transitive):.2f}–"
                         f"{max(transitive):.2f} (в среднем {sum(transitive) / len(transitive):.2f}).")
        details.append(note)
    return lines + details + [""]


def report():
    """Отчёт по всем прогонам в Markdown."""
    data = runs()
    if not data:
        return "Прогонов с полными раундами и голосами нет."
    lines = ["# Анализ прогонов", "",
             f"Прогонов: {len(data)}. Прочерк — значения нет: у судьи не хватает оценок или все они одинаковы.",
             "",
             "Термины — обзора «Социумы ИИ-агентов» (literature-review.up.railway.app): закрепление "
             "ролей — элемент уклада, повтор состава ролей — конвенция, голосование — делегирование. "
             "Задано средой: меню шести ролей, ротация центра латинским квадратом, условия видимости, "
             "чередование порядка пар. Наблюдается: назначенные роли, прогнозы, голоса по логпробам — "
             "события среды, а не самоотчёты. Интерпретация — за человеком: какие гипотезы числа "
             "поддерживают и каким механизмом, решает он.",
             ""]
    for condition in run.CONDITIONS:
        subset = [d for d in data if d["spec"]["condition"] == condition]
        if subset:
            lines += condition_section(condition, subset)
    lines.append(f"p — перестановочный тест, {SIMULATIONS} розыгрышей, зерно фиксировано: "
                 "повторный запуск даёт тот же отчёт.")
    return "\n".join(lines)
