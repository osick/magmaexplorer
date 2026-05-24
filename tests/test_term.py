import pytest

from magmaexplorer.term import Equation, Op, ParseError, Var, parse, parse_equation, pretty, pretty_equation


def test_parse_single_variable():
    assert parse("x") == Var("x")


def test_parse_simple_product():
    assert parse("x*y") == Op(Var("x"), Var("y"))


def test_parse_parenthesised_variable():
    assert parse("(x)") == Var("x")


def test_parse_left_associative():
    # x*y*z parses as (x*y)*z
    assert parse("x*y*z") == Op(Op(Var("x"), Var("y")), Var("z"))


def test_parse_explicit_right_assoc():
    # y*(x*x) keeps the right grouping
    assert parse("y*(x*x)") == Op(Var("y"), Op(Var("x"), Var("x")))


def test_parse_strips_whitespace():
    assert parse(" x  *  y ") == Op(Var("x"), Var("y"))


def test_pretty_variable():
    assert pretty(Var("x")) == "x"


def test_pretty_left_assoc_no_parens():
    # (x*y)*z prints as x*y*z (no parens needed under left-assoc)
    t = Op(Op(Var("x"), Var("y")), Var("z"))
    assert pretty(t) == "x*y*z"


def test_pretty_right_grouping_keeps_parens():
    # y*(x*x) must keep the parens
    t = Op(Var("y"), Op(Var("x"), Var("x")))
    assert pretty(t) == "y*(x*x)"


def test_pretty_round_trip():
    for s in ["x", "x*y", "x*y*z", "y*(x*x)", "(a*b)*(c*d)"]:
        assert pretty(parse(s)) == s.replace(" ", "").replace("(a*b)*(c*d)", "a*b*(c*d)")


def test_parse_equation_basic():
    eq = parse_equation("x*y = y*(x*x)")
    assert eq.lhs == Op(Var("x"), Var("y"))
    assert eq.rhs == Op(Var("y"), Op(Var("x"), Var("x")))


def test_pretty_equation():
    eq = Equation(Op(Var("x"), Var("y")), Op(Var("y"), Op(Var("x"), Var("x"))))
    assert pretty_equation(eq) == "x*y = y*(x*x)"


@pytest.mark.parametrize("bad", [
    "",          # empty
    "X",         # uppercase not allowed
    "xy",        # multi-letter var
    "x*",        # trailing operator
    "*x",        # leading operator
    "(x",        # unbalanced
    "x)",        # unbalanced
    "x*(y",      # unbalanced
    "x**y",      # double operator
    "x y",       # adjacency
])
def test_parse_rejects_malformed(bad):
    with pytest.raises(ParseError):
        parse(bad)


@pytest.mark.parametrize("bad", [
    "x",            # no =
    "x=y=z",        # two =
    "=x",           # empty lhs
    "x=",           # empty rhs
])
def test_parse_equation_rejects_malformed(bad):
    with pytest.raises(ParseError):
        parse_equation(bad)


# --- Definitions (`:=`) ---

from magmaexplorer.term import Definition, parse_entry


def test_parse_definition_basic():
    d = parse_entry("u := x*x")
    assert isinstance(d, Definition)
    assert d.name == "u"
    assert d.body == Op(Var("x"), Var("x"))


def test_parse_definition_with_whitespace():
    d = parse_entry("  u  :=  x*x  ")
    assert isinstance(d, Definition)
    assert d.name == "u"


def test_parse_entry_dispatches_to_equation():
    e = parse_entry("x*y = y*x")
    assert isinstance(e, Equation)
    assert e.lhs == Op(Var("x"), Var("y"))


@pytest.mark.parametrize("bad", [
    "uv := x*x",       # multi-letter LHS
    "x*y := z",        # compound LHS
    "X := y",          # uppercase LHS
    ":= y",            # empty LHS
    "u :=",            # empty body
    "u := v := w",     # double :=
])
def test_parse_definition_rejects_malformed(bad):
    with pytest.raises(ParseError):
        parse_entry(bad)
