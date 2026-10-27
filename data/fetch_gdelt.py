"""Недельный P-индекс Франции из дневных файлов событий GDELT 1.0 и запись data/series.csv.

P-индекс — число событий с корневым кодом CAMEO 14 (протест) и местом действия
во Франции (ActionGeo_CountryCode = FR), попавших в дневные файлы GDELT за неделю
с понедельника. Счёт идёт по дате файла, то есть по дню, когда событие зарегистрировано.
Если дневного файла в GDELT нет, сумма недели пересчитывается на 7 дней по имеющимся.
Дневные числа сохраняются в data/gdelt_daily.csv и повторно не скачиваются.
Запуск:
    python data/fetch_gdelt.py 2024-01-01 2025-06-09
"""
import csv
import datetime as dt
import io
import sys
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

URL = "http://data.gdeltproject.org/events/{:%Y%m%d}.export.CSV.zip"
ROOT_CODE, COUNTRY = 28, 51  # EventRootCode, ActionGeo_CountryCode
HERE = Path(__file__).resolve().parent
DAILY = HERE / "gdelt_daily.csv"


def count(day):
    """Число протестных событий во Франции в дневном файле; None, если файла нет."""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(URL.format(day), timeout=300) as response:
                archive = zipfile.ZipFile(io.BytesIO(response.read()))
            break
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if attempt == 2:
                raise
        except OSError:
            if attempt == 2:
                raise
    with archive.open(archive.namelist()[0]) as file:
        rows = csv.reader(io.TextIOWrapper(file, "utf-8", errors="replace"), delimiter="\t")
        return sum(1 for row in rows if len(row) > COUNTRY and row[ROOT_CODE] == "14" and row[COUNTRY] == "FR")


def load_daily():
    if not DAILY.exists():
        return {}
    with open(DAILY, encoding="utf-8", newline="") as file:
        return {dt.date.fromisoformat(r["day"]): int(r["count"]) if r["count"] else None
                for r in csv.DictReader(file)}


def save_daily(daily):
    with open(DAILY, "w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["day", "count"])
        writer.writerows((d.isoformat(), "" if c is None else c) for d, c in sorted(daily.items()))


def main(first, last):
    first, last = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    if first.weekday() != 0 or last.weekday() != 0:
        raise ValueError("Границы — понедельники")
    days = [first + dt.timedelta(days=i) for i in range((last - first).days)]
    daily = load_daily()
    todo = [d for d in days if d not in daily]
    try:
        with ThreadPoolExecutor(4) as pool:
            for day, value in zip(todo, pool.map(count, todo)):
                daily[day] = value
    finally:
        save_daily(daily)
    weeks, missing = [], []
    for i in range(len(days) // 7):
        monday = first + dt.timedelta(weeks=i)
        values = [daily[monday + dt.timedelta(days=d)] for d in range(7)]
        known = [v for v in values if v is not None]
        missing += [monday + dt.timedelta(days=d) for d, v in enumerate(values) if v is None]
        if len(known) < 5:
            raise RuntimeError(f"В неделе {monday} меньше 5 дней с данными")
        weeks.append((monday.isoformat(), round(sum(known) * 7 / len(known))))
    with open(HERE / "series.csv", "w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["week", "value"])
        writer.writerows(weeks)
    print(f"{len(weeks)} недель, {weeks[0][0]} — {weeks[-1][0]}; нет файлов за: "
          + (", ".join(map(str, missing)) or "—"))


if __name__ == "__main__":
    main(*sys.argv[1:3])
