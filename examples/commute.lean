-- magmaexplorer export: commute_script
-- 11 entries
--
-- Each `axiom` is an input equation (no derivation in the REPL).
-- Each `theorem` carries a proof or a `sorry` placeholder; the comment block
-- above it records the DSL primitives the magmaexplorer REPL used to derive it.
-- Fill in the `by ...` blocks to produce a complete Lean proof script for
-- submission (e.g. to the equational-theories distillation challenge).
--
-- Each declaration carries its own `{G : Type _} [Mul G]` binders. `axiom`
-- does not pick up `variable` declarations in Lean 4 and `Type*` is a
-- Mathlib-only shorthand, so making the binders explicit keeps the file
-- compiling in both vanilla Lean 4 and Mathlib environments.


-- [0] axiom: x*y = y*(x*x)
axiom eq_0 {G : Type _} [Mul G] : ∀ x y : G, x * y = y * (x * x)

-- [1] derived from [0]
--     1. inst [0] x:=y, y:=x*x
theorem eq_1 {G : Type _} [Mul G] : ∀ x y : G, y * (x * x) = x * x * (y * y) := by
  intro x y
  exact eq_0 y (x * x)

-- [2] derived from [1], [0]
--     1. rewrite [1] using [0] backwards
theorem eq_2 {G : Type _} [Mul G] : ∀ x y : G, x * y = x * x * (y * y) := by
  intro x y
  -- NOTE: `rw` rewrites ALL occurrences; the DSL only rewrites the
  -- leftmost-outermost one. If the goal disagrees, replace `rw` with
  -- `nth_rewrite 1` (from Mathlib) to target a single occurrence.
  have h := eq_1 x y
  rw [← eq_0] at h
  exact h

-- [3] derived from [2]
--     1. inst [2] y:=x*x, x:=y
theorem eq_3 {G : Type _} [Mul G] : ∀ x y : G, y * (x * x) = y * y * (x * x * (x * x)) := by
  intro x y
  exact eq_2 y (x * x)

-- [4] derived from [2]
--     1. inst [2] y:=x
theorem eq_4 {G : Type _} [Mul G] : ∀ x : G, x * x = x * x * (x * x) := by
  intro x
  exact eq_2 x x

-- [5] derived from [3], [4]
--     1. rewrite [3] using [4] backwards
theorem eq_5 {G : Type _} [Mul G] : ∀ x y : G, y * (x * x) = y * y * (x * x) := by
  intro x y
  -- NOTE: `rw` rewrites ALL occurrences; the DSL only rewrites the
  -- leftmost-outermost one. If the goal disagrees, replace `rw` with
  -- `nth_rewrite 1` (from Mathlib) to target a single occurrence.
  have h := eq_3 x y
  rw [← eq_4] at h
  exact h

-- [6] derived from [5], [0]
--     1. trans [5] [0]
theorem eq_6 {G : Type _} [Mul G] : ∀ x y : G, y * y * (x * x) = x * y := by
  intro x y
  exact (eq_5 x y).symm.trans (eq_0 x y).symm

-- [7] derived from [5], [0]
--     1. trans [5] [0]
theorem eq_7 {G : Type _} [Mul G] : ∀ x y : G, y * y * (x * x) = x * y := by
  intro x y
  exact (eq_5 x y).symm.trans (eq_0 x y).symm

-- [8] derived from [6]
--     1. inst [6] x:=y, y:=x
theorem eq_8 {G : Type _} [Mul G] : ∀ x y : G, x * x * (y * y) = y * x := by
  intro x y
  exact eq_6 y x

-- [9] derived from [7]
--     1. sym [7]
theorem eq_9 {G : Type _} [Mul G] : ∀ x y : G, x * y = y * y * (x * x) := by
  intro x y
  exact (eq_7 x y).symm

-- [10] derived from [8], [2]
--     1. trans [8] [2]
theorem eq_10 {G : Type _} [Mul G] : ∀ x y : G, y * x = x * y := by
  intro x y
  exact (eq_8 x y).symm.trans (eq_2 x y).symm
