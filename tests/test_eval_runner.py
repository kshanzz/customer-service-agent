import subprocess
import sys
from pathlib import Path

from evals.run import run_eval


def test_eval_runner_all_scenarios_pass():
    report = run_eval(Path("evals/scenarios.json"))

    assert report["scenario_passed"] == report["scenario_total"]
    assert report["scenario_pass_rate"] == 1.0


def test_eval_runner_failure_returns_non_zero_exit_code(tmp_path: Path):
    failing = tmp_path / "failing_scenarios.json"
    failing.write_text(
        '[{"name": "failing", "turns": ["我要换货"], '
        '"interpreter_intents": [{"intent": "exchange", "order_id": "A1001", '
        '"reason": "x"}], '
        '"orders": {"A1001": {"product": "耳机", "status": "delivered", '
        '"days_since_delivery": 3}}, '
        '"expectations": {"final_status": "ready", '
        '"final_intent": "refund", "final_has_order": true, '
        '"final_has_exchange_request": false, "final_has_refund_request": false, '
        '"interpreter_calls": 1, "tool_calls": {"order_lookup": 1, '
        '"exchange_creator": 0, "refund_creator": 0}}}]',
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, "-m", "evals.run", "--scenarios", str(failing)],
        check=False,
        text=True,
    )

    assert completed.returncode == 1
