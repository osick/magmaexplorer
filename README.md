# magmaexplorer

A small interactive REPL for exploring magma equations from equational theories
(see <https://teorth.github.io/equational_theories/>). Seed the session with an
equation; grow a numbered list of derived equations via natural-language
commands interpreted by Claude.

## Install

    python -m venv .venv
    .venv/bin/pip install -e '.[dev]'

## Run

    export ANTHROPIC_API_KEY=sk-...
    .venv/bin/python -m magmaexplorer

Optional CLI flags:

    python -m magmaexplorer --model claude-sonnet-4-6 'x*y=y*(x*x)'

## Commands

    <term>=<term>       add an equation directly
    /list               show the numbered list
    /clear              empty the list (asks for confirmation)
    /save <path>        write list to a JSON file
    /load <path>        replace list from a JSON file
    /debug              toggle printing of the exact payload sent to the LLM
    /help               show this list
    /quit               exit (Ctrl-D also works)

Anything else is sent to the LLM as a derivation command, e.g.
`apply y=x*x to 0`. The response must be a single equation; magmaexplorer
parses it syntactically and appends it to the list with the LLM's
justification.

## Term grammar

    equation := term '=' term
    term     := primary ('*' primary)*    # '*' is left-associative
    primary  := variable | '(' term ')'
    variable := single lowercase letter

`x*y*z` parses as `(x*y)*z`. Use parentheses for any other grouping.

## Manual acceptance test

1. `pip install -e '.[dev]'`
2. `export ANTHROPIC_API_KEY=...`
3. `python -m magmaexplorer`
4. Type `x*y=y*(x*x)` — see `[0] x*y = y*(x*x)`.
5. Type `apply y=x*x to 0` — see `[1] ... - <justification>`.
6. Type `/list` — see both lines.
7. `/save /tmp/m.json`, `/clear`, `/load /tmp/m.json`, `/list` — list restored.
8. `/quit`.

## Tests

    .venv/bin/pytest --cov=src/magmaexplorer
