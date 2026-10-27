"""Ряд P-индекса: чтение data/*.csv и показ агентам под условными метками недель."""
import csv

from . import store


def load():
    """Значения ряда по порядку. CSV с колонками week, value; настоящие даты агентам не показываются."""
    with open(store.ROOT / store.config()["data"], encoding="utf-8", newline="") as file:
        return [float(row["value"]) for row in csv.DictReader(file)]


def label(index):
    return f"неделя {index + 1}"


def table(values, start):
    return "\n".join(f"{label(start + i)}: {v:g}" for i, v in enumerate(values))


def window(round_index):
    """Начало истории, точка прогноза и последняя неделя горизонта для раунда round_index."""
    cfg = store.config()
    origin = cfg["first_origin"] + round_index * cfg["horizon"]
    return origin - cfg["history_weeks"], origin, origin + cfg["horizon"]


def check():
    cfg, values = store.config(), load()
    if cfg["first_origin"] < cfg["history_weeks"] or window(cfg["agents"] - 1)[2] > len(values):
        raise ValueError("Ряд короче, чем требуют first_origin, history_weeks, horizon и число раундов")


def mase(forecast, actual, history):
    """Средняя абсолютная ошибка, делённая на ошибку наивного прогноза «как на прошлой неделе» по истории."""
    naive = sum(abs(b - a) for a, b in zip(history, history[1:])) / (len(history) - 1)
    error = sum(abs(f - a) for f, a in zip(forecast, actual)) / len(actual)
    return error / naive if naive else None
