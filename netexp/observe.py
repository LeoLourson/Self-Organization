"""Наблюдаемые свойства социума по рабочему словарю обзора: роли, связи, конвенции.

Все функции чистые: принимают раунды прогона и возвращают числа, без обращений
к модели. Назначенная роль — то, что организатор выдал исполнителю в раунде;
фактическая специализация — то, на чём агент оказался по итогам всего прогона.
Эти величины кладутся в итог прогона (run.finish) и дальше сравниваются между
условиями open и closed.
"""


def role_matrix(rounds):
    """Матрица «агент → роль → сколько раз»: назначенные роли по всем раундам."""
    matrix = {}
    for current in rounds:
        for agent, item in current["assignments"].items():
            matrix.setdefault(agent, {})
            role = item["role"]
            matrix[agent][role] = matrix[agent].get(role, 0) + 1
    return matrix


def specialization(rounds):
    """Доля назначений агента, пришедшихся на его самую частую роль.

    1 — все назначения в одной роли (выраженная фактическая специализация),
    0.25 — равномерно по четырём ролям у исполнителя пяти раундов.
    """
    result = {}
    for agent, counts in role_matrix(rounds).items():
        total = sum(counts.values())
        result[agent] = max(counts.values()) / total if total else 0.0
    return result


def links(rounds):
    """Сеть заданий: организатор → исполнитель → сколько раз передавал задание."""
    result = {}
    for current in rounds:
        boss = result.setdefault(current["organizer"], {})
        for agent in current["assignments"]:
            boss[agent] = boss.get(agent, 0) + 1
    return result


def conventions(rounds):
    """Доля назначений «исполнитель → роль», уже встречавшихся в прошлых раундах.

    По одному числу на раунд, первый раунд всегда 0. Повтор в `open` можно
    объяснить тем, что организатор видел чужие распределения; в `closed` — нет,
    поэтому повтор там говорит о притяжении, а не о передаче.
    """
    seen, shares = set(), []
    for current in rounds:
        pairs = {(agent, item["role"]) for agent, item in current["assignments"].items()}
        shares.append(round(len(pairs & seen) / len(pairs), 4) if pairs else 0.0)
        seen |= pairs
    return shares
