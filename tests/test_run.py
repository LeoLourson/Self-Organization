"""Проверки без платных вызовов: python -m unittest discover tests -v"""
import json
import math
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netexp import llm, run, sandbox, store

PROJECT = store.ROOT
ROLES = list(store.read_json(PROJECT / "prompts/roles.json"))


def fake_answer(message, tag="?"):
    """Корректный ответ на последнее сообщение среды, как дал бы послушный агент или судья."""
    if "Ответ не принят" in message:
        message = fake_answer.last
    fake_answer.last = message
    if '{"assignments"' in message:
        team = re.findall(r'"(A\d+)": \{"role"', message)
        return json.dumps({"assignments": {a: {"role": ROLES[i % 2], "task": f"задание {a}"}
                                           for i, a in enumerate(team)}}, ensure_ascii=False)
    if '{"forecast"' in message:
        return '{"forecast": [10, 11, 12, 13]}'
    if '"ranking"' in message:
        others = message.split("Кого из агентов ")[1].split(" ты")[0].split(", ")
        return json.dumps({"ranking": others, "reason": "по ролям"}, ensure_ascii=False)
    if '"allocation"' in message:
        return '{"allocation": 4, "comment": "ок"}'
    if '"role_fit"' in message:
        return '{"role_fit": 3, "usefulness": 2, "comment": "ок"}'
    return f"ответ {tag}"


class RunTests(unittest.TestCase):
    def setUp(self):
        """Тексты проекта и синтетический ряд во временном корне."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copytree(PROJECT / "prompts", self.root / "prompts")
        cfg = store.read_json(PROJECT / "config.json")
        cfg["data"] = "data/test.csv"
        (self.root / "data").mkdir()
        rows = ["week,value"] + [f"{i},{20 + 5 * math.sin(i / 3):.1f}" for i in range(80)]
        (self.root / "data/test.csv").write_text("\n".join(rows), encoding="utf-8")
        (self.root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        context = patch.object(store, "ROOT", self.root)
        context.start()
        self.addCleanup(context.stop)
        self.calls = []

    def fake_call(self, profile, scope, messages):
        self.calls.append((profile["id"], messages))
        return fake_answer(messages[-1]["content"], scope)

    def test_api_run_reaches_judge(self):
        with patch.object(llm, "call", self.fake_call):
            ident = run.start("deepseek", "open", 1)["id"]
            result = run.advance(ident)
        self.assertTrue(result["done"])
        data = store.read_json(self.root / result["result_file"])
        self.assertEqual(data["status"], "done")
        # Повтор 1 сдвигает ротацию: первым организует A2.
        self.assertEqual([r["organizer"] for r in data["rounds"]], ["A2", "A3", "A4", "A5", "A1"])
        self.assertEqual(len(data["votes"]), 5)
        agents = [c for c in self.calls if c[0] == "deepseek"]
        judges = [c for c in self.calls if c[0] == "deepseek_pro"]
        self.assertEqual(len(agents), 5 * 6 + 5 + 5)
        self.assertEqual(len(judges), 5 * 5)
        self.assertTrue(all(j["error"] is None for j in data["judgments"]))
        self.assertTrue(all(set(row) == {"accuracy", "judge_allocation"} for row in data["vote_tau"].values()))
        # История агента растёт, а меню ролей приходит в первом сообщении.
        last = agents[-1][1]
        self.assertEqual(last[0]["role"], "system")
        self.assertGreater(len(last), 10)
        self.assertIn("Список ролей", last[1]["content"])

    def test_judge_does_not_see_outcome_or_agents_see_judge(self):
        with patch.object(llm, "call", self.fake_call):
            ident = run.start("deepseek", "open")["id"]
            run.advance(ident)
        data = store.read_json(store.run_file("results", ident))
        for profile, messages in self.calls:
            if profile == "deepseek_pro":
                self.assertNotIn("Реальные значения", messages[-1]["content"])
                self.assertNotIn("Организатор:", messages[-1]["content"])
        seen = json.dumps(data["histories"], ensure_ascii=False)
        self.assertNotIn("role_fit", seen)

    def test_conditions_control_summary(self):
        with patch.object(llm, "call", self.fake_call):
            for condition, assignments in [("open", True), ("closed", False)]:
                ident = run.start("deepseek", condition)["id"]
                run.advance(ident)
                summary = store.read_json(store.run_file("results", ident))["rounds"][0]["summary"]
                self.assertEqual("Роли и задания" in summary, assignments, condition)
                self.assertIn("Прогноз организатора", summary)
                self.assertIn("Реальные значения", summary)
                self.assertNotIn("MASE", summary)

    def test_role_order_shuffled_per_run(self):
        orders = {tuple(run.spec("deepseek", "open", k)["role_order"]) for k in range(5)}
        self.assertGreater(len(orders), 1)
        self.assertEqual(run.spec("deepseek", "open", 0), run.spec("deepseek", "open", 0))

    def test_role_outside_menu_rejected(self):
        bad = json.dumps({"assignments": {a: {"role": "эконометрист", "task": "x"}
                                          for a in ("A2", "A3", "A4", "A5")}}, ensure_ascii=False)
        with patch.object(llm, "call", lambda p, s, m: bad):
            ident = run.start("deepseek", "closed")["id"]
            result = run.advance(ident)
        self.assertEqual(result["phase"], "failed")
        history = store.read_json(store.run_file("state", ident))["histories"]["A1"]
        self.assertIn("Ответ не принят", history[2]["content"])

    def test_private_answers_not_seen_by_others(self):
        with patch.object(llm, "call", self.fake_call):
            ident = run.start("deepseek", "open")["id"]
            run.advance(ident)
        state = store.read_json(store.run_file("state", ident))
        for item in state["rounds"]:
            for answer in item["answers"].values():
                readers = {a for a, h in state["histories"].items()
                           if any(answer in m["content"] for m in h if m["role"] == "user")}
                self.assertEqual(readers, {item["organizer"]})

    def test_subagent_run_keeps_one_session_per_agent(self):
        ident = run.start("sonnet_sub", "closed")["id"]
        sessions, sent = {}, 0
        with patch.object(llm, "call", self.fake_call):
            while True:
                status = run.advance(ident)
                if status["done"]:
                    break
                request = status["request"]
                agent = request["agent"]
                self.assertEqual(request["agent_id"], sessions.get(agent))
                if request["agent_id"]:
                    self.assertNotIn("Ты — агент", request["message"])
                sessions.setdefault(agent, f"sub-{agent}")
                envelope = {"request_id": request["request_id"],
                            "content": fake_answer(request["message"], request["request_id"]),
                            "execution": {"engine": "claude-code", "model": "claude-sonnet-5-5",
                                          "agent_id": sessions[agent]}}
                if request["agent_id"] and sent < 10:
                    wrong = dict(envelope, execution=dict(envelope["execution"], agent_id="sub-other"))
                    with self.assertRaises(ValueError):
                        run.submit(ident, wrong)
                run.submit(ident, envelope)
                sent += 1
        self.assertEqual(sent, 40)
        self.assertEqual(len(self.calls), 25)  # судья идёт через API и в прогоне на субагентах
        data = store.read_json(store.run_file("results", ident))
        self.assertEqual(data["sessions"], {a: f"sub-{a}" for a in run.names()})

    def test_kendall_tau(self):
        self.assertEqual(run.kendall_tau(["a", "b", "c"], ["a", "b", "c"]), 1)
        self.assertEqual(run.kendall_tau(["c", "b", "a"], ["a", "b", "c"]), -1)

    def test_sandbox_round_trip(self):
        home = self.root / "home"
        (home / ".claude/skills/x").mkdir(parents=True)
        (home / ".claude/CLAUDE.md").write_text("правила", encoding="utf-8")
        (home / ".claude/settings.json").write_text('{"enabledPlugins": {"p@m": true}, "theme": "dark"}',
                                                    encoding="utf-8")
        cfg = store.config()
        cfg["sandbox"]["paths"] = [str(home / ".claude/CLAUDE.md"), str(home / ".claude/skills"),
                                   str(home / "нет")]
        (self.root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        with patch.object(sandbox, "SETTINGS", home / ".claude/settings.json"):
            sandbox.on()
            self.assertFalse((home / ".claude/CLAUDE.md").exists())
            settings = json.loads((home / ".claude/settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["enabledPlugins"], {"p@m": False})
            with self.assertRaises(RuntimeError):
                sandbox.on()
            (home / ".claude/CLAUDE.md").write_text("новое", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                sandbox.off()
            (home / ".claude/CLAUDE.md").unlink()
            sandbox.off()
        self.assertEqual((home / ".claude/CLAUDE.md").read_text(encoding="utf-8"), "правила")
        self.assertTrue((home / ".claude/skills/x").is_dir())
        settings = json.loads((home / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["enabledPlugins"], {"p@m": True})


if __name__ == "__main__":
    unittest.main()
