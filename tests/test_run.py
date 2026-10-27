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


def fake_answer(message, agent="A?"):
    """Корректный ответ на последнее сообщение среды, как дал бы послушный агент."""
    if "Ответ не принят" in message:
        message = fake_answer.last
    fake_answer.last = message
    if '{"tasks"' in message:
        team = re.findall(r'"(A\d+)": "задача"', message)
        return json.dumps({"tasks": {a: f"задача для {a}" for a in team}}, ensure_ascii=False)
    if '{"forecast"' in message:
        return '{"forecast": [10, 11, 12, 13]}'
    if '"ranking"' in message:
        others = message.split("агентов (")[1].split(")")[0].split(", ")
        return json.dumps({"ranking": others, "reason": "по точности"}, ensure_ascii=False)
    return f"ответ {agent}"


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
        self.calls.append(messages)
        return fake_answer(messages[-1]["content"], scope)

    def test_api_run_reaches_vote_and_scores(self):
        with patch.object(llm, "call", self.fake_call):
            ident = run.start("deepseek", "full", 1)["id"]
            result = run.advance(ident)
        self.assertTrue(result["done"])
        data = store.read_json(self.root / result["result_file"])
        self.assertEqual(data["status"], "done")
        # Повтор 1 сдвигает ротацию: первым организует A2.
        self.assertEqual([r["organizer"] for r in data["rounds"]], ["A2", "A3", "A4", "A5", "A1"])
        self.assertEqual(len(data["votes"]), 5)
        self.assertEqual(len(self.calls), 5 * 6 + 5)
        # История агента растёт: последний запрос несёт все его прежние сообщения.
        last = self.calls[-1]
        self.assertEqual(last[0]["role"], "system")
        self.assertGreater(len(last), 10)

    def test_conditions_control_summary(self):
        with patch.object(llm, "call", self.fake_call):
            for condition, tasks, accuracy in [("full", True, True), ("score", False, True),
                                               ("roles", True, False), ("blind", False, False)]:
                ident = run.start("deepseek", condition)["id"]
                run.advance(ident)
                summary = store.read_json(store.run_file("results", ident))["rounds"][0]["summary"]
                self.assertEqual("Задания организатора" in summary, tasks, condition)
                self.assertEqual("MASE" in summary, accuracy, condition)
                self.assertIn("Реальные значения", summary)

    def test_private_answers_not_seen_by_others(self):
        with patch.object(llm, "call", self.fake_call):
            ident = run.start("deepseek", "full")["id"]
            run.advance(ident)
        # Ответ исполнителя попадает во входящие сообщения только организатора раунда.
        state = store.read_json(store.run_file("state", ident))
        for item in state["rounds"]:
            for author, answer in item["answers"].items():
                readers = {a for a, h in state["histories"].items()
                           if any(answer in m["content"] for m in h if m["role"] == "user")}
                self.assertEqual(readers, {item["organizer"]})

    def test_invalid_answer_retried_then_failed(self):
        answers = iter(["не json", '{"tasks": {}}', "снова нет"])
        with patch.object(llm, "call", lambda p, s, m: next(answers)):
            ident = run.start("deepseek", "blind")["id"]
            result = run.advance(ident)
        self.assertEqual(result["phase"], "failed")
        history = store.read_json(store.run_file("state", ident))["histories"]["A1"]
        self.assertIn("Ответ не принят", history[2]["content"])

    def test_subagent_run_keeps_one_session_per_agent(self):
        ident = run.start("sonnet_sub", "score")["id"]
        sessions, sent = {}, 0
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
            envelope = {"request_id": request["request_id"], "content": fake_answer(request["message"], agent),
                        "execution": {"engine": "claude-code", "model": "claude-sonnet-5-5",
                                      "agent_id": sessions[agent]}}
            if request["agent_id"] and sent < 10:
                wrong = dict(envelope, execution=dict(envelope["execution"], agent_id="sub-other"))
                with self.assertRaises(ValueError):
                    run.submit(ident, wrong)
            run.submit(ident, envelope)
            sent += 1
        self.assertEqual(sent, 35)
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
