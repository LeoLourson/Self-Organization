"""Вызов модели по OpenAI-совместимому API с кэшем ответов и общим лимитом запросов в requests.sqlite3."""
import hashlib
import json
import math
import os
import sqlite3

from . import store


def _cached(key, model, produce):
    """Ответ produce() с кэшем и общим лимитом; produce делает запрос и возвращает (ответ, расход)."""
    db = sqlite3.connect(store.ROOT / "requests.sqlite3", timeout=300)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, model TEXT, response TEXT, usage TEXT)")
        # Одна транзакция на запрос: параллельные процессы не превысят лимит и не оплатят ответ дважды.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT response FROM requests WHERE id=?", (key,)).fetchone()
        if row:
            return row[0]
        if db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= store.config()["api"]["max_requests"]:
            raise RuntimeError("Достигнут общий лимит запросов max_requests")
        response, usage = produce()
        # Пустой ответ тоже сохраняется: он оплачен и входит в учёт расхода.
        db.execute("INSERT INTO requests VALUES (?, ?, ?, ?)", (key, model, response, json.dumps(usage)))
        db.commit()
        return response
    finally:
        db.close()


def call(profile, scope, messages):
    """Ответ модели на messages; может быть пустой строкой. scope — ID запроса прогона: повтор берётся из кэша."""
    store.check_stop()
    engine = store.config()["engines"][profile["engine"]]
    key = hashlib.sha256(json.dumps([scope, profile["model"], profile["settings"], messages],
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def produce():
        from openai import OpenAI
        with OpenAI(api_key=os.environ[engine["key_env"]], base_url=engine["base_url"],
                    timeout=180, max_retries=0) as client:
            response = client.chat.completions.create(model=profile["model"], messages=messages,
                                                      max_tokens=store.config()["api"]["max_tokens"],
                                                      **profile["settings"])
        return (response.choices[0].message.content or "").strip(), \
            response.usage.model_dump() if response.usage else {}

    return _cached(key, profile["model"], produce)


def _logsumexp(values):
    top = max(values)
    return top + math.log(sum(math.exp(v - top) for v in values))


def first_token_logprobs(response, candidates):
    """Логпробы кандидатов в первом токене ответа: {кандидат: логпроб или None}.

    Токен считается ответом-кандидатом, если после отбрасывания пробелов совпадает
    с ним целиком (учитывает варианты вида « 1»); вклад нескольких таких токенов
    складывается через logsumexp. Кандидата, которого нет среди топ-20 логпробов,
    в ответе нет: его значение None.
    """
    logprobs = getattr(response.choices[0], "logprobs", None)
    content = getattr(logprobs, "content", None) if logprobs else None
    if not content:
        raise ValueError("Эндпоинт не вернул логпробы; он должен поддерживать logprobs и top_logprobs")
    entry = content[0]
    tokens = [(item.token, float(item.logprob)) for item in (entry.top_logprobs or [])]
    if all(token != entry.token for token, _ in tokens):
        tokens.append((entry.token, float(entry.logprob)))
    values = {}
    for candidate in candidates:
        matched = [logprob for token, logprob in tokens if token.strip() == candidate]
        values[candidate] = _logsumexp(matched) if matched else None
    return values


def choice(profile, scope, messages, candidates):
    """Логпробы кандидатов в первом токене ответа; ответ запрашивается одним токеном.

    Для вопросов с ответом в одно слово это измерение без сэмплирования: логпробы
    не зависят от температуры и дают не только выбор, но и перевес. Как и call,
    повтор берётся из кэша.
    """
    store.check_stop()
    engine = store.config()["engines"][profile["engine"]]
    key = hashlib.sha256(json.dumps(["choice", scope, profile["engine"], profile["model"], profile["settings"],
                                     messages, list(candidates)], ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def produce():
        from openai import OpenAI
        with OpenAI(api_key=os.environ[engine["key_env"]], base_url=engine["base_url"],
                    timeout=180, max_retries=0) as client:
            response = client.chat.completions.create(model=profile["model"], messages=messages,
                                                      max_tokens=1, logprobs=True, top_logprobs=20,
                                                      **profile["settings"])
        return json.dumps(first_token_logprobs(response, candidates)), \
            response.usage.model_dump() if response.usage else {}

    return json.loads(_cached(key, profile["model"], produce))
