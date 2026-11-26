"""The eval grader itself must be right: a perfect answer passes, a wrong one fails."""
import json
import sys
from pathlib import Path

from atlas.understand import ClaudeParser
from test_stage1 import FakeClient, item

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import run_eval  # noqa: E402

CASES = {c["id"]: c for c in map(json.loads, (Path(run_eval.ROOT) / "evals" / "phrases.jsonl")
                                 .read_text("utf-8").splitlines()) if c}


def run(case_id, items):
    return run_eval.run_case(CASES[case_id], ClaudeParser(client=FakeClient(items)))[0]


def test_all_cases_load_and_have_unique_ids():
    lines = [l for l in (Path(run_eval.ROOT) / "evals" / "phrases.jsonl").read_text("utf-8").splitlines() if l]
    assert len(lines) == len(CASES) >= 40


def test_perfect_answer_passes():
    text = CASES["roberto-3-tasks"]["text"]
    assert "на завтра підготувати місце біля рампи 31" in text
    assert run("roberto-3-tasks", [
        item("new_loop", "на завтра підготувати місце біля рампи 31", title="Підготувати місце біля рампи 31",
             due="tomorrow", assigned_by="Роберто Полі"),
        item("new_loop", "в суботу звільнити зону 2O 06", title="Звільнити зону 2O 06",
             due="weekday:sat", assigned_by="Роберто Полі"),
        item("new_loop", "на наступний тиждень перевірити етикетки 3Q", title="Перевірити етикетки 3Q",
             due="next_week", assigned_by="Роберто Полі"),
    ]) == []
    assert run("done-claim", [item("change_state", "Зону 2O 06 звільнили", loop_id=1,
                                   state="done_claimed")]) == []
    assert run("done-confirm", [item("confirm_done", "Так", loop_id=1)]) == []


def test_wrong_answer_fails():
    errs = run("roberto-3-tasks", [
        item("new_loop", "в суботу звільнити зону 2O 06", title="Звільнити зону 2O 06",
             due="tomorrow", assigned_by="Роберто Полі")])
    assert len(errs) == 3
    assert run("greeting", [item("idea", "Привіт", title="Привітатися")])
