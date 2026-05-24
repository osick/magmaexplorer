# magmaexplorer report: commute

_11 entries_

## Entries

| # | Kind | Statement | Sources | Steps |
|---|------|-----------|---------|-------|
| [0] | equation | `x*y = y*(x*x)` | - | - |
| [1] | equation | `y*(x*x) = x*x*(y*y)` | 0 | 1. inst [0] x:=y, y:=x*x |
| [2] | equation | `x*y = x*x*(y*y)` | 1, 0 | 1. rewrite [1] using [0] backwards |
| [3] | equation | `y*(x*x) = y*y*(x*x*(x*x))` | 2 | 1. inst [2] y:=x*x, x:=y |
| [4] | equation | `x*x = x*x*(x*x)` | 2 | 1. inst [2] y:=x |
| [5] | equation | `y*(x*x) = y*y*(x*x)` | 3, 4 | 1. rewrite [3] using [4] backwards |
| [6] | equation | `y*y*(x*x) = x*y` | 5, 0 | 1. trans [5] [0] |
| [7] | equation | `y*y*(x*x) = x*y` | 5, 0 | 1. trans [5] [0] |
| [8] | equation | `x*x*(y*y) = y*x` | 6 | 1. inst [6] x:=y, y:=x |
| [9] | equation | `x*y = y*y*(x*x)` | 7 | 1. sym [7] |
| [10] | equation | `y*x = x*y` | 8, 2 | 1. trans [8] [2] |

## Deduction graph

Each node shows the entry's magma statement. An arrow `na --> nb` means entry `b` cites entry `a` as a source.
Edge labels name the DSL primitive(s) that consumed the source while deriving the target.
Definitions are drawn as stadiums; equations as rectangles.

```mermaid
graph TD
    n0["x*y = y*(x*x)"]
    n1["y*(x*x) = x*x*(y*y)"]
    n2["x*y = x*x*(y*y)"]
    n3["y*(x*x) = y*y*(x*x*(x*x))"]
    n4["x*x = x*x*(x*x)"]
    n5["y*(x*x) = y*y*(x*x)"]
    n6["y*y*(x*x) = x*y"]
    n7["y*y*(x*x) = x*y"]
    n8["x*x*(y*y) = y*x"]
    n9["x*y = y*y*(x*x)"]
    n10["y*x = x*y"]
    n0 -->|inst| n1
    n1 -->|rewrite| n2
    n0 -->|rewrite| n2
    n2 -->|inst| n3
    n2 -->|inst| n4
    n3 -->|rewrite| n5
    n4 -->|rewrite| n5
    n5 -->|trans| n6
    n0 -->|trans| n6
    n5 -->|trans| n7
    n0 -->|trans| n7
    n6 -->|inst| n8
    n7 -->|sym| n9
    n8 -->|trans| n10
    n2 -->|trans| n10
```
