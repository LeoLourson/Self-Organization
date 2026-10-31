"""Проверки без платных вызовов: python -m unittest discover tests -v"""
import json
import math
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        env = patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test", "ANTHROPIC_API_KEY": "test"})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        self.choices = []

    def fake_call(self, profile, scope, messages):
        self.calls.append((profile["id"], messages))
        return fake_answer(messages[-1]["content"], scope)

    def fake_choice(self, profile, scope, messages, candidates):
        """Попарный вопрос: перевес в пользу агента с меньшим именем."""
        question = messages[-1]["content"]
        first, second = re.findall(r"организатором: (A\d+) или (A\d+)", question)[0]
        strong, weak = sorted([first, second])
        values = {"1": -0.5 if first == strong else -1.5, "2": -1.5 if first == strong else -0.5}
        self.choices.append((profile["id"], first, second))
        return values

    def test_api_run_reaches_judge(self):
        with patch.object(llm, "call", self.fake_call), patch.object(llm, "choice", self.fake_choice):
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
        self.assertEqual(len(agents), 5 * 6 + 5)   # раунды и итоги, без голосования
        self.assertEqual(len(judges), 5 * 5)
        self.assertEqual(len(self.choices), 5 * 6)  # попарные вопросы
        self.assertTrue(all(j["error"] is None for j in data["judgments"]))
        self.assertTrue(all(set(row) == {"accuracy", "judge_allocation"} for row in data["vote_tau"].values()))
        # История агента растёт, а меню ролей приходит в первом сообщении.
        last = agents[-1][1]
        self.assertEqual(last[0]["role"], "system")
        self.assertGreater(len(last), 10)
        self.assertIn("Список ролей", last[1]["content"])

    def test_judge_does_not_see_outcome_or_agents_see_judge(self):
        with patch.object(llm, "call", self.fake_call), patch.object(llm, "choice", self.fake_choice):
            ident = run.start("deepseek", "open")["id"]
            run.advance(ident)
        data = store.read_json(store.run_file("results", ident))
        for profile, messages in self.calls:
            if profile == "deepseek_pro":
                self.assertNotIn("Реальные значения", messages[-1]["content"])
                self.assertNotIn("Организатор:", messages[-1]["content"])
        seen = json.dumps(data["histories"], ensure_ascii=False)
        self.assertNotIn("role_fit", seen)
        self.assertNotIn("Кого ты назначил", seen)  # вопросы голосования в истории не остаются

    def test_conditions_control_summary(self):
        with patch.object(llm, "call", self.fake_call), patch.object(llm, "choice", self.fake_choice):
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
        with patch.object(llm, "call", self.fake_call), patch.object(llm, "choice", self.fake_choice):
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
        with patch.object(llm, "call", self.fake_call), patch.object(llm, "choice", self.fake_choice):
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
        # Голосование и судья идут через API и субагентов не запрашивают.
        self.assertEqual(sent, 5 * 6 + 5)
        self.assertEqual(len(self.calls), 25)   # только судья
        self.assertEqual(len(self.choices), 30)  # голоса меряет sonnet_api из vote_profile
        self.assertEqual({c[0] for c in self.choices}, {"sonnet_api"})
        data = store.read_json(store.run_file("results", ident))
        self.assertEqual(data["sessions"], {a: f"sub-{a}" for a in run.names()})

    def test_kendall_tau(self):
        self.assertEqual(run.kendall_tau(["a", "b", "c"], ["a", "b", "c"]), 1)
        self.assertEqual(run.kendall_tau(["c", "b", "a"], ["a", "b", "c"]), -1)

    def test_vote_measured_by_logprobs(self):
        """Голосование: по шесть пар на агента, с перевесами и чередованием порядка."""
        with patch.object(llm, "call", self.fake_call), patch.object(llm, "choice", self.fake_choice):
            ident = run.start("deepseek", "open")["id"]
            run.advance(ident)
        data = store.read_json(store.run_file("results", ident))
        others = lambda a: [x for x in run.names() if x != a]
        for voter, ballots in data["votes"].items():
            self.assertEqual(sorted(tuple(b["pair"]) for b in ballots),
                             sorted(run.pairs(others(voter))))
            for ballot in ballots:
                self.assertIn(ballot["one"], ballot["pair"])   # порядок записан верно
                x, y = ballot["pair"]
                self.assertEqual(set(ballot["logprob"]), set(ballot["pair"]))
                # фейк предпочитает меньшее имя: перевес положителен в его сторону
                self.assertGreater(ballot["logprob"][min(x, y)] - ballot["logprob"][max(x, y)], 0)
        # Позиция «1» достаётся обоим порядкам пары
        orientations = {(tuple(b["pair"]), b["one"]) for b in
                        (bb for bl in data["votes"].values() for bb in bl)}
        self.assertGreater(len(orientations), len({p for p, _ in orientations}))
        # Ранжирование строится по сумме перевесов: меньшие имена выше
        self.assertEqual(data["vote_rankings"]["A5"], ["A1", "A2", "A3", "A4"])
        self.assertEqual(data["vote_rankings"]["A1"], ["A2", "A3", "A4", "A5"])

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


class LlmChoiceTests(unittest.TestCase):
    """Разбор логпробов ответа: llm.choice без сети, с фальшивым OpenAI-клиентом."""

    class FakeOpenAI:
        calls, responses = [], []

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            def create(**kwargs):
                LlmChoiceTests.FakeOpenAI.calls.append(kwargs)
                return LlmChoiceTests.FakeOpenAI.responses[len(LlmChoiceTests.FakeOpenAI.calls) - 1]
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
            return self

        def __exit__(self, *args):
            return False

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copytree(PROJECT / "prompts", self.root / "prompts")
        self.root.joinpath("data").mkdir()
        rows = ["week,value"] + [f"{i},{20 + 5 * math.sin(i / 3):.1f}" for i in range(80)]
        self.root.joinpath("data/test.csv").write_text("\n".join(rows), encoding="utf-8")
        cfg = store.read_json(PROJECT / "config.json")
        cfg["data"] = "data/test.csv"
        (self.root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        context = patch.object(store, "ROOT", self.root)
        context.start()
        self.addCleanup(context.stop)
        env = patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"})
        env.start()
        self.addCleanup(env.stop)
        LlmChoiceTests.FakeOpenAI.calls, LlmChoiceTests.FakeOpenAI.responses = [], []
        self.client = patch("openai.OpenAI", self.FakeOpenAI)
        self.client.start()
        self.addCleanup(self.client.stop)
        self.profile = store.profile("deepseek")
        self.messages = [{"role": "user", "content": "Кого назначить: A1 или A2? Ответь одной цифрой: 1 — A1, 2 — A2."}]

    @staticmethod
    def logprob_response(token, top):
        """Ответ API: топ-логпробы первой позиции, top — список пар (токен, логпроб)."""
        entry = SimpleNamespace(token=token, logprob=dict(top).get(token, -99.0),
                                top_logprobs=[SimpleNamespace(token=t, logprob=v) for t, v in top])
        return SimpleNamespace(choices=[SimpleNamespace(logprobs=SimpleNamespace(content=[entry]))],
                               usage=None)

    def test_both_candidates_found(self):
        top = [("1", -0.2), ("2", -1.7), (" 1", -3.0)]
        LlmChoiceTests.FakeOpenAI.responses = [self.logprob_response("1", top)]
        values = llm.choice(self.profile, "s1", self.messages, ("1", "2"))
        self.assertAlmostEqual(values["1"], -0.2 + math.log(1 + math.exp(-2.8)))  # logsumexp(-0.2, -3.0)
        self.assertEqual(values["2"], -1.7)
        kwargs = LlmChoiceTests.FakeOpenAI.calls[0]
        self.assertEqual(kwargs["max_tokens"], 1)
        self.assertTrue(kwargs["logprobs"])
        self.assertEqual(kwargs["top_logprobs"], 20)
        # Тот же запрос не ходит в сеть дважды
        llm.choice(self.profile, "s1", self.messages, ("1", "2"))
        self.assertEqual(len(LlmChoiceTests.FakeOpenAI.calls), 1)

    def test_candidate_outside_top_is_none(self):
        top = [("1", -0.1), ("да", -5.0)]
        LlmChoiceTests.FakeOpenAI.responses = [self.logprob_response("1", top)]
        values = llm.choice(self.profile, "s2", self.messages, ("1", "2"))
        self.assertEqual(values["1"], -0.1)
        self.assertIsNone(values["2"])

    def test_missing_logprobs_rejected(self):
        LlmChoiceTests.FakeOpenAI.responses = [SimpleNamespace(
            choices=[SimpleNamespace(logprobs=None)], usage=None)]
        with self.assertRaises(ValueError):
            llm.choice(self.profile, "s3", self.messages, ("1", "2"))
        # Ничего не закэшировано: повтор делает новый запрос
        LlmChoiceTests.FakeOpenAI.calls = []
        LlmChoiceTests.FakeOpenAI.responses = [self.logprob_response("2", [("1", -1.0), ("2", -0.3)])]
        values = llm.choice(self.profile, "s3", self.messages, ("1", "2"))
        self.assertEqual(values["2"], -0.3)


if __name__ == "__main__":
    unittest.main()
