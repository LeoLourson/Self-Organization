"""Командная строка эксперимента. Порядок работы описан в README.md.

  start   --profile P --condition C [--repeat N]   создать прогон
  advance --id ID                                  исполнять прогон до запроса субагенту или до конца
  submit  --id ID --file FILE                      передать ответ субагента
  status  --id ID                                  фаза прогона и ожидающий запрос
  plan    --profile P [--conditions ...] --repeats N   перечислить прогоны порции
  analyze [--save]                                 сводка и статистики по всем прогонам
  check-vote --profile P                           проверить логпробы эндпоинта одним вопросом
  revote   --id ID [--id ID ...] [--pairs N] [--fresh]  заново проголосовать по парам с причиной
  vote-probe --id ID --agent A1 [--repeats N]      повторить голос агента по парам: цифрой и JSON
  agent-file                                       записать .claude/agents/<subagent_type>.md
  sandbox on|off|status                            убрать или вернуть инструкции Claude Code
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

from netexp import analysis, revote, run, sandbox, store


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

    p = commands.add_parser("revote")
    p.add_argument("--id", action="append", required=True, help="ID прогона; можно указать несколько")
    p.add_argument("--pairs", type=int, help="только первые N пар на прогон — для пробы")
    p.add_argument("--fresh", action="store_true", help="забыть собранные голоса и пройти заново")

    p = commands.add_parser("revote-report")
    p.add_argument("--save", action="store_true", help="записать отчёт в reports/ вместо вывода на экран")

    p = commands.add_parser("vote-probe")
    p.add_argument("--id", required=True, help="ID прогона")
    p.add_argument("--agent", required=True, help="агент, чьи голоса повторяются")
    p.add_argument("--repeats", type=int, default=revote.PROBE_REPEATS, help="вызовов на пару")

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
    elif command == "revote":
        result = [revote.collect(ident, limit=args["pairs"], fresh=args["fresh"]) for ident in args["id"]]
    elif command == "revote-report":
        markdown = revote.report()
        if args["save"]:
            path = store.ROOT / "reports" / f"vote-reasons-{datetime.date.today().isoformat()}.md"
            path.parent.mkdir(exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
            result = {"report": store.relative(path)}
        else:
            print(markdown)
            return
    elif command == "vote-probe":
        record = revote.probe(args["id"], args["agent"], args["repeats"])
        state = store.read_json(store.run_file("state", args["id"]))
        print("\n".join(revote.probe_block(state, record)))
        return
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
