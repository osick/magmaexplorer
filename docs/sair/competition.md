# challenge 2 
## Overview
This competition explores a core question in AI for mathematics: can strong mathematical reasoning be distilled into a compact, human-readable artifact that improves LLM performance on formal tasks?

This competition is organized by:

Damek Davis (Associate Professor, Department of Statistics and Data Science, University of Pennsylvania)

Terence Tao (Fields Medalist, Professor at UCLA, Co-Founder of SAIR Foundation)

and SAIR Foundation.

The setup is inspired by Honda, Murakami, and Zhang (2025), Distilling Many-Shot In-Context Learning into a Cheat Sheet. Our difference is that the distilled artifact is discovered through an open competition process rather than a single model query.

Background
The pilot task is equational implication over magmas: given Equation 1 and Equation 2, determine whether Equation 1 implies Equation 2.

This challenge is based on the Equational Theories Project:

Raw implication graph: export_raw_implications
Law list (4694 laws): equations.txt
Example: E_4: x = x * y implies E_3: x = x * x.

Core Task
Stage 2 raises the bar from Stage 1. Instead of only predicting true/false, participants must prove their answers:

If the implication is true: a Lean 4 proof that the hypothesis implies the goal.
If the implication is false: a Lean 4 proof certificate (a finite magma witness where the hypothesis holds but the goal fails).
Both directions require machine-verifiable certificates. A deterministic Lean judge accepts or rejects each answer — no partial credit, no probabilistic scoring.

What Participants Submit
Participants submit a solver: a single solver.py file, ≤ 500 KB, that follows the I/O protocol of the chosen track.

The solver can combine:

Deterministic strategies (brute-force counterexample search, pattern matching, symbolic proof construction)
LLM calls (via the organizer-provided proxy)
Judge calls (submit candidate proofs for Lean verification, receive accept/reject feedback)
Tracks
Stage 2 has two tracks. Both share the same judge, the same five-status verdict mapping, and the same single-file solver.py contract (≤ 500 KB). They differ only in I/O shape and budgeting:

Solo — one problem per solver subprocess, fixed per-problem budget, stdin/stdout JSON protocol.
Marathon — N problems per solver subprocess (reference N=100), one shared global budget = compression_ratio × N × Solo per-problem (default compression_ratio = 0.5), file-based manifest in / append-only JSONL out.
One source file can support both tracks. Concrete I/O, size limits, budgets, scoring, and the evaluation model are documented in evaluation.md.

Key Dates
Stage 2 pre-registration opens: April 23, 2026
Stage 2 officially starts: May 1, 2026, 12:00 UTC
Stage 2 submission deadline: August 31, 2026, 23:59 AoE
Official Repository
The official GitHub repository for Stage 2 contains the evaluation pipeline, demo solvers, a step-by-step tutorial, and local testing support:

https://github.com/SAIRcompetition/equational-theories-lean-stage2
For setup instructions and the recommended local testing workflow, see evaluation.md.

Publication Policy
Stage 1 submitted prompts may be made public to support community learning.
Stage 2 submitted solvers may be made public after evaluation.
Eligibility and Registration
Stage 2 registration is open to everyone — participation is not restricted to Stage 1 participants or top-performing teams. Anyone can register a team and submit a solver before the Stage 2 submission deadline.

Team Participation and Anti-Cheating Policy
Each individual or organization can participate in only one team.
Teams must register members and sponsors in advance.
If coordinated cheating is detected (including sockpuppet teams), all related teams will be disqualified.
Community Feedback
Rules, scoring details, and evaluation procedures are still being refined and will be shaped by community input. Community contributions are welcome.

Join the SAIR Foundation Zulip community for discussion and collaboration:

https://zulip.sair.foundation/

## Stage 2 Evaluation Setup
We want your feedback. The evaluation plan described below — including the model, configuration, scoring rules, and problem sets — is still being refined, and items marked TBD will be decided based on community input. Please share suggestions on the SAIR Foundation Zulip.

This page specifies how Stage 2 submissions are evaluated: submission format, solver environment, budget, scoring, proof policy, and the evaluation model.

For the high-level task description, key dates, and participation policy, see overview.md.

Submission Format
A Stage 2 submission is a single Python file.

File	Purpose	Size limit
solver.py	The solving program for both tracks. Must contain all solving logic, including any prompt text as an in-file constant. The I/O protocol depends on the track (see below).	500 KB
The solver is a free-form Python program. No required function signatures — the only requirement is following the I/O protocol of the chosen track.

If the solver uses LLM calls in Solo, it declares its prompt template as a top-level PROMPT = "..." string literal; the proxy extracts it via static AST parsing (the module is never imported or executed on the host), fills {placeholder} variables, and queries the LLM. In Marathon, the solver makes LLM calls itself via the helper from marathon_llm import call_llm (or any OpenAI-SDK call) against a local HTTP proxy; no template extraction.

Tracks
Stage 2 has two tracks. Both share the same judge, the same five-status verdict mapping, and the same single-file solver.py contract (≤ 500 KB). They differ only in I/O shape and budgeting:

Track	Workload per process	Budget	I/O
Solo	One problem per solver subprocess	Fixed per-problem	stdin (problem JSON) / stdout (answer JSON)
Marathon	N problems per solver subprocess (reference N=100)	Single global budget = compression_ratio × N × Solo per-problem (default compression_ratio = 0.5)	manifest JSONL in / append-only JSONL out
One solver source can support both. Full specs: docs/solo_mode.md and docs/marathon_mode.md in the repository.

Solver Environment
The solver runs in an isolated subprocess:

No secrets: no inherited API keys or environment variables beyond a minimal allowlist (PATH, HOME, LANG, etc.)
No direct network: the internet is reachable only through the organizer-provided proxy
LLM access: through the proxy — Solo via stdin/stdout JSON, Marathon via a local-only HTTP endpoint that authenticates with a per-run shared secret and meters tokens against the global budget
Judge access: through the proxy — Solo via stdin/stdout JSON, Marathon via append-only JSONL output that the runner scores at end of run
 
Solver (subprocess) <--track-specific protocol--> Proxy
                                                 <---> Judge (Lean verification)
                                                 <---> LLM (OpenAI-compatible API)
 
Solver Budget
Reference values in pipeline/config.json. Numbers may still be tuned during Stage 2 based on community feedback.

Solo (per problem):

Resource	Reference value	Notes
Wall-clock timeout	3600 seconds	Excludes organizer-side LLM latency.
LLM max output tokens per call	65536	Per-call cap on the LLM response length.
Submitted Lean code	100 KB	Per-call code size cap.
Marathon (per run, N problems):

The global budget is derived from Solo's per-problem reference:

Resource	Formula	Default at N=100
Wall-clock	compression_ratio × N × 3600 s	180 000 s (50 h) at 0.5
Tokens	compression_ratio × N × 65536	~3.3 M at 0.5
compression_ratio defaults to 0.5 — the solver cannot finish all N at single-problem cost and must triage. Setting it to 1.0 removes compression; smaller values squeeze harder.

The solver manages its own pacing within the budget. Deterministic strategies cost no tokens. Exceeding the wall-clock or token budget terminates the solver.

Answer Format
For each problem, the solver submits a proof certificate via a judge call:

 
{ "call": "judge", "verdict": "true", "code": "<Lean code>" }
 
or

 
{ "call": "judge", "verdict": "false", "code": "<Lean code>" }
 
True certificate: a Lean 4 proof that the hypothesis equation implies the goal equation.
False certificate: a Lean 4 proof that there exists a finite magma satisfying the hypothesis but not the goal.
Both are verified by the deterministic Lean judge. The judge returns exactly one of the following statuses:

Status	Meaning
accepted	Certificate verified successfully
unparsed	Raw JSON could not be parsed
malformed	JSON parsed but violates schema
incomplete_proof	Proof uses sorry, admit, or disallowed axioms/declarations
incorrect	Proof is structurally valid but does not verify in Lean
A problem is solved when the judge returns accepted.

Scoring
TBD. Final scoring rules (point assignment, aggregation across problems, and tiebreakers) are still being decided based on community feedback. The baseline intent is: a problem is solved when the judge returns accepted, and higher solved counts are better.

Proof Policy
Submitted proofs are checked against a dependency policy:

Allowed trusted axioms: propext, Quot.sound, Classical.choice
Allowed declarations: configurable allowlist per problem (when specified)
Proofs using sorry, admit, or disallowed axioms/declarations are rejected as incomplete_proof.
Evaluation Model
TBD. The evaluation model — including the model family, provider, and routing — is still being decided. The current candidate under consideration is an open-weight model accessed via OpenRouter with a pinned provider route and deterministic settings (seeded, low temperature), but this is subject to community feedback.

Evaluation Configuration
TBD. Final generation parameters (temperature, max output tokens, reasoning effort, seeding, provider fallback policy) will be published alongside the confirmed evaluation model. Whatever settings are finalized will be reflected in pipeline/config.json in the repository.

Evaluation Problem Sets
TBD. The private Stage 2 evaluation set (size, composition, balance between true/false implications) is still being decided. The set is separate from any public problem sets.

For development, participants can use:

Problems from the Equational Theories Project and the Stage 1 public subsets.
The organizer runs offline evaluation on the private evaluation set.

Official Repository
The official GitHub repository for Stage 2:

https://github.com/SAIRcompetition/equational-theories-lean-stage2
This repository includes:

the evaluation pipeline (proxy, runner, judge)
demo solvers organized by track under examples/{solo,marathon}/demos/ (Solo: baseline/, oss_twophase/, oss_opnorm/; Marathon: baseline/, triage_oss/, fewshot_oss/)
a step-by-step tutorial per track (examples/solo/TUTORIAL.md, examples/marathon/TUTORIAL.md)
local testing support via scripts/run_harness.py (Solo) and scripts/run_marathon_harness.py (Marathon)
Local Testing
The repository supports full local testing before submission. A typical workflow:

Run bash scripts/setup.sh (one-time environment setup).
Source the environment: source .env.judge.
Study the demo solvers (start with examples/solo/demos/baseline/) and read examples/solo/TUTORIAL.md for annotated walkthroughs. For the Marathon track, see examples/marathon/TUTORIAL.md.
Test your solver locally, for example:
 
python3 -m pipeline.runner \
  --submission examples/solo/demos/baseline \
  --problems examples/problems/sample_20.json
 
Review results in pipeline/results/.
Iterate — improve deterministic strategies first, then refine your prompt.
Submit only after your solver is stable locally.