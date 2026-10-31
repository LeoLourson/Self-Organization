"""Командная строка эксперимента. Порядок работы описан в README.md.

  start   --profile P --condition C [--repeat N]   создать прогон
  advance --id ID                                  исполнять прогон до запроса субагенту или до конца
  submit  --id ID --file FILE                      передать ответ субагента
  status  --id ID                                  фаза прогона и ожидающий запрос
  plan    --profile P [--conditions ...] --repeats N   перечислить прогоны порции
  analyze [--save]                                 сводка и статистики по всем прогонам
  check-vote --profile P                           проверить логпробы эндпоинта одним вопросом
  agent-file                                       записать .claude/agents/<subagent_type>.md
  sandbox on|off|status                            убрать или вернуть инструкции Claude Code
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

from netexp import analysis, run, sandbox, store


def plan(profile, conditions, repeats):
    """Прогоны порции; у завершённых done=true. Повторы 0..agents-1 закрывают латинский квадрат."""
    jobs = []
    for condition in conditions:
        for repeat in range(repeats):
            ident = run.spec(profile, condition, repeat)["id"]
            jobs.append({"id": ident, "profile": profile, "condition": condition, "repeat": repeat,
                         "done": store.run_file("results", ident).exists()})
    return jobs


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("start")
    p.add_argument("--profile", required=True)
    p.add_argument("--condition", choices=run.CONDITIONS, required=True)
    p.add_argument("--repeat", type=int, default=0)

    p = commands.add_parser("plan")
    p.add_argument("--profile", required=True)
    p.add_argument("--conditions", nargs="+", choices=run.CONDITIONS, default=list(run.CONDITIONS))
    p.add_argument("--repeats", type=int, required=True)

    p = commands.add_parser("analyze")
    p.add_argument("--save", action="store_true", help="записать отчёт в reports/ вместо вывода на экран")

    p = commands.add_parser("check-vote")
    p.add_argument("--profile", required=True)

    for name in ("advance", "status", "submit"):
        p = commands.add_parser(name)
        p.add_argument("--id", required=True)
        if name == "submit":
            p.add_argument("--file", required=True, help="JSON-конверт с ответом субагента")

    commands.add_parser("agent-file")
    p = commands.add_parser("sandbox")
    p.add_argument("action", choices=["on", "off", "status"])

    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "analyze":
        markdown = analysis.report()
        if args["save"]:
            path = store.ROOT / "reports" / f"analysis-{datetime.date.today().isoformat()}.md"
            path.parent.mkdir(exist_ok=True)
            path.write_text(markdown + "\n", encoding="utf-8")
            result = {"report": store.relative(path)}
        else:
            print(markdown)
            return
    elif command == "start":
        result = run.start(**args)
    elif command == "check-vote":
        result = run.check_vote(args["profile"])
    elif command == "plan":
        result = plan(**args)
    elif command == "submit":
        result = run.submit(args["id"], store.read_json(Path(args["file"])))
    elif command == "agent-file":
        result = sandbox.write_agent_definition()
    elif command == "sandbox":
        result = getattr(sandbox, args["action"])()
    else:
        result = {"advance": run.advance, "status": run.status}[command](args["id"])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
