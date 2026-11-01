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


# --- проба повторов ----------------------------------------------------------

PROBE_REPEATS = 5


def probe_file(ident, agent):
    """Файл пробы повторов: runs/probe/<id>-<агент>.json."""
    return store.ROOT / "runs" / "probe" / f"{ident}-{agent}.json"


def messages(state, agent, one, two, structured):
    """Сообщения одного вопроса: история агента и вопрос пары в прежнем или структурированном виде."""
    system = runner.system_prompt(state["spec"]["condition"])
    text = question(one, two) if structured else runner.text("pair").format(first=one, second=two)
    return [{"role": "system", "content": system}, *state["histories"][agent],
            {"role": "user", "content": text}]


def probe(ident, agent, repeats=PROBE_REPEATS):
    """Повторить голос одного агента по каждой паре: сначала цифрой, затем JSON с причиной.

    Прежнее голосование читало логпробы — измерение без сэмплирования, и один вызов
    даёт всё распределение. Здесь тот же вопрос задаётся несколько раз, чтобы видеть,
    воспроизводится ли ответ, и меняется ли распределение от формата.
    """
    state = store.read_json(store.run_file("state", ident))
    profile = state["spec"]["vote"]
    cells = []
    for x, y in runner.pairs([a for a in runner.names() if a != agent]):
        one, two = (y, x) if int(store.digest([ident, agent, x, y]), 16) % 2 else (x, y)
        previous = next(v for v in state["votes"][agent] if set(v["pair"]) == {x, y})
        cell = {"pair": [x, y], "one": one, "two": two,
                "old_margin": round(previous["logprob"][one] - previous["logprob"][two], 2),
                "digits": [], "picks": [], "margins": [], "reasons": []}
        for repeat in range(repeats):
            values = llm.choice(profile, store.digest([ident, "probe-цифра", agent, x, y, repeat]),
                                messages(state, agent, one, two, False), ("1", "2"))
            cell["digits"].append(None if values["1"] is None or values["2"] is None
                                  else round(values["1"] - values["2"], 2))
            value = llm.ballot(profile, store.digest([ident, "probe-JSON", agent, x, y, repeat]),
                               messages(state, agent, one, two, True))
            try:
                parsed = parse(value)
                cell["picks"].append(one if parsed["choice"] == 1 else two)
                cell["reasons"].append(parsed["reason"])
            except ValueError:
                cell["picks"].append(None)
                cell["reasons"].append("")
            cell["margins"].append(None if value["logprob"]["1"] is None or value["logprob"]["2"] is None
                                   else round(value["logprob"]["1"] - value["logprob"]["2"], 2))
        cells.append(cell)
    record = {"id": ident, "agent": agent, "repeats": repeats, "cells": cells}
    store.write_json(probe_file(ident, agent), record)
    return record


def probe_block(state, record):
    """Строки пробы: перевесы и выборы повторов рядом с прежним голосом и итог по парам."""
    cells, repeats = record["cells"], record["repeats"]
    steady = sum(1 for cell in cells
                 if None not in cell["digits"] and len({digit > 0 for digit in cell["digits"]}) == 1)
    same_pick = sum(1 for cell in cells if None not in cell["picks"] and len(set(cell["picks"])) == 1)
    shifts = [abs(sum(cell["digits"]) / len(cell["digits"]) - cell["old_margin"])
              for cell in cells if None not in cell["digits"]]
    spreads = [max(cell["digits"]) - min(cell["digits"]) for cell in cells if None not in cell["digits"]]
    shift = sum(shifts) / len(shifts) if shifts else 0.0
    spread = max(spreads) if spreads else 0.0
    digit_line = (f"Знак перевеса в формате цифры совпал во всех повторах в {steady} "
                  f"{pair_form(steady)} из {len(cells)}; "
                  + ("значения повторились до последнего знака — распределение одно и то же."
                     if spread < 0.05 else
                     f"внутри одной пары значения расходятся до {spread:.2f} нат: провайдеры "
                     "одной модели отдают разные распределения."))
    shift_line = (f"Перевес при смене формата сдвигается в среднем на {shift:.2f} нат: "
                  + ("распределение от формата почти не зависит."
                     if shift < 0.5 else
                     "структурированный ответ меняет само распределение, а не только запись."))
    lines = [f"### Проба: {repeats} вызовов на одну пару, агент {record['agent']}", "",
             f"Прогон {state['spec']['id'][:8]}: тот же агент с той же историей опрошен "
             f"по каждой паре {count_word(repeats, 'раз', 'раза', 'раз')} — сначала цифрой, "
             "затем JSON с причиной. Первый столбец — перевес прежнего голосования.", "",
             "| Пара | Прежний голос | Цифра: перевес каждого вызова | JSON: выбор каждого вызова |",
             "|---|---|---|---|"]
    for cell in cells:
        digits = ", ".join("—" if digit is None else f"{digit:+.2f}" for digit in cell["digits"])
        picks = ", ".join("—" if pick is None else pick for pick in cell["picks"])
        lines.append(f"| {cell['one']}–{cell['two']} | {cell['old_margin']:+.2f} | {digits} | {picks} |")
    lines += ["", digit_line,
              f"В формате с причиной выбор совпал во всех повторах в {same_pick} {pair_form(same_pick)} "
              f"из {len(cells)}; в остальных тот же агент назвал разных организаторов, и причина "
              "каждый раз подкрепляла названного.",
              shift_line, ""]
    return lines


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


def pair_form(n):
    """Форма слова «пара» после числительного: в 1 паре, в 2 парах, в 11 парах."""
    return "паре" if n % 10 == 1 and n % 100 != 11 else "парах"


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
             "решают сумма перевесов и имя.", "",
             "Первое голосование читало распределение цифр, а не выбирало токен; ответ с причиной "
             "цифру выбирает, поэтому одиночный голос здесь — один замер из распределения, а не "
             "само распределение. Насколько он устойчив, видно по пробе повторов в конце каждой "
             "серии (`vote-probe`): одинаковые вызовы расходятся там, где перевес мал, и причина "
             "каждый раз подкрепляет уже названную сторону.", ""]
    for key, subset in groups:
        lines += [f"# Серия — {analysis.series_title(key)}", ""]
        for ident in subset:
            lines += run_block(states[ident], subsets[ident])
        lines += why_block([states[i] for i in subset], subsets)
        lines += agree_block([ballot for ident in subset for ballot in subsets[ident]])
        for path in sorted((store.ROOT / "runs/probe").glob("*.json")):
            record = store.read_json(path)
            if record["id"] in subset:
                lines += probe_block(states[record["id"]], record)
    return "\n".join(lines).rstrip() + "\n"

