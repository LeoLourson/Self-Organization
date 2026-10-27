"""Недельный P-индекс Франции из дневных файлов событий GDELT 1.0 и запись data/series.csv.

P-индекс — число событий с корневым кодом CAMEO 14 (протест) и местом действия
во Франции (ActionGeo_CountryCode = FR), попавших в дневные файлы GDELT за неделю
с понедельника. Счёт идёт по дате файла, то есть по дню, когда событие зарегистрировано.
Запуск:
    python data/fetch_gdelt.py 2024-01-01 2025-12-29
"""
import csv
import datetime as dt
import io
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

URL = "http://data.gdeltproject.org/events/{:%Y%m%d}.export.CSV.zip"
ROOT_CODE, COUNTRY = 28, 51  # EventRootCode, ActionGeo_CountryCode


def count(day):
    """Число протестных событий во Франции в дневном файле."""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(URL.format(day), timeout=300) as response:
                archive = zipfile.ZipFile(io.BytesIO(response.read()))
            break
        except OSError:
            if attempt == 2:
                raise
    with archive.open(archive.namelist()[0]) as file:
        rows = csv.reader(io.TextIOWrapper(file, "utf-8", errors="replace"), delimiter="\t")
        return sum(1 for row in rows if len(row) > COUNTRY and row[ROOT_CODE] == "14" and row[COUNTRY] == "FR")


def main(first, last):
    first, last = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    if first.weekday() != 0 or last.weekday() != 0:
        raise ValueError("Границы — понедельники")
    days = [first + dt.timedelta(days=i) for i in range((last - first).days)]
    with ThreadPoolExecutor(4) as pool:
        daily = dict(zip(days, pool.map(count, days)))
    weeks = [((first + dt.timedelta(weeks=i)).isoformat(),
              sum(daily[first + dt.timedelta(weeks=i, days=d)] for d in range(7)))
             for i in range(len(days) // 7)]
    path = Path(__file__).resolve().parent / "series.csv"
    with open(path, "w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["week", "value"])
        writer.writerows(weeks)
    print(f"{path}: {len(weeks)} недель, {weeks[0][0]} — {weeks[-1][0]}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
