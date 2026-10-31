"""Вызов модели по OpenAI-совместимому API с кэшем ответов и общим лимитом запросов в requests.sqlite3.

Помимо облачных движков поддержан движок `local` — любой локальный сервер с тем же
API (Ollama, vLLM, LM Studio, llama.cpp). У него можно не задавать ключ, а адрес —
переопределить переменной окружения; подробности в Запуск_6B.md.
"""
import hashlib
import json
import math
import os
import sqlite3

from . import store


def endpoint(engine):
    """Адрес и ключ движка. У локальных движков адрес переопределяется переменной окружения."""
    base_url = os.environ.get(engine.get("base_url_env", ""), "") or engine["base_url"]
    if engine.get("key_optional"):
        return base_url, os.environ.get(engine.get("key_env", ""), "local")
    return base_url, os.environ[engine["key_env"]]


def _cached(key, model, produce, logprob_id=None):
    """Ответ produce() с кэшем и общим лимитом; produce делает запрос и возвращает (ответ, расход, логиты)."""
    db = sqlite3.connect(store.ROOT / "requests.sqlite3", timeout=300)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, model TEXT, response TEXT, usage TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS logprobs (id TEXT PRIMARY KEY, summary TEXT)")
        # Одна транзакция на запрос: параллельные процессы не превысят лимит и не оплатят ответ дважды.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT response FROM requests WHERE id=?", (key,)).fetchone()
        if row:
            return row[0]
        if db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= store.config()["api"]["max_requests"]:
            raise RuntimeError("Достигнут общий лимит запросов max_requests")
        response, usage, summary = produce()
        # Пустой ответ тоже сохраняется: он оплачен и входит в учёт расхода.
        db.execute("INSERT INTO requests VALUES (?, ?, ?, ?)", (key, model, response, json.dumps(usage)))
        if summary:
            db.execute("INSERT OR REPLACE INTO logprobs VALUES (?, ?)",
                       (logprob_id or key, json.dumps(summary, ensure_ascii=False)))
        db.commit()
        return response
    finally:
        db.close()


def number_logprobs(choice, limit=80):
    """Логиты числовых токенов ответа и общая уверенность; None, если сервер логиты не отдал.

    Для вопросов о конвенциях и памяти достаточно чисел: по альтернативам видно,
    в каких числах прогноза модель была не уверена и какие рассматривала.
    """
    content = getattr(getattr(choice, "logprobs", None), "content", None)
    if not content:
        return None
    items = list(content)
    numbers = []
    for item in items:
        if not any(ch.isdigit() for ch in (getattr(item, "token", "") or "")):
            continue
        alternatives = [{"token": alt.token, "logprob": alt.logprob}
                        for alt in (getattr(item, "top_logprobs", None) or [])]
        numbers.append({"token": item.token, "logprob": item.logprob, "top": alternatives})
        if len(numbers) >= limit:
            break
    return {"n_tokens": len(items),
            "mean_top1": round(sum(item.logprob for item in items) / len(items), 4),
            "numbers": numbers}


def call(profile, scope, messages):
    """Ответ модели на messages; может быть пустой строкой. scope — ID запроса прогона: повтор берётся из кэша."""
    store.check_stop()
    cfg = store.config()
    api, engine = cfg["api"], cfg["engines"][profile["engine"]]
    key = hashlib.sha256(json.dumps([scope, profile["model"], profile["settings"], messages],
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def produce():
        base_url, api_key = endpoint(engine)
        settings = dict(profile["settings"])
        # Общий предел длины ответа; профиль может задать свой: локальным моделям нужен меньший.
        settings.setdefault("max_tokens", api["max_tokens"])
        from openai import OpenAI
        with OpenAI(api_key=api_key, base_url=base_url, timeout=180, max_retries=0) as client:
            response = client.chat.completions.create(model=profile["model"], messages=messages, **settings)
        choice = response.choices[0]
        answer = (choice.message.content or "").strip()
        try:
            summary = number_logprobs(choice)
        except (AttributeError, TypeError, ValueError):
            summary = None  # формат логитов зависит от сервера; без них прогноз остаётся без уверенности
        return answer, response.usage.model_dump() if response.usage else {}, summary

    return _cached(key, profile["model"], produce, scope)


def cached_logprobs(scope):
    """Сохранённые логиты по scope запроса; None, если их нет: без логитов, тестовый дубль или другой профиль."""
    db = sqlite3.connect(store.ROOT / "requests.sqlite3", timeout=300)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS logprobs (id TEXT PRIMARY KEY, summary TEXT)")
        row = db.execute("SELECT summary FROM logprobs WHERE id=?", (scope,)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        db.close()


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
        base_url, api_key = endpoint(engine)
        from openai import OpenAI
        settings = {k: v for k, v in profile["settings"].items()
                    if k not in ("max_tokens", "logprobs", "top_logprobs")}
        with OpenAI(api_key=api_key, base_url=base_url, timeout=180, max_retries=0) as client:
            response = client.chat.completions.create(model=profile["model"], messages=messages,
                                                      max_tokens=1, logprobs=True, top_logprobs=20,
                                                      **settings)
        return json.dumps(first_token_logprobs(response, candidates)), \
            response.usage.model_dump() if response.usage else {}, None

    return json.loads(_cached(key, profile["model"], produce))
