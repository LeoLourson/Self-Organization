"""Анализ запусков: какие роли раздают организаторы, закрепляются ли роли за агентами,
повторяется ли состав ролей у сменяющихся организаторов, какую иерархию выстраивает
голосование и взаимны ли предпочтения. Запросов к моделям нет.

Читает готовые запуски из runs/results и незавершённые — из runs/state (те, где есть
все раунды и все голоса). Каждая статистика считается против случайного уровня:
закрепление ролей — перестановочный тест (те же наборы ролей, случайно розданные тем
же исполнителям), повтор состава — независимые выборы из эмпирических наборов
условия, линейность иерархии — сравнение с голосованием случайных voters.
Отчёт собирается в Markdown: python run.py analyze [--save].
Числа идут без выводов: какую гипотезу они поддерживают, решает человек.
"""
import functools
import itertools
import math
import random

from . import reasons, run, series, store, vote

SIMULATIONS = 20000
RECIPROCITY_SIMULATIONS = 2000  # нуль считается на каждый голос каждого запуска
SEED = 20261101


# --- данные -----------------------------------------------------------------

def runs(base=None):
    """Запуски с полными раундами: готовые результаты, незавершённые состояния с голосами
    и состояния контроля solo (голосов и судьи в solo нет — достаточно раундов)."""
    agents = store.config()["agents"]
    base = base or store.ROOT / "runs"
    found = []
    for path in sorted((base / "results").glob("*.json")):
        found.append(store.read_json(path))
    for path in sorted((base / "state").glob("*.json")):
        if (base / "results" / path.name).exists():
            continue
        state = store.read_json(path)
        if len(state.get("rounds", [])) == agents and (len(state.get("votes", {})) == agents
                                                       or state["spec"]["condition"] == "solo"):
            found.append(state)
    return found


def status(data):
    """Статус запуска словом: завершён, остановлен на судье, провален или имя фазы."""
    value = data.get("status") or data.get("phase") or "?"
    return {"done": "завершён", "judge": "остановлен на судье", "failed": "провален"}.get(value, value)


def vote_kind(data):
    """Форма голосования: логпробы (протокол 3) или ранжирование (протокол 2)."""
    first = next(iter(data["votes"].values()), None)
    return "логпробы" if isinstance(first, list) else "ранжирование"


# --- серии -------------------------------------------------------------------

def series_key(data):
    """Ключ серии: протокол, движок и модель.

    Статистики считаются только внутри серии: у другой модели своё распределение голосов
    и ошибок, а другой протокол — это другой набор промптов. Смешав их, тест сравнивает
    модели, а не проверяет гипотезу.
    """
    profile = data["spec"].get("profile") or {}
    return (data["spec"].get("protocol") or "—", profile.get("engine") or "—",
            profile.get("model") or "—")


def by_series(data):
    """Запуски, разложенные по сериям в порядке первого появления."""
    groups = []
    for item in data:
        for key, subset in groups:
            if key == series_key(item):
                subset.append(item)
                break
        else:
            groups.append((series_key(item), [item]))
    return groups


def series_title(key):
    """Подпись серии: модель, движок, протокол и судья."""
    protocol, engine, model = key
    return f"{model} · {engine} · протокол {protocol}"


def language(subset):
    """Язык промптов по меню ролей: латиница — английский, кириллица — русский."""
    order = "".join((subset[0]["spec"].get("role_order") or []))
    if not order:
        return None
    return "английский" if order.isascii() else "русский"


def series_judges(subset):
    """Судьи серии: в solo судьи нет, поэтому его имя берётся с групповых запусков."""
    names = sorted({(item["spec"].get("judge") or {}).get("model") for item in subset} - {None})
    return ", ".join(names)


def series_line(key, subset):
    """Строка о серии: судья, язык промптов и сколько запусков каждого условия."""
    judge = series_judges(subset) or "не звали"
    counts = {}
    for item in subset:
        condition = item["spec"]["condition"]
        counts[condition] = counts.get(condition, 0) + 1
    mix = ", ".join(f"`{name}` {counts[name]}" for name in run.CONDITIONS if name in counts)
    tongue = language(subset)
    if tongue:
        prompts = f"промпты на {'английском' if tongue == 'английский' else 'русском'} языке"
    else:
        prompts = "язык промптов неизвестен"
    return f"Судья `{judge}`, {prompts}; запусков {len(subset)}: {mix}."


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
    """Скольким сменам организатора состав ролей повторил предыдущий.

    При pool и rng наборы раундов заменяются независимыми выборами из pool —
    так получается нулевой уровень конвенции: повторы, объясняющиеся общими
    предпочтениями организаторов, а не передачей состава между ними.
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


def unanimous_pairs(votes, agents):
    """Сколько пар все три голосующих решили одинаково."""
    _, scores, _ = majority(votes, agents)
    return sum(1 for score in scores.values() if max(score) == len(agents) - 2)


def random_unanimous(agents, rng):
    """Единодушные пары при голосовании, где предпочтения каждого агента — случайный порядок."""
    orders = {a: rng.sample(agents, len(agents)) for a in agents}
    count = 0
    for x, y in itertools.combinations(agents, 2):
        for_x = sum(1 for v in agents if v not in (x, y) and orders[v].index(x) < orders[v].index(y))
        count += int(for_x in (0, len(agents) - 2))
    return count


@functools.lru_cache(maxsize=None)
def unanimity_values(agents):
    """Распределение единодушных пар одного запуска при случайных голосующих."""
    rng = random.Random(SEED)
    return tuple(random_unanimous(list(agents), rng) for _ in range(SIMULATIONS))


def unanimity_test(vote_sets, agents):
    """Среднее число единодушных пар по запускам условия против случайных голосующих; p односторонний."""
    observed = sum(unanimous_pairs(votes, agents) for votes in vote_sets) / len(vote_sets)
    values, rng = unanimity_values(tuple(agents)), random.Random(SEED)
    draws = [sum(rng.choices(values, k=len(vote_sets))) / len(vote_sets) for _ in range(SIMULATIONS)]
    return {"mean": observed, "null": interval(draws),
            "p": (1 + sum(v >= observed for v in draws)) / (SIMULATIONS + 1)}


def leader_shares(wins):
    """Лидеры запуска с долями: при равенстве побед запуск делится поровну, имя не решает."""
    best = max(wins.values())
    leaders = [a for a in wins if wins[a] == best]
    return {a: 1 / len(leaders) for a in leaders}


def top_share(runs_shares, agents):
    """Самый частый лидер и сумма его долей по запускам."""
    totals = {a: sum(shares.get(a, 0) for shares in runs_shares) for a in agents}
    leader = max(agents, key=lambda a: (totals[a], a == agents[0]))
    return leader, totals[leader]


def leader_test(vote_sets, agents):
    """Повторяется ли лидер между повторами условия.

    Нуль: имена агентов независимо переставляются внутри каждого запуска, поэтому число
    лидеров и равенства побед сохраняются, а связь с именем разрушается. p односторонний.
    """
    observed = [leader_shares(majority(votes, agents)[0]) for votes in vote_sets]
    leader, count = top_share(observed, agents)
    rng, at_least = random.Random(SEED), 0
    for _ in range(SIMULATIONS):
        drawn = []
        for shares in observed:
            names = rng.sample(agents, len(agents))
            drawn.append({names[agents.index(a)]: share for a, share in shares.items()})
        at_least += int(top_share(drawn, agents)[1] >= count - 1e-9)
    return {"leader": leader, "count": count, "runs": len(vote_sets), "p": (1 + at_least) / (SIMULATIONS + 1)}


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
    """Взаимность: согласованность мест, которые агенты дают друг другу.

    Для каждой пары агентов берётся точка (место j у i, место i у j); τ — конкордация
    этих точек со связями: +1 — предпочтения возвращаются (высокое место взаимно),
    −1 — односторонние, 0 — независимые.
    """
    return rankings_tau({voter: voter_ranking(vote) for voter, vote in votes.items()})


def rankings_tau(rankings):
    """τ взаимности по готовым порядкам голосующих; None, если взаимных пар меньше двух."""
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


def interval(values):
    """Среднее и 95%-интервал ряда значений."""
    ordered = sorted(values)
    count = len(ordered)
    return {"mean": sum(ordered) / count, "low": ordered[math.floor(0.025 * count)],
            "high": ordered[math.ceil(0.975 * count) - 1]}


@functools.lru_cache(maxsize=None)
def random_tau_values(agents):
    """τ взаимности при независимых случайных порядках голосующих."""
    rng = random.Random(SEED)
    return tuple(rankings_tau({v: rng.sample([a for a in agents if a != v], len(agents) - 1) for v in agents})
                 for _ in range(RECIPROCITY_SIMULATIONS))


def reciprocity_null(agents):
    return interval(random_tau_values(tuple(agents)))


def shared_tau_values(votes):
    """τ взаимности, если у агентов остаются только общие предпочтения и прежний разброс.

    Общий порядок сам даёт τ > 0: популярные агенты ставят друг друга высоко. Поэтому оценка
    голосующего разлагается на популярность кандидата и остаток; остатки перемешиваются
    между всеми клетками, что сохраняет популярность и разброс, но разрушает связь внутри пар.
    Голосам без перевесов разложить нечего: None.
    """
    if not all(isinstance(vote, list) for vote in votes.values()):
        return None
    scores = {voter: run.ballot_score(ballots) for voter, ballots in votes.items()}
    agents = sorted({a for row in scores.values() for a in row})
    popularity = {a: sum(row[a] for row in scores.values() if a in row) / sum(a in row for row in scores.values())
                  for a in agents}
    cells = [(voter, a) for voter, row in scores.items() for a in row]
    residuals = [scores[voter][a] - popularity[a] for voter, a in cells]
    rng = random.Random(SEED)
    values = []
    for _ in range(RECIPROCITY_SIMULATIONS):
        rng.shuffle(residuals)
        drawn = {voter: {} for voter in scores}
        for (voter, a), residual in zip(cells, residuals):
            drawn[voter][a] = popularity[a] + residual
        values.append(rankings_tau({voter: sorted(row, key=lambda a: (-row[a], a)) for voter, row in drawn.items()}))
    return values


def reciprocity_test(votes):
    """τ взаимности против двух нулей; односторонний p — против общих предпочтений."""
    tau = reciprocity(votes)
    randoms = random_tau_values(tuple(sorted(votes)))
    shared = shared_tau_values(votes)
    return {"tau": tau, "random": interval(randoms), "random_values": randoms,
            "shared": interval(shared) if shared else None, "shared_values": shared,
            "p": (1 + sum(v >= tau for v in shared)) / (len(shared) + 1) if shared else None}


def pooled_reciprocity(tests):
    """Средний τ по запускам условия: каждый розыгрыш берёт значение нуля из каждого запуска независимо."""
    rng = random.Random(SEED)
    mean_draws = lambda rows: [sum(rng.choice(row) for row in rows) / len(rows) for _ in range(RECIPROCITY_SIMULATIONS)]
    tau = sum(test["tau"] for test in tests) / len(tests)
    random_draws = mean_draws([test["random_values"] for test in tests])
    with_shared = all(test["shared_values"] for test in tests)
    shared_draws = mean_draws([test["shared_values"] for test in tests]) if with_shared else None
    return {"tau": tau, "runs": len(tests), "random": interval(random_draws),
            "shared": interval(shared_draws) if shared_draws else None,
            "p": (1 + sum(v >= tau for v in shared_draws)) / (len(shared_draws) + 1) if shared_draws else None}


def fmt_interval(value):
    """Среднее и интервал нуля со знаком или прочерк."""
    return "—" if value is None else f"{value['mean']:+.2f} ({value['low']:+.2f}…{value['high']:+.2f})"


def fmt_p(value):
    return "—" if value is None else ("<0.001" if value < 0.001 else f"{value:.3f}")


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


def agreement(data, votes=None):
    """Согласие голосов (τ Кендалла) с точностью и оценкой судьи: по голосующим и группе."""
    votes = data["votes"] if votes is None else votes
    agents = run.names()
    accuracy, judge = organizer_orders(data)
    rows = {}
    for voter, ballots in votes.items():
        ranking = voter_ranking(ballots)
        row = {}
        for name, reference in (("accuracy", accuracy), ("judge", judge)):
            reference = [a for a in reference if a != voter]
            if sorted(reference) == sorted(ranking):
                row[name] = run.kendall_tau(ranking, reference)
        rows[voter] = row
    wins, _, _ = majority(votes, agents)
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


# --- следуют ли голоса за точностью -------------------------------------------

def raw_errors(data):
    """Ошибка каждого организатора в запуске: то, что агенты могли сравнить по сводкам."""
    return {r["organizer"]: r["mase"] for r in data["rounds"] if r.get("mase") is not None}


def window_errors(data, subset):
    """Ошибка организатора минус средняя ошибка его окна по запускам условия: поправка на трудность периода."""
    means = {}
    for index in range(len(data["rounds"])):
        values = [d["rounds"][index]["mase"] for d in subset
                  if len(d["rounds"]) > index and d["rounds"][index].get("mase") is not None]
        means[index] = sum(values) / len(values) if values else None
    return {r["organizer"]: r["mase"] - means[i] for i, r in enumerate(data["rounds"]) if r.get("mase") is not None}


def accuracy_stats(rows):
    """Первые места у самых точных и средний τ между порядком голосующего и порядком ошибок."""
    hits = voters = winners = 0
    taus = []
    for row in rows:
        errors = row["errors"]
        best = min(errors, key=lambda a: (errors[a], a))
        winners += int(row["group"][0] == best)
        for ranking in row["rankings"].values():
            reference = sorted(ranking, key=lambda a: (errors[a], a))
            hits += int(ranking[0] == reference[0])
            voters += 1
            taus.append(run.kendall_tau(ranking, reference))
    return {"hits": hits, "voters": voters, "winners": winners, "runs": len(rows), "tau": sum(taus) / len(taus)}


def accuracy_test(rows):
    """Следование голоса за ошибками против перемешивания ошибок между агентами внутри запуска.

    Перемешивание идёт по запускам, поскольку голоса внутри запуска зависимы.
    Односторонний p: голоса следуют за точностью не реже, чем при случайной привязке ошибок.
    """
    observed = accuracy_stats(rows)
    rng = random.Random(SEED)
    at_least_hits = at_least_tau = 0
    for _ in range(RECIPROCITY_SIMULATIONS):
        drawn = []
        for row in rows:
            values = list(row["errors"].values())
            rng.shuffle(values)
            drawn.append({**row, "errors": dict(zip(row["errors"], values))})
        stats = accuracy_stats(drawn)
        at_least_hits += int(stats["hits"] >= observed["hits"])
        at_least_tau += int(stats["tau"] >= observed["tau"])
    return {**observed, "p_hits": (1 + at_least_hits) / (RECIPROCITY_SIMULATIONS + 1),
            "p_tau": (1 + at_least_tau) / (RECIPROCITY_SIMULATIONS + 1)}


def accuracy_lines(subset, label, errors_of, title):
    """Строка отчёта об одном голосе и одном виде ошибки."""
    rows = []
    for data in subset:
        votes = dict(vote_forms(data)).get(label)
        errors = errors_of(data)
        if votes is None or len(errors) != len(run.names()):
            continue
        rows.append({"rankings": {v: voter_ranking(ballots) for v, ballots in votes.items()},
                     "group": agreement(data, votes)[1], "errors": errors})
    if not rows:
        return []
    value = accuracy_test(rows)
    return [f"- Голос {label}, {title}: самого точного поставили первым {value['hits']} из {value['voters']} голосующих "
            f"(случайно 25%, p = {fmt_p(value['p_hits'])}); победитель голосования — самый точный в {value['winners']} "
            f"из {value['runs']} запусков (случайно 20%); средняя τ с точностью {value['tau']:+.2f} "
            f"(p = {fmt_p(value['p_tau'])})."]


def judge_errors(data):
    """Оценки судьи за распределение ролей со знаком минус: чем выше оценка, тем раньше в порядке.

    Без всех пяти оценок или при равных оценках порядка нет: None.
    """
    grades = {j["organizer"]: j["allocation"] for j in data.get("judgments", [])
              if j["kind"] == "allocation" and isinstance(j.get("allocation"), int)}
    if len(grades) != len(run.names()) or len(set(grades.values())) == 1:
        return None
    return {agent: -grade for agent, grade in grades.items()}


def judge_lines(subset, label):
    """Строка отчёта: следует ли голос за оценками судьи; уровень сравнения — перемешанные оценки."""
    rows = []
    for data in subset:
        votes, errors = dict(vote_forms(data)).get(label), judge_errors(data)
        if votes is not None and errors is not None:
            rows.append({"rankings": {v: voter_ranking(ballots) for v, ballots in votes.items()},
                         "group": agreement(data, votes)[1], "errors": errors})
    if not rows:
        return []
    value = accuracy_test(rows)
    return [f"- Голос {label}, оценка судьи: средняя τ с судьёй {value['tau']:+.2f} по {value['runs']} запуск. "
            f"(p = {fmt_p(value['p_tau'])})."]


def accuracy_section(subset):
    """Блок условия: голоса против сырой ошибки, ошибки с поправкой на окно и оценок судьи."""
    lines = []
    for label in ("цифра", "JSON"):
        lines += accuracy_lines(subset, label, raw_errors, "сырая ошибка")
        lines += accuracy_lines(subset, label, lambda data: window_errors(data, subset), "с поправкой на окно")
        lines += judge_lines(subset, label)
    return ["### Следуют ли голоса за точностью и судьёй", "", *lines, ""] if lines else []


# --- коллективный выигрыш ----------------------------------------------------

def sign_test(wins, total):
    """Двусторонний точный знаковый тест при p=0.5; нули и ничьи не считаются."""
    if not total:
        return None
    tail = sum(math.comb(total, i) for i in range(min(wins, total - wins) + 1)) / 2 ** total
    return min(1.0, 2 * tail)


def ensemble_mase(data, condition, round_index):
    """MASE медианного прогноза всех запусков условия на окно раунда round_index.

    Медиана — механическая агрегация без обсуждения: независимый ансамбль.
    """
    forecasts, actual = [], None
    for d in data:
        if d["spec"]["condition"] != condition or len(d["rounds"]) <= round_index:
            continue
        current = d["rounds"][round_index]
        if current.get("forecast"):
            forecasts.append(current["forecast"])
        if actual is None and current.get("actual"):
            actual = current["actual"]
    if not forecasts or actual is None:
        return None, 0
    weeks = list(zip(*forecasts))
    median = [sorted(week)[len(week) // 2] for week in weeks]
    begin, origin, end = series.window(round_index)
    return series.mase(median, actual, series.load()[begin:origin]), len(forecasts)


def delta_q_section(data):
    """Коллективный выигрыш: организованные условия против одиночек на парных окнах."""
    # Пара «группа — одиночка» допустима только внутри одной серии: иначе ΔQ сравнивает разные модели.
    solo = {((d["spec"].get("profile") or {}).get("model"), d["spec"]["repeat"]): d
            for d in data if d["spec"]["condition"] == "solo"}
    lines = ["## Коллективный выигрыш (ΔQ)", ""]
    pairs = []
    for condition in ("open", "closed"):
        for d in data:
            key = ((d["spec"].get("profile") or {}).get("model"), d["spec"]["repeat"])
            if d["spec"]["condition"] != condition or key not in solo:
                continue
            for group_round, solo_round in zip(d["rounds"], solo[key]["rounds"]):
                if group_round.get("mase") is None or solo_round.get("mase") is None:
                    continue
                pairs.append((condition, d["spec"]["repeat"], group_round["organizer"],
                              group_round["mase"], solo_round["mase"]))
    if not pairs:
        return lines + [f"solo-запусков с полными раундами в серии: {len(solo)}; пар с групповыми условиями "
                        "нет — запустите те же повторы командой `python run.py plan --profile <профиль> "
                        "--conditions solo --repeats 5`.", ""]
    lines += ["| Условие | Повтор | Раунд ведёт | MASE группа | MASE одиночка | ΔQ |", "|---|---|---|---|---|---|"]
    for condition, repeat, agent, group_mase, solo_mase in pairs:
        lines.append(f"| `{condition}` | {repeat} | {agent} | {group_mase:.2f} | {solo_mase:.2f} "
                     f"| {solo_mase - group_mase:+.2f} |")
    deltas = [solo_mase - group_mase for _, _, _, group_mase, solo_mase in pairs]
    wins = sum(1 for v in deltas if v > 0)
    losses = sum(1 for v in deltas if v < 0)
    p_value = sign_test(wins, wins + losses)
    p_text = "—" if p_value is None else ("<0.001" if p_value < 0.001 else f"{p_value:.3f}")
    lines += ["", f"Медианный ΔQ {sorted(deltas)[len(deltas) // 2]:+.2f}; группа лучше в {wins} из "
             f"{len(deltas)} пар ({losses} хуже, {len(deltas) - wins - losses} равных); знаковый тест p = {p_text}.",
             ""]
    lines += ["### Независимый ансамбль (медиана прогнозов на окне)", "",
              "| Окно | `open` | `closed` | `solo` |", "|---|---|---|---|"]
    for index in range(store.config()["agents"]):
        cells = []
        for condition in ("open", "closed", "solo"):
            mase, count = ensemble_mase(data, condition, index)
            cells.append("—" if mase is None else f"{count}, {mase:.2f}")
        lines.append(f"| {index + 1} | " + " | ".join(cells) + " |")
    return lines + [""]


def solo_section(subset):
    """Секция контроля solo: одиночные прогнозы без общения."""
    lines = ["## Контроль `solo`", "", "| Запуск | Повтор | Статус | MASE по раундам |", "|---|---|---|---|"]
    for data in subset:
        rounds = "; ".join(f"{r['organizer']} {r['mase']:.2f}" if r.get("mase") is not None
                          else f"{r['organizer']} —" for r in data["rounds"])
        lines.append(f"| {data['spec']['id'][:8]} | {data['spec']['repeat']} | {status(data)} | {rounds} |")
    return lines + [""]


# --- отчёт -------------------------------------------------------------------

def legend():
    """Как читать таблицы: правила одинаковы для всех серий, поэтому стоят в начале отчёта."""
    return [
        "**Роли.** Пространство выбора задано меню шести ролей; одна роль может достаться "
        "нескольким агентам, часть ролей может остаться неиспользованной.",
        f"**Конвенция.** Повтор — следующему организатору достался тот же состав ролей, что предыдущему. "
        f"Случайный уровень — составы запуска разыграны как независимые выборы из эмпирических "
        f"составов серии ({SIMULATIONS} розыгрышей): повторы, объясняющиеся общими предпочтениями, "
        "не считаются передачей состава.",
        f"**Закрепление.** Повтор — агент в следующий раз работает исполнителем и получает ту "
        f"же роль, что в прошлый. Случайный уровень — те же наборы ролей, случайно розданные тем же "
        f"исполнителям ({SIMULATIONS} розыгрышей).",
        "**Голосование.** По каждой паре голосуют остальные трое, победитель — "
        "большинство. Линейность h — доля согласованных троек в итоговом порядке (1 — полный порядок "
        "без циклов); случайный уровень — те же голосующие со случайными порядками предпочтений. "
        "τ Кендалла измеряет сходство двух порядков: при +1 они совпадают, при −1 обратны, а около нуля не связаны. "
        "В отчёте эта мера применяется к разным порядкам, поэтому её значения между собой не сопоставляются. "
        "τ взаимности сравнивает места, которые два агента дают друг другу: при +1 предпочтения возвращаются, "
        "при −1 они односторонние. Её интервалы показывают два уровня: случайные порядки и общие предпочтения, "
        f"где популярность кандидатов и разброс сохранены, а связь внутри пар разрушена ({RECIPROCITY_SIMULATIONS} "
        "розыгрышей). Общие предпочтения сами поднимают τ взаимности выше нуля, поэтому p односторонний и считается "
        "против второго уровня. «τ группы с точностью» и «τ группы с судьёй» сравнивают итоговый порядок голосования "
        "с порядком организаторов по MASE и по оценкам судьи.",
        "**Согласие о руководителе.** Единодушная пара — все три голосующих решили её одинаково; уровень сравнения — "
        "случайные голосующие, p односторонний. Лидер — агент с наибольшим числом побед, при равенстве запуск делится "
        "поровну; уровень сравнения — имена агентов, независимо переставленные внутри каждого запуска.",
        "**Два голоса.** Каждая пара решается цифрой и JSON с причиной. Строка «цифра» считается по "
        "перевесу логпробов, строка «JSON» — по названной стороне. Причина пишется после выбора и мотивом "
        "не считается. Слово роли, основа и ссылка на поведение — механические признаки английского текста.",
        "**Следование за точностью.** Лучшего по способностям среди одинаковых агентов нет, поэтому проверяем, "
        "следует ли голос за видимой точностью. Сырая ошибка — то, что агент мог сравнить; поправка на окно вычитает "
        "среднюю ошибку того же окна по запускам условия. Уровень сравнения — ошибки, перемешанные между агентами "
        "внутри запуска; p односторонний. τ с точностью сравнивает порядок голосующего с порядком остальных "
        "агентов по ошибке и усредняется по голосующим.",
        "**Контроль `solo`.** Те же агенты, окна и ротация, но без ролей, исполнителей и сообщений: "
        "каждый делает свой прогноз один и видит только свои итоги. MASE — ошибка относительно "
        "наивного «как на прошлой неделе»; меньше 1 — лучше наивного.",
        "**ΔQ.** MASE одиночки − MASE группы на одном окне: положительное значение — организованная "
        "группа ошибается меньше. Пары стыкуются повторами: в повторе k раунд r и в группе, и в solo "
        "ведёт один и тот же агент на том же окне (латинский квадрат). Ресурсы при этом не равны: "
        "группа тратит ещё и на общение, поэтому рядом независимый ансамбль — медиана "
        "прогнозов одиночек на окне против медиан групповых.",
    ]


def vote_forms(data):
    """Голоса запуска по форматам: цифрой и, если есть, с причиной."""
    forms = [("цифра", data["votes"])]
    if data.get("reasons"):
        forms.append(("JSON", vote.sign_votes(data["reasons"])))
    return forms


def condition_section(condition, subset):
    agents, lines = run.names(), [f"## Условие `{condition}`", ""]
    lines += ["| Запуск | Повтор | Статус | Голосование |", "|---|---|---|---|"]
    for data in subset:
        lines.append(f"| {data['spec']['id'][:8]} | {data['spec']['repeat']} | {status(data)} | {vote_kind(data)} |")
    lines.append("")

    # Роли: что организаторы раздают; пространство выбора задано меню
    counts, catalog = {}, run.roles()
    for data in subset:
        for role, count in role_counts(data["rounds"]).items():
            counts[role] = counts.get(role, 0) + count
    lines += ["### Роли", "", "| Роль | Назначений |", "|---|---|"]
    for role in sorted(catalog, key=lambda r: (-counts.get(r, 0), r)):
        lines.append(f"| {role} | {counts.get(role, 0)} |")
    unused = [role for role in catalog if role not in counts]
    if unused:
        lines += ["", f"Не использована ни разу: {', '.join(unused)}."]
    lines += [""]

    # Конвенция: повторяется ли состав ролей у сменяющихся организаторов
    pool = [item for data in subset for item in role_sets(data["rounds"])]
    lines += ["### Конвенция состава ролей", "", "| Запуск | Повторы | Случайно (95%) | p |", "|---|---|---|---|"]
    for data in subset:
        value = convention(data["rounds"], pool)
        p_value = "<0.001" if value["p"] < 0.001 else f"{value['p']:.3f}"
        lines.append(f"| {data['spec']['id'][:8]} | {value['observed']} из {value['transitions']} "
                     f"| {value['expected']:.1f} ({value['low']}–{value['high']}) | {p_value} |")
    lines += [""]

    # Закрепление: держатся ли роли за агентами
    lines += ["### Закрепление ролей", "",
              "| Запуск | Повторы | к случайному | Случайно (95%) | p |", "|---|---|---|---|---|"]
    for data in subset:
        value = fixation(data["rounds"])
        p_value = "<0.001" if value["p"] < 0.001 else f"{value['p']:.3f}"
        effect = f"×{value['observed'] / value['expected']:.1f}" if value["expected"] else "—"
        lines.append(f"| {data['spec']['id'][:8]} | {value['observed']} из {value['pairs']} | {effect} "
                     f"| {value['expected']:.1f} ({value['low']}–{value['high']}) | {p_value} |")
    lines.append("")

    # Голосование: какая иерархия выстроилась
    lines += ["### Голосование", "",
              "| Запуск | Голос | Порядок по победам | h | Случайно (95%) | Единодушных пар | τ взаимности | Она же при случайных порядках (95%) | Она же при общих предпочтениях (95%) | p | τ группы с точностью | τ группы с судьёй |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    chance = linearity_null(agents)
    tests, vote_sets = {}, {}
    for data in subset:
        for label, votes in vote_forms(data):
            vote_sets.setdefault(label, []).append(votes)
            wins, scores, undecided = majority(votes, agents)
            _, group, tau_group = agreement(data, votes)
            unanimous = sum(1 for score in scores.values() if max(score) == len(agents) - 2)
            split = f" (+{undecided} без большинства)" if undecided else ""
            test = reciprocity_test(votes)
            tests.setdefault(label, []).append(test)
            lines.append(f"| {data['spec']['id'][:8]} | {label} | {' > '.join(group)} | {linearity(wins):.2f} "
                         f"| {chance['mean']:.2f} ({chance['low']:.2f}–{chance['high']:.2f}) "
                         f"| {unanimous} из {len(scores)}{split} "
                         f"| {fmt(test['tau'])} | {fmt_interval(test['random'])} | {fmt_interval(test['shared'])} "
                         f"| {fmt_p(test['p'])} "
                         f"| {fmt(tau_group['accuracy'])} | {fmt(tau_group['judge'])} |")
    lines.append("")
    for label, group in tests.items():
        pooled = pooled_reciprocity(group)
        lines += [f"Средняя τ взаимности по {pooled['runs']} запуск., голос {label}, равна {pooled['tau']:+.2f}; "
                  f"случайные порядки дают {fmt_interval(pooled['random'])}, общие предпочтения — "
                  f"{fmt_interval(pooled['shared'])}, p = {fmt_p(pooled['p'])}.", ""]
    for label, sets in vote_sets.items():
        unanimity, leader = unanimity_test(sets, agents), leader_test(sets, agents)
        null = unanimity["null"]
        lines += [f"Голос {label}. Единодушных пар в среднем {unanimity['mean']:.1f} из {math.comb(len(agents), 2)}; "
                  f"случайные голосующие дают {null['mean']:.1f} ({null['low']:.1f}…{null['high']:.1f}), "
                  f"p = {fmt_p(unanimity['p'])}. Самый частый лидер {leader['leader']}: {leader['count']:.1f} из "
                  f"{leader['runs']} запусков, p = {fmt_p(leader['p'])}.", ""]
    details = []
    for data in subset:
        for label, votes in vote_forms(data):
            rows = agreement(data, votes)[0]
            accuracy = tau_range([row.get("accuracy") for row in rows.values()])
            judge = tau_range([row.get("judge") for row in rows.values()])
            note = (f"Запуск {data['spec']['id'][:8]}, голос {label}: у отдельных голосующих τ с точностью лежит "
                    f"в пределах {accuracy}, τ с судьёй — в пределах {judge}.")
            if vote_kind(data) == "логпробы":
                if label == "цифра":
                    values = sorted(margins_abs(votes))
                    note += f" Медианный перевес {values[len(values) // 2]:.1f} логита."
                transitive = [t for t in (transitivity(v) for v in votes.values()) if t is not None]
                if transitive:
                    note += (f" Согласованные тройки у отдельных голосующих: {min(transitive):.2f}–"
                             f"{max(transitive):.2f} (в среднем {sum(transitive) / len(transitive):.2f}).")
            details.append(note)
    return lines + details + [""] + accuracy_section(subset)


def series_section(key, subset):
    """Секции одной серии: условия по отдельности, контроль solo и ΔQ."""
    lines = [f"# Серия — {series_title(key)}", "", series_line(key, subset), ""]
    for condition in run.CONDITIONS:
        if condition == "solo":
            continue  # у контроля solo своя секция
        block = [d for d in subset if d["spec"]["condition"] == condition]
        if block:
            lines += condition_section(condition, block)
    solo = [d for d in subset if d["spec"]["condition"] == "solo"]
    if solo:
        lines += solo_section(solo)
    lines += delta_q_section(subset)
    lines += reasons.section(subset)
    return lines


def series_table(data):
    """Список серий с составом: числа разных серий не смешиваются."""
    lines = ["| Серия | Движок | Судья | Промпты | Протокол | Запусков |",
             "|---|---|---|---|---|---|"]
    for key, subset in by_series(data):
        protocol, engine, model = key
        lines.append(f"| {model} | {engine} | {series_judges(subset) or 'не звали'} | "
                     f"{language(subset) or '—'} | {protocol} | {len(subset)} |")
    return lines + [""]


def report(base=None):
    """Отчёт по всем запускам в Markdown, отдельными блоками по сериям."""
    data = runs(base)
    if not data:
        return "Запусков с полными раундами и голосами нет."
    groups = by_series(data)
    lines = ["# Анализ запусков", "",
             f"Запусков: {len(data)} в сериях: {len(groups)}. Прочерк — значения нет: у судьи не хватает "
             "оценок или все они одинаковы.", "",
             "Серия — это протокол, движок и модель: другой протокол означает другой набор промптов, "
             "другая модель — другое распределение голосов и ошибок. Поэтому все числа считаются внутри "
             "серии, а серии только перечислены рядом; сравнивать их между собой напрямую нельзя.", ""]
    lines += series_table(data)
    lines += ["## Как читать таблицы", ""]
    for item in legend():
        lines.append(f"- {item}")
    lines.append("")
    lines += ["Задано средой: меню шести ролей, ротация организатора латинским квадратом, условия видимости, "
              "чередование порядка пар. Наблюдается: назначенные роли, прогнозы, голоса по логпробам. "
              "Какие гипотезы числа поддерживают и каким механизмом, решает человек.", ""]
    for key, subset in groups:
        lines += series_section(key, subset)
    lines.append(f"p — перестановочный тест (закрепление, состав, линейность) или точный знаковый тест "
                 f"(ΔQ); {SIMULATIONS} розыгрышей, зерно фиксировано: повторный запуск даёт тот же отчёт.")
    return "\n".join(lines)
