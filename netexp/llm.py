"""Вызов модели по OpenAI-совместимому API с кэшем ответов и общим лимитом запросов в requests.sqlite3."""
import hashlib
import json
import os
import sqlite3

from . import store


def call(profile, scope, messages):
    """Ответ модели на messages. scope — ID запроса прогона: повтор того же запроса берётся из кэша."""
    store.check_stop()
    cfg = store.config()
    api, engine = cfg["api"], cfg["engines"][profile["engine"]]
    key = hashlib.sha256(json.dumps([scope, profile["model"], profile["settings"], messages],
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    db = sqlite3.connect(store.ROOT / "requests.sqlite3", timeout=300)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, model TEXT, response TEXT, usage TEXT)")
        # Одна транзакция на запрос: параллельные процессы не превысят лимит и не оплатят ответ дважды.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT response FROM requests WHERE id=?", (key,)).fetchone()
        if row:
            if not row[0]:
                raise RuntimeError("Сохранён пустой ответ API")
            return row[0]
        if db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= api["max_requests"]:
            raise RuntimeError("Достигнут общий лимит запросов max_requests")
        from openai import OpenAI
        with OpenAI(api_key=os.environ[engine["key_env"]], base_url=engine["base_url"],
                    timeout=180, max_retries=0) as client:
            response = client.chat.completions.create(model=profile["model"], messages=messages,
                                                      max_tokens=api["max_tokens"], **profile["settings"])
        answer = (response.choices[0].message.content or "").strip()
        usage = response.usage.model_dump() if response.usage else {}
        # Пустой ответ тоже сохраняется: он оплачен и входит в учёт расхода.
        db.execute("INSERT INTO requests VALUES (?, ?, ?, ?)", (key, profile["model"], answer, json.dumps(usage)))
        db.commit()
        if not answer:
            raise RuntimeError("API вернул пустой ответ; запрос сохранён для учёта расхода")
        return answer
    finally:
        db.close()
