-- magmaexplorer implication: [0] => [10]
-- Chain length: 9 entries.
-- Hypothesis (h): x*y = y*(x*x)
-- Goal:           y*x = x*y
--
-- The hypothesis appears as the proof parameter `h` (no `axiom` declarations,
-- which the equational-theories Stage 2 judge would reject as `incomplete_proof`).
-- Intermediate derivation steps are inlined as universally-quantified `have`
-- blocks; the final tactic block discharges the goal.
--
-- For the equational-theories Lean project, swap `[Mul G]` for `[Magma G]` and
-- `*` for the project's `◇` notation as needed.

theorem implication {G : Type _} [Mul G]
    (h : ∀ x y : G, x * y = y * (x * x)) :
    ∀ x y : G, y * x = x * y := by
  have h_1 : ∀ x y : G, y * (x * x) = x * x * (y * y) := by
    intro x y
    exact h y (x * x)
  have h_2 : ∀ x y : G, x * y = x * x * (y * y) := by
    intro x y
    -- NOTE: `rw` rewrites ALL occurrences; the DSL only rewrites the
    -- leftmost-outermost one. If the goal disagrees, replace `rw` with
    -- `nth_rewrite 1` (from Mathlib) to target a single occurrence.
    have h_rw := h_1 x y
    rw [← h] at h_rw
    exact h_rw
  have h_3 : ∀ x y : G, y * (x * x) = y * y * (x * x * (x * x)) := by
    intro x y
    exact h_2 y (x * x)
  have h_4 : ∀ x : G, x * x = x * x * (x * x) := by
    intro x
    exact h_2 x x
  have h_5 : ∀ x y : G, y * (x * x) = y * y * (x * x) := by
    intro x y
    -- NOTE: `rw` rewrites ALL occurrences; the DSL only rewrites the
    -- leftmost-outermost one. If the goal disagrees, replace `rw` with
    -- `nth_rewrite 1` (from Mathlib) to target a single occurrence.
    have h_rw := h_3 x y
    rw [← h_4] at h_rw
    exact h_rw
  have h_6 : ∀ x y : G, y * y * (x * x) = x * y := by
    intro x y
    exact (h_5 x y).symm.trans (h x y).symm
  have h_8 : ∀ x y : G, x * x * (y * y) = y * x := by
    intro x y
    exact h_6 y x
  intro x y
  exact (h_8 x y).symm.trans (h_2 x y).symm
