-- magmaexplorer export: commute_script
-- 11 entries
--
-- Each `axiom` is an input equation (no derivation in the REPL).
-- Each `theorem` carries a `sorry` placeholder; the comment block above it
-- records the DSL primitives the magmaexplorer REPL used to derive it.
-- Fill in the `by ...` blocks to produce a complete Lean proof script for
-- submission (e.g. to the equational-theories distillation challenge).

variable {G : Type*} [Mul G]


-- [0] axiom: x*y = y*(x*x)
axiom eq_0 : ∀ x y : G, x * y = y * (x * x)

-- [1] derived from [0]
--     1. inst [0] x:=y, y:=x*x
theorem eq_1 : ∀ x y : G, y * (x * x) = x * x * (y * y) := by
  sorry

-- [2] derived from [1], [0]
--     1. rewrite [1] using [0] backwards
theorem eq_2 : ∀ x y : G, x * y = x * x * (y * y) := by
  sorry

-- [3] derived from [2]
--     1. inst [2] y:=x*x, x:=y
theorem eq_3 : ∀ x y : G, y * (x * x) = y * y * (x * x * (x * x)) := by
  sorry

-- [4] derived from [2]
--     1. inst [2] y:=x
theorem eq_4 : ∀ x : G, x * x = x * x * (x * x) := by
  sorry

-- [5] derived from [3], [4]
--     1. rewrite [3] using [4] backwards
theorem eq_5 : ∀ x y : G, y * (x * x) = y * y * (x * x) := by
  sorry

-- [6] derived from [5], [0]
--     1. trans [5] [0]
theorem eq_6 : ∀ x y : G, y * y * (x * x) = x * y := by
  sorry

-- [7] derived from [5], [0]
--     1. trans [5] [0]
theorem eq_7 : ∀ x y : G, y * y * (x * x) = x * y := by
  sorry

-- [8] derived from [6]
--     1. inst [6] x:=y, y:=x
theorem eq_8 : ∀ x y : G, x * x * (y * y) = y * x := by
  sorry

-- [9] derived from [7]
--     1. sym [7]
theorem eq_9 : ∀ x y : G, x * y = y * y * (x * x) := by
  sorry

-- [10] derived from [8], [2]
--     1. trans [8] [2]
theorem eq_10 : ∀ x y : G, y * x = x * y := by
  sorry
