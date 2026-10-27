"""Файлы проекта: конфигурация, профили моделей, каталог runs/."""
import copy
import hashlib
import json
import re
from pathlib import Path

# Остальные модули обращаются к store.ROOT, чтобы тесты могли подменить корень.
ROOT = Path(__file__).resolve().parent.parent


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    """Запись через временный файл: прерванная запись не портит прежнее содержимое."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def config():
    return read_json(ROOT / "config.json")


def profile(name):
    """Профиль модели из config.json с проверкой, что по нему можно запускать прогон."""
    cfg = config()
    value = copy.deepcopy(cfg["profiles"][name])
    if value.get("transport") == "api":
        if value.get("engine") not in cfg["engines"]:
            raise ValueError("Движок API не описан в engines")
    elif value.get("transport") == "subagent":
        if value.get("engine") != "claude-code":
            raise ValueError("Субагенты поддерживаются только для claude-code")
    else:
        raise ValueError("Неизвестный транспорт профиля")
    if not isinstance(value.get("model"), str) or not value["model"].strip():
        raise ValueError(f"Укажите точную модель в profiles.{name}.model")
    if not isinstance(value.get("settings"), dict):
        raise ValueError("settings должны быть объектом")
    return {"id": name, **value}


PROTOCOL_FIELDS = ("agents", "horizon", "history_weeks", "first_origin", "worker_words", "data")


def protocol_digest():
    """Хеш всего, что видят агенты: тексты, данные, длины. Профили моделей сюда не входят."""
    cfg = config()
    files = [*sorted((ROOT / "prompts").glob("*.txt")), ROOT / "prompts/summary.json", ROOT / "prompts/roles.json", ROOT / cfg["data"]]
    fields = json.dumps([cfg[name] for name in PROTOCOL_FIELDS] + [cfg["api"]["max_tokens"]]).encode()
    return hashlib.sha256(b"".join(p.read_bytes() for p in files) + fields).hexdigest()[:12]


def run_file(kind, ident):
    """Путь runs/<kind>/<ident>.json; kind — state, results, requests."""
    if not re.fullmatch(r"[a-f0-9]{24}", ident):
        raise ValueError("Некорректный ID")
    return ROOT / "runs" / kind / f"{ident}.json"


def relative(path):
    return path.relative_to(ROOT).as_posix()


def check_stop():
    if (ROOT / "STOP").exists():
        raise RuntimeError("Найден файл STOP")
