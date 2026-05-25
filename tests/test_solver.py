"""Tests for the minimal proof-search solver (`magmaexplorer.solver`).

The solver is the bridge between an LLM and `lean_export`: given a hypothesis
equation `from_eq` and a goal `to_eq`, it runs a bounded search loop calling an
injectable `llm` callable, verifies each DSL step via `dsl.execute_step`, and
returns a list of `Entry` ready for `lean_export.render_implication_file`.

The LLM is injected as a `StubLLM` callable so these tests are deterministic
and network-free.
"""

from __future__ import annotations

import pytest

from magmaexplorer import lean_export, solver
from magmaexplorer.entries import Entry
from magmaexplorer.llm import LLMError, LLMResult
from magmaexplorer.term import parse_equation


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class StubLLM:
    """Programmable callable matching `llm.LLMCallable` signature.

    Pop one response per call from `responses`. Each entry may be an
    `LLMResult` (returned) or an `Exception` (raised). Calls are recorded
    on `.calls` as `(items, command)` tuples.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[list, str]] = []

    def __call__(self, items, command):
        self.calls.append((list(items), command))
        if not self._responses:
            raise AssertionError(
                f"StubLLM ran out of responses after {len(self.calls)} calls"
            )
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


# ---------------------------------------------------------------------------
# verify_derivation
# ---------------------------------------------------------------------------


class TestVerifyDerivation:
    def test_single_sym_step_ok(self):
        items = [parse_equation("a*b = c")]
        v = solver.verify_derivation(["sym [0]"], items)
        assert v.ok is True
        assert v.error is None
        assert v.final_equation == parse_equation("c = a*b")
        assert v.annotated_steps == ["✓ sym [0]"]

    def test_no_steps_is_not_ok(self):
        v = solver.verify_derivation([], [parse_equation("a = a")])
        assert v.ok is False
        assert v.final_equation is None
        assert "no steps" in (v.error or "")

    def test_unparseable_step_marked_question(self):
        items = [parse_equation("a = b")]
        v = solver.verify_derivation(["frobnicate [0]"], items)
        assert v.ok is False
        assert v.annotated_steps == ["? frobnicate [0]"]
        assert "parse failed" in (v.error or "")

    def test_executor_failure_marked_cross(self):
        items = [parse_equation("a = b")]
        v = solver.verify_derivation(["trans [0] [0]"], items)
        # trans on same equation against itself: a == a -> result b = b? actually
        # a.lhs == b.lhs (both equal "a"), so result is rhs=rhs => b=b. So it
        # actually succeeds; pick a definitely-failing case instead.
        # Use trans across mismatched equations.
        items2 = [parse_equation("a = b"), parse_equation("c = d")]
        v2 = solver.verify_derivation(["trans [0] [1]"], items2)
        assert v2.ok is False
        assert v2.annotated_steps[0].startswith("✗ ")
        assert "exec failed" in (v2.error or "")

    def test_chain_using_step_refs(self):
        items = [parse_equation("x*y = y*x")]
        v = solver.verify_derivation(
            ["inst [0] x:=a, y:=b", "sym s1"], items
        )
        assert v.ok is True
        assert v.final_equation == parse_equation("b*a = a*b")
        assert v.annotated_steps == ["✓ inst [0] x:=a, y:=b", "✓ sym s1"]

    def test_narrated_steps_include_intermediate_equations(self):
        items = [parse_equation("x*y = y*x")]
        v = solver.verify_derivation(
            ["inst [0] x:=a, y:=b", "sym s1"], items
        )
        assert len(v.narrated_steps) == 2
        # Each ✓ step is followed by `=> <pretty equation>` so the LLM can see
        # the value at every position.
        assert "=> a*b = b*a" in v.narrated_steps[0]
        assert "=> b*a = a*b" in v.narrated_steps[1]
        assert v.narrated_steps[0].startswith("✓ inst [0] x:=a, y:=b")
        assert v.narrated_steps[1].startswith("✓ sym s1")

    def test_narrated_steps_for_failed_chain_show_successful_prefix(self):
        # `sym [0]` succeeds (s1 = b = a). `rewrite s1 using [0]` then tries
        # to rewrite the pattern `a` from [0] inside `b = a` — that succeeds
        # too; replace with the exec-failing case below.
        items = [parse_equation("a = b"), parse_equation("c = d")]
        v = solver.verify_derivation(["sym [0]", "trans s1 [1]"], items)
        assert v.ok is False
        # Successful prefix is narrated with its result; failed step keeps the
        # error annotation (no `=>`).
        assert "=> b = a" in v.narrated_steps[0]
        assert v.narrated_steps[1].startswith("✗ trans s1 [1]")
        assert "=>" not in v.narrated_steps[1]

    def test_narrated_steps_for_parse_failure(self):
        v = solver.verify_derivation(["junk garbage"], [parse_equation("a = b")])
        assert v.ok is False
        assert v.narrated_steps == ["? junk garbage"]

    def test_expected_final_matches_returns_ok(self):
        items = [parse_equation("a*b = c")]
        v = solver.verify_derivation(
            ["sym [0]"], items, expected_final=parse_equation("c = a*b")
        )
        assert v.ok is True
        assert v.error is None

    def test_expected_final_mismatch_returns_not_ok(self):
        items = [parse_equation("a*b = c")]
        v = solver.verify_derivation(
            ["sym [0]"], items, expected_final=parse_equation("c = c")
        )
        assert v.ok is False
        assert v.final_equation == parse_equation("c = a*b")
        assert "≠" in (v.error or "") or "!=" in (v.error or "")


# ---------------------------------------------------------------------------
# solve_implication: happy paths
# ---------------------------------------------------------------------------


class TestSolveHappyPath:
    def test_trivial_when_from_equals_to_skips_llm(self):
        eq = parse_equation("a*b = b*a")
        stub = StubLLM([])  # no responses; if LLM is called, the stub will assert
        entries = solver.solve_implication(eq, eq, llm=stub)
        assert len(entries) == 2
        assert entries[0].content == eq
        assert entries[0].sources == []
        assert entries[1].content == eq
        assert entries[1].sources == [0]
        assert stub.calls == []

    def test_single_sym_step(self):
        from_eq = parse_equation("a*b = c")
        to_eq = parse_equation("c = a*b")
        stub = StubLLM([
            LLMResult(equation="c = a*b", steps=["sym [0]"], sources=[0]),
        ])
        entries = solver.solve_implication(from_eq, to_eq, llm=stub)
        assert len(entries) == 2
        assert entries[0].content == from_eq
        assert entries[1].content == to_eq
        assert entries[1].sources == [0]
        assert entries[1].steps == ["✓ sym [0]"]
        assert len(stub.calls) == 1

    def test_multi_step_inst_then_sym(self):
        from_eq = parse_equation("x*y = y*x")
        to_eq = parse_equation("b*a = a*b")
        stub = StubLLM([
            LLMResult(
                equation="b*a = a*b",
                steps=["inst [0] x:=a, y:=b", "sym s1"],
                sources=[0],
            ),
        ])
        entries = solver.solve_implication(from_eq, to_eq, llm=stub)
        assert len(entries) == 2
        assert entries[1].content == to_eq
        assert entries[1].steps == [
            "✓ inst [0] x:=a, y:=b",
            "✓ sym s1",
        ]


# ---------------------------------------------------------------------------
# solve_implication: retry behaviour
# ---------------------------------------------------------------------------


class TestSolveRetries:
    def test_retry_on_unparseable_dsl(self):
        from_eq = parse_equation("a*b = c")
        to_eq = parse_equation("c = a*b")
        stub = StubLLM([
            LLMResult(equation="c = a*b", steps=["junk garbage"], sources=[0]),
            LLMResult(equation="c = a*b", steps=["sym [0]"], sources=[0]),
        ])
        entries = solver.solve_implication(from_eq, to_eq, llm=stub, max_attempts=3)
        assert entries[1].content == to_eq
        assert len(stub.calls) == 2

    def test_retry_on_wrong_final_equation(self):
        from_eq = parse_equation("x*y = y*x")
        to_eq = parse_equation("b*a = a*b")
        stub = StubLLM([
            # Wrong: only inst, no sym -- final would be a*b = b*a, not b*a = a*b
            LLMResult(
                equation="a*b = b*a",
                steps=["inst [0] x:=a, y:=b"],
                sources=[0],
            ),
            LLMResult(
                equation="b*a = a*b",
                steps=["inst [0] x:=a, y:=b", "sym s1"],
                sources=[0],
            ),
        ])
        entries = solver.solve_implication(from_eq, to_eq, llm=stub, max_attempts=3)
        assert entries[1].content == to_eq
        assert len(stub.calls) == 2

    def test_retry_on_llm_error(self):
        from_eq = parse_equation("a*b = c")
        to_eq = parse_equation("c = a*b")
        stub = StubLLM([
            LLMError("simulated network failure"),
            LLMResult(equation="c = a*b", steps=["sym [0]"], sources=[0]),
        ])
        entries = solver.solve_implication(from_eq, to_eq, llm=stub, max_attempts=3)
        assert entries[1].content == to_eq
        assert len(stub.calls) == 2

    def test_max_attempts_exhausted_raises(self):
        from_eq = parse_equation("a = b")
        to_eq = parse_equation("a = c")  # unreachable from from_eq
        stub = StubLLM([
            LLMResult(equation="a = c", steps=["sym [0]"], sources=[0]),
            LLMResult(equation="a = c", steps=["sym [0]"], sources=[0]),
            LLMResult(equation="a = c", steps=["sym [0]"], sources=[0]),
        ])
        with pytest.raises(solver.SolverError) as excinfo:
            solver.solve_implication(from_eq, to_eq, llm=stub, max_attempts=3)
        assert "3 attempts" in str(excinfo.value)
        assert len(stub.calls) == 3

    def test_max_attempts_one(self):
        from_eq = parse_equation("a = b")
        to_eq = parse_equation("a = c")
        stub = StubLLM([
            LLMResult(equation="a = c", steps=["sym [0]"], sources=[0]),
        ])
        with pytest.raises(solver.SolverError):
            solver.solve_implication(from_eq, to_eq, llm=stub, max_attempts=1)
        assert len(stub.calls) == 1


# ---------------------------------------------------------------------------
# solve_implication: prompt shape
# ---------------------------------------------------------------------------


class TestPromptShape:
    def test_command_mentions_goal(self):
        from_eq = parse_equation("a*b = c")
        to_eq = parse_equation("c = a*b")
        stub = StubLLM([
            LLMResult(equation="c = a*b", steps=["sym [0]"], sources=[0]),
        ])
        solver.solve_implication(from_eq, to_eq, llm=stub)
        _, command = stub.calls[0]
        assert "c = a*b" in command

    def test_command_references_entry_zero(self):
        from_eq = parse_equation("a*b = c")
        to_eq = parse_equation("c = a*b")
        stub = StubLLM([
            LLMResult(equation="c = a*b", steps=["sym [0]"], sources=[0]),
        ])
        solver.solve_implication(from_eq, to_eq, llm=stub)
        _, command = stub.calls[0]
        assert "[0]" in command

    def test_retry_command_includes_full_narrated_history(self):
        # First attempt: an inst that succeeds but no sym; final equation
        # mismatches the goal. The retry command must include the LLM-visible
        # narrated chain — both the bare step text AND the intermediate
        # equation values — so the LLM can see exactly where it landed.
        from_eq = parse_equation("x*y = y*x")
        to_eq = parse_equation("b*a = a*b")
        stub = StubLLM([
            LLMResult(
                equation="a*b = b*a",
                steps=["inst [0] x:=a, y:=b"],
                sources=[0],
            ),
            LLMResult(
                equation="b*a = a*b",
                steps=["inst [0] x:=a, y:=b", "sym s1"],
                sources=[0],
            ),
        ])
        solver.solve_implication(from_eq, to_eq, llm=stub)
        _, second_command = stub.calls[1]
        # The retry includes the previous step trace verbatim.
        assert "inst [0] x:=a, y:=b" in second_command
        # And the intermediate equation value the step produced.
        assert "a*b = b*a" in second_command
        # Plus the high-level failure reason.
        assert "≠" in second_command or "final" in second_command.lower()

    def test_retry_command_includes_previous_error(self):
        from_eq = parse_equation("a*b = c")
        to_eq = parse_equation("c = a*b")
        stub = StubLLM([
            LLMResult(equation="c = a*b", steps=["totally bogus"], sources=[0]),
            LLMResult(equation="c = a*b", steps=["sym [0]"], sources=[0]),
        ])
        solver.solve_implication(from_eq, to_eq, llm=stub)
        _, second_command = stub.calls[1]
        assert "previous" in second_command.lower() or "failed" in second_command.lower()

    def test_items_passed_contain_only_from_eq(self):
        from_eq = parse_equation("a*b = c")
        to_eq = parse_equation("c = a*b")
        stub = StubLLM([
            LLMResult(equation="c = a*b", steps=["sym [0]"], sources=[0]),
        ])
        solver.solve_implication(from_eq, to_eq, llm=stub)
        items_seen, _ = stub.calls[0]
        assert items_seen == [from_eq]


# ---------------------------------------------------------------------------
# solve_implication: source normalisation
# ---------------------------------------------------------------------------


class TestSourceNormalisation:
    def test_empty_sources_defaults_to_zero(self):
        from_eq = parse_equation("a*b = c")
        to_eq = parse_equation("c = a*b")
        stub = StubLLM([
            LLMResult(equation="c = a*b", steps=["sym [0]"], sources=[]),
        ])
        entries = solver.solve_implication(from_eq, to_eq, llm=stub)
        assert entries[1].sources == [0]

    def test_out_of_range_sources_dropped(self):
        from_eq = parse_equation("a*b = c")
        to_eq = parse_equation("c = a*b")
        stub = StubLLM([
            LLMResult(equation="c = a*b", steps=["sym [0]"], sources=[0, 99, -1]),
        ])
        entries = solver.solve_implication(from_eq, to_eq, llm=stub)
        assert entries[1].sources == [0]

    def test_duplicate_sources_collapsed(self):
        from_eq = parse_equation("a*b = c")
        to_eq = parse_equation("c = a*b")
        stub = StubLLM([
            LLMResult(equation="c = a*b", steps=["sym [0]"], sources=[0, 0, 0]),
        ])
        entries = solver.solve_implication(from_eq, to_eq, llm=stub)
        assert entries[1].sources == [0]


# ---------------------------------------------------------------------------
# Integration: solver output round-trips through lean_export
# ---------------------------------------------------------------------------


class TestLeanExportRoundTrip:
    def test_simple_sym_renders_implication_file(self):
        from_eq = parse_equation("a*b = c")
        to_eq = parse_equation("c = a*b")
        stub = StubLLM([
            LLMResult(equation="c = a*b", steps=["sym [0]"], sources=[0]),
        ])
        entries = solver.solve_implication(from_eq, to_eq, llm=stub)

        rendered = lean_export.render_implication_file(
            entries, from_idx=0, to_idx=1, name="test_thm"
        )
        # Compare only against the Lean source proper, not the comment header.
        code_lines = [
            ln for ln in rendered.splitlines() if not ln.lstrip().startswith("--")
        ]
        code = "\n".join(code_lines)

        assert "theorem implication" in code
        # Hypothesis appears as proof parameter, not as an axiom.
        assert "(h :" in code
        assert "axiom" not in code
        # No leftover sorry — sym is auto-translatable.
        assert "sorry" not in code
        # Standalone binders (not Mathlib Type*).
        assert "Type _" in code
        assert "Type*" not in code

    def test_compute_implication_chain_finds_solver_output(self):
        from_eq = parse_equation("x*y = y*x")
        to_eq = parse_equation("b*a = a*b")
        stub = StubLLM([
            LLMResult(
                equation="b*a = a*b",
                steps=["inst [0] x:=a, y:=b", "sym s1"],
                sources=[0],
            ),
        ])
        entries = solver.solve_implication(from_eq, to_eq, llm=stub)
        chain = lean_export.compute_implication_chain(entries, 0, 1)
        # Chain must include both endpoints
        assert 0 in chain
        assert 1 in chain
