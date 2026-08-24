from __future__ import annotations

from argparse import Namespace, ArgumentParser
import json
from pathlib import Path
from typing import Any
import sys
from collections.abc import Callable

from exchange_tools import InMemoryExchangeService
from refund_tools import InMemoryRefundService
from order_tools import OrderRecord
from schemas import ConversationState, IntentResult
from tracing import run_traced_message, TurnTrace


class SequenceInterpreter:
    """Deterministic, no-network interpreter for evaluations."""

    def __init__(self, results: list[IntentResult]) -> None:
        self._results = iter(results)
        self.calls: list[str] = []

    def __call__(self, message: str) -> IntentResult:
        self.calls.append(message)
        return next(self._results, IntentResult(intent="unknown"))


class InMemoryLookup:
    """Deterministic in-memory order lookup tool."""

    def __init__(self, orders: dict[str, OrderRecord]) -> None:
        self._orders = orders
        self.calls: list[str] = []

    def __call__(self, order_id: str) -> OrderRecord | None:
        self.calls.append(order_id)
        return self._orders.get(order_id.upper())


class InMemoryCreator:
    """Simple counting creator without DB side effects."""

    def __init__(self, service: Callable[[str, str], Any]) -> None:
        self._service = service
        self.calls: list[tuple[str, str]] = []

    def __call__(self, order_id: str, reason: str):
        self.calls.append((order_id, reason))
        return self._service(order_id, reason)


def _build_interpreter(sequence: list[dict[str, Any]]) -> SequenceInterpreter:
    results = [IntentResult(**entry) for entry in sequence]
    return SequenceInterpreter(results)


def _build_orders(records: dict[str, dict[str, Any]]) -> dict[str, OrderRecord]:
    normalized: dict[str, OrderRecord] = {}
    for order_id, order_data in records.items():
        normalized[order_id.upper()] = OrderRecord(
            order_id=order_id.upper(),
            product=order_data["product"],
            status=order_data["status"],
            days_since_delivery=order_data["days_since_delivery"],
        )
    return normalized


def _run_turn(
    state: ConversationState,
    message: str,
    interpreter: SequenceInterpreter,
    order_lookup: InMemoryLookup,
    exchange_creator: InMemoryCreator,
    refund_creator: InMemoryCreator,
    events: list[TurnTrace],
) -> ConversationState:
    next_state, trace = run_traced_message(
        state,
        message,
        interpreter,
        order_lookup,
        exchange_creator,
        refund_creator,
    )
    events.append(trace)
    return next_state


def _check_scenario(
    scenario: dict[str, Any],
) -> dict[str, Any]:
    state = ConversationState()
    interpreter = _build_interpreter(scenario["interpreter_intents"])
    order_lookup = InMemoryLookup(_build_orders(scenario["orders"]))
    exchange_creator = InMemoryCreator(InMemoryExchangeService().create_request)
    refund_creator = InMemoryCreator(InMemoryRefundService().create_request)
    traces: list[TurnTrace] = []

    failures: list[str] = []
    final_state = state
    for message in scenario["turns"]:
        final_state = _run_turn(
            final_state,
            message,
            interpreter,
            order_lookup,
            exchange_creator,
            refund_creator,
            traces,
        )

    exp = scenario["expectations"]
    if final_state.status != exp["final_status"]:
        failures.append(f"status={final_state.status}, expect {exp['final_status']}")
    final_intent = (
        final_state.intent_result.intent if final_state.intent_result else None
    )
    if final_intent != exp["final_intent"]:
        failures.append(f"intent={final_intent}, expect {exp['final_intent']}")
    if bool(final_state.order is not None) != exp["final_has_order"]:
        failures.append(
            "order presence mismatch"
            f" got {final_state.order is not None} expect {exp['final_has_order']}"
        )
    if bool(final_state.exchange_request is not None) != exp["final_has_exchange_request"]:
        failures.append(
            "exchange_request presence mismatch"
            f" got {final_state.exchange_request is not None}"
            f" expect {exp['final_has_exchange_request']}"
        )
    if bool(final_state.refund_request is not None) != exp["final_has_refund_request"]:
        failures.append(
            "refund_request presence mismatch"
            f" got {final_state.refund_request is not None}"
            f" expect {exp['final_has_refund_request']}"
        )

    if len(interpreter.calls) != exp["interpreter_calls"]:
        failures.append(
            f"interpreter calls {len(interpreter.calls)} != {exp['interpreter_calls']}"
        )
    if len(order_lookup.calls) != exp["tool_calls"]["order_lookup"]:
        failures.append(
            f"order_lookup calls {len(order_lookup.calls)} != "
            f"{exp['tool_calls']['order_lookup']}"
        )
    if len(exchange_creator.calls) != exp["tool_calls"]["exchange_creator"]:
        failures.append(
            f"exchange_creator calls {len(exchange_creator.calls)} != "
            f"{exp['tool_calls']['exchange_creator']}"
        )
    if len(refund_creator.calls) != exp["tool_calls"]["refund_creator"]:
        failures.append(
            f"refund_creator calls {len(refund_creator.calls)} != "
            f"{exp['tool_calls']['refund_creator']}"
        )

    return {
        "name": scenario["name"],
        "passed": not failures,
        "failures": failures,
        "trace_count": len(traces),
        "final_status": final_state.status,
        "final_intent": final_intent,
    }


def _as_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result["passed"])
    final_state_passed = sum(
        1
        for result in results
        if all(not failure for failure in result["failures"])
    )
    tool_boundary_passed = final_state_passed

    return {
        "scenario_total": total,
        "scenario_passed": passed,
        "scenario_pass_rate": passed / total if total else 0.0,
        "final_state_pass_rate": final_state_passed / total if total else 0.0,
        "tool_boundary_pass_rate": tool_boundary_passed / total if total else 0.0,
        "scenarios": results,
        "failures": [r for r in results if not r["passed"]],
    }


def _print_summary(report: dict[str, Any]) -> None:
    print("eval summary:")
    print(f"  scenario_total: {report['scenario_total']}")
    print(f"  scenario_passed: {report['scenario_passed']}")
    print(f"  scenario_pass_rate: {report['scenario_pass_rate']:.2%}")
    print(f"  final_state_pass_rate: {report['final_state_pass_rate']:.2%}")
    print(f"  tool_boundary_pass_rate: {report['tool_boundary_pass_rate']:.2%}")
    if report["failures"]:
        print("failed scenarios:")
        for item in report["failures"]:
            print(f" - {item['name']}: {', '.join(item['failures'])}")


def _parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(description="Run deterministic V11 evaluation scenarios")
    parser.add_argument(
        "--scenarios",
        default=str(Path(__file__).with_name("scenarios.json")),
        help="Path to scenario JSON file.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional JSON report path.",
    )
    return parser.parse_args(argv)


def run_eval(
    scenarios_path: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    with scenarios_path.open("r", encoding="utf-8") as f:
        scenarios = json.load(f)

    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        result = _check_scenario(scenario)
        results.append(result)

    report = _as_report(results)
    _print_summary(report)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = run_eval(Path(args.scenarios), Path(args.output) if args.output else None)
    return 0 if report["scenario_passed"] == report["scenario_total"] else 1


if __name__ == "__main__":
    sys.exit(main())
