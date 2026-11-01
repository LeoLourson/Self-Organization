"""Повторное голосование с причиной: те же пары, структурированный ответ, отдельный файл.

Голосование прогона (run.collect_votes) регистрирует только логпробы цифр «1» и
«2»: видно, кого назначают организатором и с каким перевесом, но не почему. Здесь
вопросы задаются заново — от той же истории агента после того же debrief — и
требуют JSON с полем reason. Прежние голоса не трогаются: проход пишется в
runs/revote/<id>.json и сравнивается с ними отдельно, поэтому ни хеш протокола,
ни файлы серии не меняются.

Отображение цифр остаётся прежним (1 — первый в вопросе, 2 — второй), и сторона
пары «1» по-прежнему выбирается хешем прогона и агента: сравнение с прежним
голосованием не сдвигается местом.
"""
from . import analysis, llm, run as runner, store

ATTEMPTS = 3          # повторов на обычный вопрос
RETRY_ATTEMPTS = 2    # повторов на уточнённый вопрос: модель ответила не 1 и не 2


def question(first, second, clarified=False):
    """Тот же вопрос пары, но в ответ требуются выбор и причина одним JSON.

    clarified добавляет прямое указание: пара состоит ровно из двух агентов. Без него
    модель иногда отвечает «выбор 3» — как будто в паре есть третий вариант.
    """
    parts = runner.text("pair").split("\n\n")
    text = (f"{parts[0]}\n\n"
            f"{parts[1].format(first=first, second=second)}\n\n"
            'Answer with a JSON object: {"choice": 1 or 2, "reason": "<one sentence>"} '
            f"— 1 if {first}, 2 if {second}. Give the reason in at most 30 words.")
    if clarified:
        text += (f"\n\nThe pair holds exactly two agents: {first} and {second}. "
                 f"Answer 1 to make {first} the organizer, 2 to make {second} the organizer. "
                 "No other number is an answer.")
    return text


def parse(value):
    """Выбор и причина из структурированного ответа; ValueError — если поля нет или выбор не 1/2."""
    data = runner.parse_json(value["content"])
    choice, reason = data.get("choice"), str(data.get("reason", "")).strip()
    if isinstance(choice, str) and choice.strip() in ("1", "2"):
        choice = int(choice.strip())
    if type(choice) is not int or choice not in (1, 2):
        raise ValueError(f"нужен choice 1 или 2; ответ: {value['content'][:200]}")
    if not reason:
        raise ValueError(f"нужна причина выбора; ответ: {value['content'][:200]}")
    return {"choice": choice, "reason": reason}


def collect(ident, limit=None, fresh=False):
    """Заново голосует по всем парам прогона; собранные голоса подхватываются при обрыве.

    limit ограничивает число пар на прогон — для пробы. fresh забывает собранное
    и начинает проход заново.
    """
    state = store.read_json(store.run_file("state", ident))
    if state["spec"]["condition"] == "solo":
        raise ValueError("solo не голосует: делегировать некого")
    profile = state["spec"]["vote"]
    agents = runner.names()
    path = store.run_file("revote", ident)
    ballots = [] if fresh or not path.exists() else store.read_json(path)["ballots"]
    done = {(ballot["agent"], *sorted(ballot["pair"])) for ballot in ballots}
    system = runner.system_prompt(state["spec"]["condition"])
    for agent in agents:
        for x, y in runner.pairs([a for a in agents if a != agent]):
            if limit is not None and len(ballots) >= limit:
                break
            if (agent, *sorted((x, y))) in done:
                continue
            one, two = (y, x) if int(store.digest([ident, agent, x, y]), 16) % 2 else (x, y)
            value = None
            for attempt in range(ATTEMPTS + RETRY_ATTEMPTS):
                scope = store.digest([ident, "revote", agent, x, y, attempt])
                messages = [{"role": "system", "content": system}, *state["histories"][agent],
                            {"role": "user", "content": question(one, two, clarified=attempt >= ATTEMPTS)}]
                try:
                    value = llm.ballot(profile, scope, messages)
                    result = parse(value)
                    break
                except ValueError:
                    value = None
            if value is None:
                raise RuntimeError(f"{agent} не дал выбора с причиной в паре {one}–{two}")
            ballots.append({"agent": agent, "pair": [x, y], "one": one, "two": two,
                            "choice": result["choice"], "picked": one if result["choice"] == 1 else two,
                            "reason": result["reason"], "logprob": value["logprob"]})
            store.write_json(path, {"id": ident, "profile": profile["id"], "ballots": ballots})
    return {"id": ident, "ballots": len(ballots),
            "with_logprob": sum(1 for ballot in ballots if ballot["logprob"]["1"] is not None
                                and ballot["logprob"]["2"] is not None)}


# --- сводка ------------------------------------------------------------------

def ranking(ballots):
    """Порядок остальных по числу выборов в парах; при равенстве — по сумме перевесов, затем по имени.

    Выбор и перевес расходятся при сэмплировании: выбранный токен может быть не самым
    вероятным, и тогда порядок по перевесам покажет другого. Порядок по выборам —
    это то, кого агент называл организатором, поэтому причина берётся оттуда.
    """
    wins = {agent: 0 for ballot in ballots for agent in ballot["pair"]}
    for ballot in ballots:
        wins[ballot["picked"]] += 1
    margins = dict.fromkeys(wins, 0.0)
    for ballot in ballots:
        if ballot["logprob"]["1"] is not None and ballot["logprob"]["2"] is not None:
            margins[ballot["one"]] += ballot["logprob"]["1"] - ballot["logprob"]["2"]
            margins[ballot["two"]] += ballot["logprob"]["2"] - ballot["logprob"]["1"]
    return sorted(wins, key=lambda agent: (-wins[agent], -margins[agent], agent))


def pick_sign(ballot, x, y):
    """Кого выбрал голосующий в паре x–y: 1 — x, −1 — y. None, если пара не про него."""
    if set(ballot["pair"]) != {x, y}:
        return None
    return 1 if ballot["picked"] == x else -1


def logprob_votes(votes):
    """Голоса протокола с логпробами: у пары есть перевесы обоих вариантов, а не только ранжирование."""
    first = next(iter(votes.values()), None)
    return bool(first) and isinstance(first[0], dict) and isinstance(first[0].get("logprob"), dict)


def flipped(old, new):
    """Сколько пар поменяли предпочтение при повторном голосовании.

    Прежний знак перевеса берётся из логпробов пары, новый — из выбора: сэмплирование
    при температуре может назвать не самый вероятный вариант, и тогда сравниваются
    именно предпочтения, а не совпадение токенов.
    """
    changed = total = 0
    for voter, ballots in new.items():
        old_ballots = old.get(voter) or []
        for ballot in ballots:
            x, y = ballot["pair"]
            for old_ballot in old_ballots:
                if set(old_ballot["pair"]) == {x, y}:
                    margin = old_ballot["logprob"][x] - old_ballot["logprob"][y]
                    changed += int(margin != 0 and (margin > 0) != (pick_sign(ballot, x, y) > 0))
                    total += 1
    return changed, total


def top_reason(ballots, agent):
    """Причина, которой голосующий обосновывал выбор agent: из пары с самым уверенным перевесом."""
    picked = [ballot for ballot in ballots if ballot["picked"] == agent]
    if not picked:
        return "—"
    return max(picked, key=lambda ballot: abs((ballot["logprob"]["1"] or 0) - (ballot["logprob"]["2"] or 0)))["reason"]


def count_word(n, one, few, many):
    """Согласование числительного с существительным: 1 голос, 2 голоса, 5 голосов."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def run_block(state, ballots):
    """Строки одного прогона: порядок у каждого голосующего и причина его первого выбора."""
    ident = state["spec"]["id"]
    old = state["votes"]
    lines = [f"### Прогон {ident[:8]} · {state['spec']['condition']} · повтор {state['spec']['repeat']}", ""]
    if len(ballots) < len(runner.names()) * 6:
        lines += [f"Собрано {count_word(len(ballots), 'голос', 'голоса', 'голосов')} из 30 — "
                  "проба, выводы по прогону преждевременны.", ""]
    lines += ["| Голосующий | Порядок по выборам | τ с прежним голосом | Перевёрнуто пар | Причина первого выбора |",
              "|---|---|---|---|---|"]
    comparable = logprob_votes(old)
    new = {}
    for agent in runner.names():
        mine = [ballot for ballot in ballots if ballot["agent"] == agent]
        if not mine:
            continue
        new[agent] = mine
        order = ranking(mine)
        old_ranking = runner.ballot_ranking(old[agent]) if comparable and old.get(agent) else None
        tau = f"{runner.kendall_tau(order, old_ranking):.2f}" if old_ranking else "—"
        if comparable:
            changed, total = flipped(old, {agent: mine})
            moved = f"{changed} из {total}" if total else "—"
        else:
            moved = "—"
        lines.append(f"| {agent} | {' > '.join(order)} | {tau} | {moved} "
                     f"| «{top_reason(mine, order[0])}» |")
    lines.append("")
    if comparable:
        changed, total = flipped(old, new)
        lines += [f"Всего перевёрнуто {changed} из {total} пар, где прежний перевес был не нулевым.", ""]
    else:
        lines += ["Прежние голоса этой серии — ранжирование без перевесов: сравнивать порядок не с чем.", ""]
    return lines


def why_block(states, subsets):
    """Почему того или иного агента называют первым: счёт выборов и цитаты причин по серии."""
    counts, quotes = {}, {}
    for state in states:
        for agent in runner.names():
            mine = [ballot for ballot in subsets[state["spec"]["id"]] if ballot["agent"] == agent]
            if not mine:
                continue
            order = ranking(mine)
            picked = order[0]
            counts[picked] = counts.get(picked, 0) + 1
            quotes.setdefault(picked, []).append(top_reason(mine, picked))
    if not counts:
        return []
    lines = ["### Почему выбирают", "", "| Назван первым организатором | Раз | Причины |", "|---|---|---|"]
    for agent in sorted(counts, key=lambda a: -counts[a]):
        reasons = sorted({reason for reason in quotes[agent]})[:3]
        lines.append(f"| {agent} | {counts[agent]} | {'; '.join(f'«{reason}»' for reason in reasons)} |")
    return lines + [""]


def agree_block(ballots):
    """Сверка выбора с перевесами цифр в том же ответе: где порядок по выборам совпал с порядком по перевесам."""
    scored = [ballot for ballot in ballots
              if ballot["logprob"]["1"] is not None and ballot["logprob"]["2"] is not None]
    if not scored:
        return ["### Согласие выбора с перевесами", "",
                "Перевесы цифр в ответах с причиной не вернулись, сверять нечего.", ""]
    same = sum(1 for ballot in scored
               if ballot["picked"] == (ballot["one"] if ballot["logprob"]["1"] >= ballot["logprob"]["2"]
                                       else ballot["two"]))
    return ["### Согласие выбора с перевесами", "",
            f"Из {len(ballots)} ответов перевесы цифр вернулись в {len(scored)}. Выбранная сторона "
            f"совпала с более вероятной в {same} из {len(scored)} — {round(100 * same / len(scored))} %. "
            "В остальных парах выбор при температуре 0.5 пал на менее вероятный вариант, поэтому "
            "порядок по выборам и порядок по перевесам разошлись именно там.", ""]


def report():
    """Сводка повторного голосования по всем собранным прогонам, отдельными блоками по сериям."""
    paths = sorted((store.ROOT / "runs/revote").glob("*.json"))
    if not paths:
        return "Повторных голосов нет: сначала python run.py revote --id ..."
    states, subsets = {}, {}
    for path in paths:
        data = store.read_json(path)
        states[data["id"]] = store.read_json(store.run_file("state", data["id"]))
        subsets[data["id"]] = data["ballots"]
    groups = []
    for ident, state in states.items():
        for key, subset in groups:
            if key == analysis.series_key(state):
                subset.append(ident)
                break
        else:
            groups.append((analysis.series_key(state), [ident]))
    lines = ["# Голосование с причинами", "",
             "Прежнее голосование регистрировало только логпробы цифр «1» и «2»: виден выбор "
             "и перевес, причины нет. Здесь те же вопросы заданы заново от тех же историй, но "
             "ответ — JSON с полями choice и reason. Прежние голоса не перезаписаны: колонка τ "
             "показывает, насколько новый порядок совпал с прежним, а «перевёрнуто пар» — "
             "сколько предпочтений изменилось. Порядок — по числу выборов в парах; при равенстве "
             "решают сумма перевесов и имя.", ""]
    for key, subset in groups:
        lines += [f"# Серия — {analysis.series_title(key)}", ""]
        for ident in subset:
            lines += run_block(states[ident], subsets[ident])
        lines += why_block([states[i] for i in subset], subsets)
        lines += agree_block([ballot for ident in subset for ballot in subsets[ident]])
    return "\n".join(lines).rstrip() + "\n"

