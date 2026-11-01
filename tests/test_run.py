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

from netexp import analysis, llm, observe, revote, run, sandbox, store

PROJECT = store.ROOT
ROLES = list(store.read_json(PROJECT / "prompts/roles.json"))


def fake_answer(message, tag="?"):
    """Корректный ответ на последнее сообщение среды, как дал бы послушный агент или судья."""
    if "The answer was not accepted" in message:
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


def temp_directory():
    """Временный каталог, доступный создавшему его процессу.

    Python 3.14 в Windows переводит режим 0o700 у каталога в ACL: доступ остаётся только
    у владельца. Под песочницей агента процесс работает с ограниченным токеном, и созданный
    им же каталог оказывается недоступен — даже на запись. Поэтому режим 0o777.
    """
    original = os.mkdir
    os.mkdir = lambda path, mode=0o777, *args, **kwargs: original(path, 0o777, *args, **kwargs)
    try:
        return tempfile.TemporaryDirectory()
    finally:
        os.mkdir = original


class RunTests(unittest.TestCase):
    def setUp(self):
        """Тексты проекта и синтетический ряд во временном корне."""
        directory = temp_directory()
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
        first, second = re.findall(r"organizer: (A\d+) or (A\d+)", question)[0]
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
        judges = [c for c in self.calls if c[0] == store.config()["judge_profile"]]
        self.assertEqual(len(agents), 5 * 6 + 5)   # раунды и итоги, без голосования
        self.assertEqual(len(judges), 5 * 5)
        self.assertEqual(len(self.choices), 5 * 6)  # попарные вопросы
        self.assertTrue(all(j["error"] is None for j in data["judgments"]))
        self.assertTrue(all(set(row) == {"accuracy", "judge_allocation"} for row in data["vote_tau"].values()))
        # История агента растёт, а меню ролей приходит в первом сообщении.
        last = agents[-1][1]
        self.assertEqual(last[0]["role"], "system")
        self.assertGreater(len(last), 10)
        self.assertIn("The list of roles", last[1]["content"])

    def test_judge_does_not_see_outcome_or_agents_see_judge(self):
        with patch.object(llm, "call", self.fake_call), patch.object(llm, "choice", self.fake_choice):
            ident = run.start("deepseek", "open")["id"]
            run.advance(ident)
        data = store.read_json(store.run_file("results", ident))
        for profile, messages in self.calls:
            if profile == store.config()["judge_profile"]:
                self.assertNotIn("Real values", messages[-1]["content"])
                self.assertNotIn("Organizer:", messages[-1]["content"])
        seen = json.dumps(data["histories"], ensure_ascii=False)
        self.assertNotIn("role_fit", seen)
        self.assertNotIn("Whom would you appoint", seen)  # вопросы голосования в истории не остаются

    def test_conditions_control_summary(self):
        with patch.object(llm, "call", self.fake_call), patch.object(llm, "choice", self.fake_choice):
            for condition, assignments in [("open", True), ("closed", False)]:
                ident = run.start("deepseek", condition)["id"]
                run.advance(ident)
                summary = store.read_json(store.run_file("results", ident))["rounds"][0]["summary"]
                self.assertEqual("Roles and tasks" in summary, assignments, condition)
                self.assertIn("Organizer's forecast", summary)
                self.assertIn("Real values", summary)
                self.assertNotIn("MASE", summary)

    def test_role_order_shuffled_per_run(self):
        orders = {tuple(run.spec("deepseek", "open", k)["role_order"]) for k in range(5)}
        self.assertGreater(len(orders), 1)
        self.assertEqual(run.spec("deepseek", "open", 0), run.spec("deepseek", "open", 0))

    def test_missing_closing_brace_repaired(self):
        """Потерянная скобка в конце ответа дописывается; ошибка внутри JSON по-прежнему отказ."""
        text = '{"assignments": {"A2": {"role": "historian", "task": "describe waves"}}'
        self.assertEqual(run.parse_json(text),
                         {"assignments": {"A2": {"role": "historian", "task": "describe waves"}}})
        with self.assertRaises(ValueError):
            run.parse_json('{"assignments": {"A2" {"role": "historian"}}')

    def test_role_outside_menu_rejected(self):
        bad = json.dumps({"assignments": {a: {"role": "econometrician", "task": "x"}
                                          for a in ("A2", "A3", "A4", "A5")}}, ensure_ascii=False)
        with patch.object(llm, "call", lambda p, s, m: bad):
            ident = run.start("deepseek", "closed")["id"]
            result = run.advance(ident)
        self.assertEqual(result["phase"], "failed")
        history = store.read_json(store.run_file("state", ident))["histories"]["A1"]
        self.assertIn("The answer was not accepted", history[1]["content"])
        # Некорректный ответ в историю не попадает: в ней только запросы среды.
        self.assertTrue(all(m["role"] == "user" for m in history))

    def test_task_length_limit(self):
        task = " ".join(["word"] * 61)
        bad = json.dumps({"assignments": {a: {"role": ROLES[0], "task": task}
                                          for a in ("A2", "A3", "A4", "A5")}}, ensure_ascii=False)
        with patch.object(llm, "call", lambda p, s, m: bad):
            ident = run.start("deepseek", "closed")["id"]
            result = run.advance(ident)
        self.assertEqual(result["phase"], "failed")
        history = store.read_json(store.run_file("state", ident))["histories"]["A1"]
        self.assertIn("longer than 60 words", history[-1]["content"])
        self.assertNotIn(bad, json.dumps(history, ensure_ascii=False))

    def test_worker_answer_length_limit(self):
        def long_answer(messages, tag="?"):
            if '{"assignments"' in messages[-1]["content"]:
                return fake_answer(messages[-1]["content"], tag)
            return " ".join(["word"] * 151)
        with patch.object(llm, "call", lambda p, s, m: long_answer(m, s)):
            ident = run.start("deepseek", "closed")["id"]
            result = run.advance(ident)
        self.assertEqual(result["phase"], "failed")
        state = store.read_json(store.run_file("state", ident))
        self.assertEqual(state["rounds"][0]["answers"], {})
        self.assertIn("longer than 150 words", state["histories"]["A2"][-1]["content"])

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
                    self.assertNotIn("You are agent", request["message"])
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

    def test_results_include_observables(self):
        with patch.object(llm, "call", self.fake_call), patch.object(llm, "choice", self.fake_choice):
            ident = run.start("deepseek", "open")["id"]
            run.advance(ident)
        observed = store.read_json(store.run_file("results", ident))["observables"]
        self.assertEqual(sum(observed["role_matrix"]["A1"].values()), 4)  # A1 был организатором в раунде 1
        self.assertEqual(len(observed["conventions"]), 5)
        self.assertEqual(observed["conventions"][0], 0.0)
        self.assertTrue(all(0 <= value <= 1 for value in observed["specialization"].values()))
        self.assertEqual(sum(sum(row.values()) for row in observed["links"].values()), 20)

    def test_local_profile_and_logprobs(self):
        profile = store.profile("local_yandex")
        self.assertEqual(profile["engine"], "local")
        base_url, api_key = llm.endpoint(store.config()["engines"]["local"])
        self.assertTrue(base_url.startswith("http://localhost"))
        self.assertEqual(api_key, "local")  # ключ локальному серверу не нужен
        self.assertIsNone(llm.cached_logprobs("нет такого запроса"))
        token = lambda name, lp, top: SimpleNamespace(token=name, logprob=lp, top_logprobs=top)
        choice = SimpleNamespace(logprobs=SimpleNamespace(content=[
            token("{'forecast'", -0.1, []), token(":", -0.2, []),
            token(" 10", -0.5, [token(" 12", -1.2, [])]), token(" 9", -0.3, [])]))
        summary = llm.number_logprobs(choice)
        self.assertEqual(summary["n_tokens"], 4)
        self.assertEqual([n["token"] for n in summary["numbers"]], [" 10", " 9"])
        self.assertEqual(summary["numbers"][0]["top"][0]["token"], " 12")
        self.assertIsNone(llm.number_logprobs(SimpleNamespace(logprobs=None)))

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

    def test_solo_control_run(self):
        """Контроль solo: та же ротация и окна, но без ролей, общения, голосования и судьи."""
        with patch.object(llm, "call", self.fake_call), patch.object(llm, "choice", self.fake_choice):
            ident = run.start("deepseek", "solo", 1)["id"]
            result = run.advance(ident)
        self.assertTrue(result["done"])
        data = store.read_json(self.root / result["result_file"])
        self.assertEqual(data["status"], "done")
        # Та же ротация, что и в групповом прогоне: повтор 1 начинает A2.
        self.assertEqual([r["organizer"] for r in data["rounds"]], ["A2", "A3", "A4", "A5", "A1"])
        self.assertTrue(all(r["assignments"] == {} and r["mase"] is not None for r in data["rounds"]))
        self.assertEqual((data["votes"], data["judgments"], data["debriefs"]), ({}, [], {}))
        agents = [c for c in self.calls if c[0] == "deepseek"]
        self.assertEqual(len(agents), 5)                    # по одному прогнозу на раунд
        self.assertEqual([c for c in self.calls if c[0] == store.config()["judge_profile"]], [])
        self.assertEqual(self.choices, [])                  # голосования нет
        for profile, messages in agents:
            self.assertIn("on their own", messages[0]["content"])   # системный промпт solo
            self.assertNotIn("The list of roles", json.dumps(messages, ensure_ascii=False))

    def test_solo_summary_stays_with_forecaster(self):
        """Итоги раунда в solo видит только прогнозист; у каждого — одно задание и одни итоги."""
        with patch.object(llm, "call", self.fake_call), patch.object(llm, "choice", self.fake_choice):
            ident = run.start("deepseek", "solo")["id"]
            run.advance(ident)
        state = store.read_json(store.run_file("state", ident))
        for item in state["rounds"]:
            readers = {a for a, h in state["histories"].items()
                       if any(m["content"] == item["summary"] for m in h if m["role"] == "user")}
            self.assertEqual(readers, {item["organizer"]})
        # задание, прогноз, итоги — и ничего больше; меню ролей не приходит
        self.assertTrue(all(len(h) == 3 for h in state["histories"].values()))
        self.assertTrue(all("The list of roles" not in m["content"] for h in state["histories"].values()
                            for m in h))

    def test_solo_spec_has_no_vote_and_judge(self):
        """В solo логпробы и судья не нужны: подходит любой эндпоинт, включая субагентов."""
        self.assertIsNone(run.spec("deepseek", "solo")["vote"])
        self.assertIsNone(run.spec("deepseek", "solo")["judge"])
        run.start("sonnet_sub", "solo")  # не должно падать без vote_profile

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


class ProtocolTests(unittest.TestCase):
    """Хеш протокола не зависит от того, с какими переводами строк файлы лежат на диске."""

    def setUp(self):
        directory = temp_directory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copytree(PROJECT / "prompts", self.root / "prompts")
        cfg = store.read_json(PROJECT / "config.json")
        cfg["data"] = "data/series.csv"
        (self.root / "data").mkdir()
        (self.root / "data/series.csv").write_text("week,value\n0,1\n", encoding="utf-8")
        (self.root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        context = patch.object(store, "ROOT", self.root)
        context.start()
        self.addCleanup(context.stop)

    def test_line_endings_do_not_change_digest(self):
        """core.autocrlf переписывает файлы при checkout; текст для агентов тот же."""
        digest = store.protocol_digest()
        for path in [*self.root.glob("prompts/*.txt"), self.root / "prompts/summary.json",
                     self.root / "prompts/roles.json", self.root / "data/series.csv"]:
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.assertEqual(store.protocol_digest(), digest)

    def test_text_change_does_change_digest(self):
        """Нормализация не должна скрывать настоящую правку протокола."""
        digest = store.protocol_digest()
        prompt = self.root / "prompts/pair.txt"
        prompt.write_bytes(prompt.read_bytes() + b"\nOne more line.")
        self.assertNotEqual(store.protocol_digest(), digest)


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
        directory = temp_directory()
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
        self.messages = [{"role": "user", "content": "Appoint A1 or A2? Answer with one digit: 1 — A1, 2 — A2."}]

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
        self.assertEqual(kwargs["max_tokens"], 2)
        self.assertTrue(kwargs["logprobs"])
        self.assertEqual(kwargs["top_logprobs"], 20)
        # Тот же запрос не ходит в сеть дважды
        llm.choice(self.profile, "s1", self.messages, ("1", "2"))
        self.assertEqual(len(LlmChoiceTests.FakeOpenAI.calls), 1)

    def test_leading_space_token_skipped(self):
        """Шаблон YandexGPT начинает ответ отдельным пробелом: цифра приходит вторым токеном."""
        space = SimpleNamespace(token=" ", logprob=0.0,
                                top_logprobs=[SimpleNamespace(token=" ", logprob=0.0)])
        digit = SimpleNamespace(token="1", logprob=-0.2,
                                top_logprobs=[SimpleNamespace(token="1", logprob=-0.2),
                                              SimpleNamespace(token="2", logprob=-1.7)])
        LlmChoiceTests.FakeOpenAI.responses = [SimpleNamespace(
            choices=[SimpleNamespace(logprobs=SimpleNamespace(content=[space, digit]))], usage=None)]
        values = llm.choice(self.profile, "s0", self.messages, ("1", "2"))
        self.assertEqual(values, {"1": -0.2, "2": -1.7})

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

    @staticmethod
    def ballot_response(content, tokens):
        """Ответ с JSON-текстом и токенами: (токен, логпроб, топ-логпробы)."""
        entry = [SimpleNamespace(token=token, logprob=logprob,
                                 top_logprobs=[SimpleNamespace(token=t, logprob=v) for t, v in top])
                 for token, logprob, top in tokens]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                                        logprobs=SimpleNamespace(content=entry))],
                               usage=None)

    def test_ballot_keeps_reason_and_choice_margin(self):
        """Цифра выбора стоит не первым токеном: логпроб берётся с позиции поля choice."""
        content = '{"choice": 2, "reason": "A2 kept the forecast close to the series"}'
        tokens = [('{"choice": ', -0.0, []),
                  ("2", -0.3, [("1", -1.4), ("2", -0.3)]),
                  (', "reason": "A2 kept the forecast close to the series"}', 0.0, [])]
        LlmChoiceTests.FakeOpenAI.responses = [self.ballot_response(content, tokens)]
        value = llm.ballot(self.profile, "b1", self.messages)
        self.assertEqual(value["content"], content)
        self.assertAlmostEqual(value["logprob"]["2"], -0.3)
        self.assertAlmostEqual(value["logprob"]["1"], -1.4)
        kwargs = LlmChoiceTests.FakeOpenAI.calls[0]
        self.assertEqual(kwargs["response_format"]["json_schema"]["strict"], True)
        self.assertTrue(kwargs["logprobs"])

    def test_ballot_without_logprobs_keeps_choice(self):
        """Сервер может не отдать логиты вместе со структурированным ответом: выбор и причина остаются."""
        content = '{"choice": 1, "reason": "x"}'
        LlmChoiceTests.FakeOpenAI.responses = [SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), logprobs=None)], usage=None)]
        value = llm.ballot(self.profile, "b2", self.messages)
        self.assertEqual(value["content"], content)
        self.assertEqual(value["logprob"], {"1": None, "2": None})


class RevoteTests(unittest.TestCase):
    """Повторное голосование спрашивает то же, что и прогон, но требует причину."""

    def test_question_keeps_pair_text_and_number_mapping(self):
        paragraphs = (store.ROOT / "prompts/pair.txt").read_text(encoding="utf-8").strip().split("\n\n")
        prompt = revote.question("A2", "A4")
        self.assertTrue(prompt.startswith(paragraphs[0]))
        self.assertIn(paragraphs[1].format(first="A2", second="A4"), prompt)
        self.assertIn("1 if A2", prompt)
        self.assertIn("2 if A4", prompt)
        self.assertNotIn("single digit", prompt)
        self.assertIn('"reason"', prompt)

    def test_parse_rejects_missing_choice_or_reason(self):
        with self.assertRaises(ValueError):
            revote.parse({"content": '{"reason": "x"}', "logprob": {}})
        with self.assertRaises(ValueError):
            revote.parse({"content": '{"choice": 3, "reason": "x"}', "logprob": {}})
        with self.assertRaises(ValueError):
            revote.parse({"content": '{"choice": 1, "reason": " "}', "logprob": {}})
        self.assertEqual(revote.parse({"content": '{"choice": 2, "reason": "x"}', "logprob": {}}),
                         {"choice": 2, "reason": "x"})

    def test_parse_accepts_digit_in_quotes(self):
        """Цифра в кавычках — тот же выбор: смысл однозначен, повтор запроса не нужен."""
        self.assertEqual(revote.parse({"content": '{"choice": "1", "reason": "x"}', "logprob": {}})["choice"], 1)

    def test_clarified_question_names_both_agents(self):
        """Уточнение на повторе: в паре ровно двое, другого ответа нет — лечит «выбор 3»."""
        plain = revote.question("A1", "A3")
        clarified = revote.question("A1", "A3", clarified=True)
        self.assertTrue(clarified.startswith(plain))
        self.assertIn("exactly two agents: A1 and A3", clarified)
        self.assertIn("Answer 1 to make A1", clarified)
        self.assertIn("2 to make A3 the organizer", clarified)

    def test_flipped_counts_reversed_preference(self):
        """Знак прежнего перевеса против нового выбора: смена предпочтения — перевёрнутая пара."""
        old = {"A1": [{"pair": ["A2", "A3"], "one": "A2", "logprob": {"A2": -0.9, "A3": -0.6}}]}
        reversed_ballot = {"agent": "A1", "pair": ["A2", "A3"], "one": "A2", "two": "A3", "choice": 1,
                           "picked": "A2", "reason": "x", "logprob": {"1": -0.3, "2": -1.4}}
        self.assertEqual(revote.flipped(old, {"A1": [reversed_ballot]}), (1, 1))
        kept_ballot = {"agent": "A1", "pair": ["A2", "A3"], "one": "A2", "two": "A3", "choice": 2,
                       "picked": "A3", "reason": "x", "logprob": {"1": -1.4, "2": -0.3}}
        self.assertEqual(revote.flipped(old, {"A1": [kept_ballot]}), (0, 1))

    def test_flipped_needs_both_margins(self):
        """Прежние голоса-ранжирование без перевесов сравнивать не с чем — и пары не считаются."""
        self.assertFalse(revote.logprob_votes({"A1": [2, 3, 4, 5]}))
        self.assertTrue(revote.logprob_votes(
            {"A1": [{"pair": ["A2", "A3"], "one": "A2", "logprob": {"A2": -0.9, "A3": -0.6}}]}))

    def test_count_word_agrees_with_number(self):
        self.assertEqual(revote.count_word(1, "голос", "голоса", "голосов"), "1 голос")
        self.assertEqual(revote.count_word(3, "голос", "голоса", "голосов"), "3 голоса")
        self.assertEqual(revote.count_word(11, "голос", "голоса", "голосов"), "11 голосов")
        self.assertEqual(revote.count_word(21, "голос", "голоса", "голосов"), "21 голос")
        self.assertEqual(revote.count_word(30, "голос", "голоса", "голосов"), "30 голосов")

    def test_agree_block_counts_picks_with_margins(self):
        """Выбор сверяется с перевесами цифр: ответ без перевесов не считается, расхождение видно."""
        ballots = [
            {"agent": "A1", "pair": ["A2", "A3"], "one": "A2", "two": "A3", "choice": 1, "picked": "A2",
             "reason": "x", "logprob": {"1": -0.3, "2": -1.4}},
            {"agent": "A2", "pair": ["A1", "A3"], "one": "A3", "two": "A1", "choice": 1, "picked": "A3",
             "reason": "y", "logprob": {"1": -2.0, "2": -0.1}},
            {"agent": "A3", "pair": ["A1", "A2"], "one": "A1", "two": "A2", "choice": 1, "picked": "A1",
             "reason": "z", "logprob": {"1": None, "2": None}},
        ]
        text = "\n".join(revote.agree_block(ballots))
        self.assertIn("Из 3 ответов перевесы цифр вернулись в 2", text)
        self.assertIn("совпала с более вероятной в 1 из 2", text)
        self.assertIn("50 %", text)

    def test_run_block_marks_ranking_votes_without_margins(self):
        """У прогона с ранжированием нет перевесов: в отчёте прочерк, а не нули."""
        state = {"spec": {"id": "abc12345", "condition": "open", "repeat": 0}, "votes": {"A1": [2, 3, 4, 5]}}
        ballots = [{"agent": "A1", "pair": ["A2", "A3"], "one": "A2", "two": "A3", "choice": 1,
                    "picked": "A2", "reason": "x", "logprob": {"1": -0.3, "2": -1.4}}]
        text = "\n".join(revote.run_block(state, ballots))
        self.assertIn("Прогон abc12345", text)
        self.assertIn("Собрано 1 голос из 30", text)
        self.assertIn("| — | — |", text)
        self.assertIn("сравнивать порядок не с чем", text)


def synthetic_rounds(fixed):
    """Пять раундов: роли закреплены за агентами или уходят от них циклическим сдвигом."""
    roles = {"A1": "modeler", "A2": "statistician", "A3": "scenarist", "A4": "critic", "A5": "historian"}
    rounds = []
    for index in range(5):
        organizer = f"A{index + 1}"
        workers = [a for a in ("A1", "A2", "A3", "A4", "A5") if a != organizer]
        values = [roles[a] for a in workers]
        assignment = {a: roles[a] for a in workers} if fixed else \
            {a: values[(i + index + 1) % len(values)] for i, a in enumerate(workers)}
        rounds.append({"organizer": organizer, "origin": 0,
                       "assignments": {a: {"role": r, "task": ""} for a, r in assignment.items()},
                       "answers": {}, "mase": 0.5, "forecast": [21, 22, 23, 24],
                       "actual": [20, 22, 24, 26]})
    return rounds


def synthetic_solo_rounds(mase):
    """Раунды контроля solo: та же ротация, без ролей, заданная ошибка."""
    return [{"organizer": f"A{index + 1}", "origin": 0, "assignments": {}, "answers": {},
             "mase": mase, "forecast": [22, 23, 24, 25], "actual": [20, 22, 24, 26]}
            for index in range(5)]


def rounds_with_sets(sets):
    """Раунды с заданным составом ролей в каждом; центры — по ротации."""
    rounds = []
    for index, current in enumerate(sets):
        organizer = f"A{index + 1}"
        workers = [a for a in ("A1", "A2", "A3", "A4", "A5") if a != organizer]
        roles_list = list(current)
        rounds.append({"organizer": organizer, "origin": 0,
                       "assignments": {a: {"role": roles_list[i % len(roles_list)], "task": ""}
                                       for i, a in enumerate(workers)},
                       "answers": {}, "mase": 0.5, "forecast": [], "actual": []})
    return rounds


def ballot(x, y, value):
    """Пара с перевесом value в пользу x (порядок пары канонический)."""
    return {"pair": [x, y], "one": x, "logprob": {x: value / 2, y: -value / 2}}


def linear_ballots(others):
    """Попарные голоса агента, предпочитающего меньшие имена."""
    return [ballot(x, y, 1.0) for x, y in run.pairs(others)]


class AnalysisTests(unittest.TestCase):
    """Статистики анализа на синтетических прогонах без запросов к моделям."""

    def setUp(self):
        directory = temp_directory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copytree(PROJECT / "prompts", self.root / "prompts")
        self.root.joinpath("runs/results").mkdir(parents=True)
        self.root.joinpath("runs/state").mkdir(parents=True)
        self.root.joinpath("data").mkdir()
        rows = ["week,value"] + [f"{i},{20 + 5 * math.sin(i / 3):.1f}" for i in range(80)]
        self.root.joinpath("data/test.csv").write_text("\n".join(rows), encoding="utf-8")
        cfg = store.read_json(PROJECT / "config.json")
        cfg["data"] = "data/test.csv"
        (self.root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        context = patch.object(store, "ROOT", self.root)
        context.start()
        self.addCleanup(context.stop)

    def write_run(self, ident, condition, votes, fixed=True, status="done", judgments=(), rounds=None, repeat=0,
                  protocol="testbench", model="synthetic"):
        data = {"spec": {"id": ident, "condition": condition, "repeat": repeat, "protocol": protocol,
                         "profile": {"engine": "local", "model": model}, "role_order": ROLES},
                "status": status,
                "rounds": rounds if rounds is not None else synthetic_rounds(fixed),
                "votes": votes, "judgments": list(judgments)}
        store.write_json(store.run_file("results", ident), data)
        return data

    def test_runs_include_incomplete_states(self):
        """Незавершённый прогон с полными голосами попадает в анализ."""
        self.write_run("a" * 24, "open", {})
        votes = {v: {"ranking": [a for a in run.names() if a != v], "reason": ""}
                 for v in run.names()}
        state = {"spec": {"id": "b" * 24, "condition": "closed", "repeat": 0}, "phase": "judge",
                 "rounds": synthetic_rounds(True), "votes": votes, "judgments": []}
        store.write_json(store.run_file("state", "b" * 24), state)
        found = analysis.runs()
        self.assertEqual({d["spec"]["id"] for d in found}, {"a" * 24, "b" * 24})
        self.assertEqual(analysis.status(state), "остановлен на судье")
        self.assertEqual(analysis.vote_kind(state), "ранжирование")

    def test_fixation_above_chance(self):
        rounds = synthetic_rounds(fixed=True)
        value = analysis.fixation(rounds)
        self.assertEqual(value["observed"], value["pairs"])  # роли закреплены намертво
        self.assertLess(value["p"], 0.001)
        shuffled = analysis.fixation(synthetic_rounds(fixed=False))
        self.assertLess(shuffled["observed"], value["observed"])
        self.assertGreater(shuffled["p"], value["p"])

    def test_convention_against_independent_choice(self):
        one, two = frozenset(ROLES[:4]), frozenset(ROLES[1:5])
        # один и тот же состав во всех раундах: повтор неотличим от общего предпочтения
        value = analysis.convention(rounds_with_sets([one] * 5), [one] * 5)
        self.assertEqual((value["observed"], value["transitions"]), (4, 4))
        self.assertEqual(value["expected"], 4.0)
        # чередование двух составов: повторов меньше, чем при независимом выборе
        alternating = [one, two, one, two, one]
        value = analysis.convention(rounds_with_sets(alternating), alternating)
        self.assertEqual(value["observed"], 0)
        self.assertGreater(value["expected"], 1.5)

    def test_reciprocity(self):
        """Взаимность делегирования: согласованность взаимных мест в ранжированиях."""
        agents = run.names()
        # все предпочитают меньшие имена: оценки согласованы, τ положителен
        linear = {v: linear_ballots([a for a in agents if a != v]) for v in agents}
        self.assertAlmostEqual(analysis.reciprocity(linear), 1 / 3)
        # цикл предпочтений: чем выше i ставит j, тем ниже j ставит i
        cyclic = {v: {"ranking": [agents[(i + t) % 5] for t in range(1, 5)], "reason": ""}
                 for i, v in enumerate(agents)}
        self.assertAlmostEqual(analysis.reciprocity(cyclic), -7 / 9)

    def test_linearity(self):
        agents = ["A1", "A2", "A3", "A4", "A5"]
        order = {a: i for i, a in enumerate(agents)}
        wins = {a: sum(1 for b in agents if b != a and order[a] < order[b]) for a in agents}
        self.assertEqual(analysis.linearity(wins), 1.0)
        cycle = {"A1": 3, "A2": 3, "A3": 3, "A4": 1, "A5": 0}  # тройка A1–A2–A3 идёт по кругу
        self.assertLess(analysis.linearity(cycle), 1.0)

    def test_majority_and_transitivity(self):
        agents, votes = run.names(), {}
        for voter in agents:
            votes[voter] = linear_ballots([a for a in agents if a != voter])
        wins, scores, undecided = analysis.majority(votes, agents)
        self.assertEqual(wins, {"A1": 4, "A2": 3, "A3": 2, "A4": 1, "A5": 0})
        self.assertTrue(all(max(s) == 3 for s in scores.values()))
        self.assertEqual(undecided, 0)
        self.assertEqual(analysis.transitivity(votes["A1"]), 1.0)
        # цикл A2>A3>A4>A2 ломает одну тройку из четырёх
        cycle = [b for b in votes["A1"]]
        for b in cycle:
            if b["pair"] == ["A2", "A4"]:
                b["logprob"] = {"A2": -0.5, "A4": 0.5}
        self.assertAlmostEqual(analysis.transitivity(cycle), 0.75)

    def test_report_written_and_sounded(self):
        votes = {v: linear_ballots([a for a in run.names() if a != v]) for v in run.names()}
        self.write_run("c" * 24, "open", votes, fixed=True)
        markdown = analysis.report()
        self.assertIn("## Условие `open`", markdown)
        self.assertIn("Конвенция состава ролей", markdown)
        self.assertIn("Закрепление ролей", markdown)
        self.assertIn("Голосование (делегирование)", markdown)
        self.assertIn("A1 > A2 > A3 > A4 > A5", markdown)
        self.assertIn("Взаимность", markdown)
        self.assertIn("literature-review.up.railway.app", markdown)
        self.assertNotIn("None", markdown)
        # Отчёт воспроизводим: фиксированное зерно
        self.assertEqual(analysis.report(), markdown)

    def test_delta_q_against_solo(self):
        """ΔQ: пары группы и одиночки на общих окнах, медианный ансамбль, знаковый тест."""
        votes = {v: linear_ballots([a for a in run.names() if a != v]) for v in run.names()}
        self.write_run("d" * 24, "open", votes)                     # MASE 0.5 во всех раундах
        self.write_run("e" * 24, "solo", {}, rounds=synthetic_solo_rounds(0.8))
        self.write_run("f" * 24, "solo", {}, rounds=synthetic_solo_rounds(0.8), repeat=1)
        markdown = analysis.report()
        self.assertIn("## Контроль `solo`", markdown)
        self.assertIn("Коллективный выигрыш", markdown)
        self.assertIn("+0.30", markdown)          # ΔQ = 0.8 − 0.5 в каждой паре
        self.assertIn("группа лучше в 5 из 5 пар", markdown)
        self.assertIn("p = 0.062", markdown)      # двусторонний точный знаковый тест: 2 × 2⁻⁵
        self.assertIn("Независимый ансамбль", markdown)
        self.assertIn("2, ", markdown)           # ансамбль одиночек из двух прогонов
        self.assertNotIn("None", markdown)

    def test_report_keeps_series_apart(self):
        """Другая модель или другой протокол — отдельная серия, а не общая статистика."""
        votes = {v: linear_ballots([a for a in run.names() if a != v]) for v in run.names()}
        self.write_run("1" * 24, "open", votes, model="first")
        self.write_run("2" * 24, "open", votes, model="second")
        groups = analysis.by_series(analysis.runs())
        self.assertEqual([len(subset) for _, subset in groups], [1, 1])
        markdown = analysis.report()
        self.assertIn("в сериях: 2", markdown)
        self.assertEqual(markdown.count("Коллективный выигрыш (ΔQ)"), 2)  # своя секция на серию
        self.assertIn("| first |", markdown)
        self.assertIn("| second |", markdown)


class ObserveTests(unittest.TestCase):
    def test_specialization_links_conventions(self):
        rounds = [
            {"organizer": "A1", "assignments": {"A2": {"role": "statistician"}, "A3": {"role": "historian"},
                                                 "A4": {"role": "critic"}, "A5": {"role": "calendar"}}},
            {"organizer": "A2", "assignments": {"A1": {"role": "statistician"}, "A3": {"role": "historian"},
                                                 "A4": {"role": "modeler"}, "A5": {"role": "calendar"}}},
        ]
        self.assertEqual(observe.specialization(rounds)["A3"], 1.0)  # обе роли историка
        self.assertEqual(observe.specialization(rounds)["A4"], 0.5)  # критик и моделист
        self.assertEqual(observe.links(rounds)["A1"]["A2"], 1)
        self.assertEqual(observe.conventions(rounds), [0.0, 0.5])  # повторились историк и календарь

    def test_empty_rounds(self):
        self.assertEqual(observe.role_matrix([]), {})
        self.assertEqual(observe.specialization([]), {})
        self.assertEqual(observe.links([]), {})
        self.assertEqual(observe.conventions([]), [])


if __name__ == "__main__":
    unittest.main()
