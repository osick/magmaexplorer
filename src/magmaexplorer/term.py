"""Term AST, parser, and pretty-printer for magma equations.

Grammar:
    entry      := equation | definition
    equation   := term '=' term
    definition := variable ':=' term          # syntactic abbreviation, NOT an equation
    term       := primary ('*' primary)*      # left-associative
    primary    := variable | '(' term ')'
    variable   := [a-z]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Op:
    left: "Term"
    right: "Term"


Term = Union[Var, Op]


@dataclass(frozen=True)
class Equation:
    lhs: Term
    rhs: Term


@dataclass(frozen=True)
class Definition:
    name: str  # single lowercase letter
    body: Term


class _Parser:
    def __init__(self, src: str):
        self.src = "".join(src.split())  # strip all whitespace
        self.pos = 0

    def peek(self) -> str:
        return self.src[self.pos] if self.pos < len(self.src) else ""

    def eat(self, ch: str) -> None:
        if self.peek() != ch:
            raise ParseError(f"expected {ch!r} at column {self.pos}, got {self.peek()!r}")
        self.pos += 1

    def at_end(self) -> bool:
        return self.pos >= len(self.src)

    def parse_term(self) -> Term:
        left = self.parse_primary()
        while self.peek() == "*":
            self.eat("*")
            right = self.parse_primary()
            left = Op(left, right)
        return left

    def parse_primary(self) -> Term:
        ch = self.peek()
        if ch == "(":
            self.eat("(")
            inner = self.parse_term()
            self.eat(")")
            return inner
        if ch.isalpha() and ch.islower() and len(ch) == 1:
            self.pos += 1
            return Var(ch)
        raise ParseError(f"expected variable or '(' at column {self.pos}, got {ch!r}")


def parse(src: str) -> Term:
    p = _Parser(src)
    if p.at_end():
        raise ParseError("empty term")
    result = p.parse_term()
    if not p.at_end():
        raise ParseError(f"unexpected {p.peek()!r} at column {p.pos}")
    return result


def parse_equation(src: str) -> Equation:
    parts = src.split("=")
    if len(parts) != 2:
        raise ParseError(f"expected exactly one '=' in equation, found {len(parts) - 1}")
    return Equation(parse(parts[0]), parse(parts[1]))


def parse_definition(src: str) -> Definition:
    parts = src.split(":=")
    if len(parts) != 2:
        raise ParseError(f"expected exactly one ':=' in definition, found {len(parts) - 1}")
    name = parts[0].strip()
    if len(name) != 1 or not (name.isalpha() and name.islower()):
        raise ParseError(f"definition name must be a single lowercase letter, got {name!r}")
    body = parse(parts[1])
    return Definition(name=name, body=body)


def parse_entry(src: str) -> Equation | Definition:
    """Parse either `lhs = rhs` (Equation) or `name := body` (Definition).

    Dispatches on the presence of `:=` (which takes priority over `=`, since
    `:=` contains `=`).
    """
    if ":=" in src:
        return parse_definition(src)
    return parse_equation(src)


def _needs_parens_right(child: Term) -> bool:
    # Right child of an Op needs parens iff it is itself an Op
    # (because `a*b*c` would otherwise re-parse as `(a*b)*c`).
    return isinstance(child, Op)


def pretty(t: Term) -> str:
    if isinstance(t, Var):
        return t.name
    left = pretty(t.left)
    right = pretty(t.right)
    if _needs_parens_right(t.right):
        right = f"({right})"
    return f"{left}*{right}"


def pretty_equation(e: Equation) -> str:
    return f"{pretty(e.lhs)} = {pretty(e.rhs)}"


def pretty_definition(d: Definition) -> str:
    return f"{d.name} := {pretty(d.body)}"


def pretty_entry(item: Equation | Definition) -> str:
    if isinstance(item, Definition):
        return pretty_definition(item)
    return pretty_equation(item)
