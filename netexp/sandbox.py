"""Песочница для субагентов: временно убирает пользовательские инструкции Claude Code и возвращает их.

on переносит пути из config.sandbox.paths в backup/sandbox/ и выключает плагины
в settings.json; off возвращает всё на место. Пока песочница включена, эти
инструкции не действуют ни в одной сессии Claude Code.
"""
import hashlib
import json
import shutil
import time
from pathlib import Path

from . import store

SETTINGS = Path("~/.claude/settings.json")


def backup_dir():
    return store.ROOT / "backup" / "sandbox"


def manifest_file():
    return backup_dir() / "manifest.json"


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def on():
    if manifest_file().exists():
        raise RuntimeError("Песочница уже включена: сначала выполните sandbox off")
    cfg = store.config()["sandbox"]
    backup_dir().mkdir(parents=True, exist_ok=True)
    moved = []
    for index, raw in enumerate(cfg["paths"]):
        path = Path(raw).expanduser()
        if path.exists():
            target = backup_dir() / f"{index}_{path.name}"
            shutil.move(str(path), str(target))
            moved.append({"original": str(path), "backup": str(target)})
    settings = None
    path = SETTINGS.expanduser()
    if cfg["disable_plugins"] and path.is_file():
        copy = backup_dir() / "settings.json"
        shutil.copy2(path, copy)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["enabledPlugins"] = {name: False for name in data.get("enabledPlugins", {})}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        settings = {"original": str(path), "backup": str(copy), "written": file_hash(path)}
    store.write_json(manifest_file(), {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                       "moved": moved, "settings": settings})
    return {"sandbox": "on", "moved": [m["original"] for m in moved],
            "plugins_disabled": settings is not None}


def off():
    if not manifest_file().exists():
        raise RuntimeError("Песочница не включена")
    manifest = store.read_json(manifest_file())
    # Ничего не перезаписываем: если за время песочницы появился новый файл, решает человек.
    conflicts = [m["original"] for m in manifest["moved"] if Path(m["original"]).exists()]
    settings = manifest["settings"]
    if settings and file_hash(Path(settings["original"])) != settings["written"]:
        conflicts.append(settings["original"])
    if conflicts:
        raise RuntimeError("Изменены за время песочницы, перенесите вручную: " + ", ".join(conflicts))
    for item in manifest["moved"]:
        shutil.move(item["backup"], item["original"])
    if settings:
        shutil.copy2(settings["backup"], settings["original"])
        Path(settings["backup"]).unlink()
    manifest_file().unlink()
    return {"sandbox": "off", "restored": [m["original"] for m in manifest["moved"]],
            "plugins_restored": settings is not None}


def status():
    return {"sandbox": "on" if manifest_file().exists() else "off"}


def write_agent_definition():
    """Файл субагента Claude Code: системный промпт эксперимента и модель профиля.

    Инструменты сужены до одного безвредного: пустой список Claude Code понимает
    как «все инструменты».
    """
    from . import run
    cfg = store.config()
    models = sorted({p["model"] for p in cfg["profiles"].values() if p["transport"] == "subagent"})
    if len(models) != 1:
        raise ValueError("Файл субагента рассчитан на одну модель субагентов в config.json")
    body = (f"---\nname: {cfg['subagent_type']}\n"
            "description: Участник эксперимента с пулом агентов. Запускает только оркестратор эксперимента.\n"
            f"model: {models[0]}\ntools: TodoWrite\n---\n\n{run.system_prompt()}\n")
    path = store.ROOT / ".claude" / "agents" / f"{cfg['subagent_type']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return {"agent_file": store.relative(path), "model": models[0]}
