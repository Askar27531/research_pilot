# P9 Evaluation and Release Validation

## Release gates

| Gate | Command | Acceptance |
|---|---|---|
| 20-item regression eval | `python -m evals.run_researchpilot_eval` | 20 tasks, four categories, 100% pass rate |
| Fixed offline demo | `python -m scripts.run_fixed_demo` | all six release scenarios pass |
| Full tests | `python -m pytest -q` | no failures |
| Static checks | `python -m ruff check .` | no findings |
| Dependency consistency | `python -m pip check` | no broken requirements |
| Clean install | create a new venv, install `.[dev]`, rerun gates | succeeds without the development venv |

## Evaluation contract

`evals/datasets/researchpilot_eval_v1.json` contains exactly 20 versioned tasks: Search ×5, Figure ×5, Evidence ×5 and Experiment ×5. Experiment references contain two reviewer scores. Every run records dataset version, time, Python/platform, model configuration, parameters, per-item scores and aggregates in `evals/snapshots/researchpilot_eval_v1_result.json`.

The suite is a deterministic regression snapshot: stored predictions are scored against frozen references. Its score detects contract/scorer/snapshot regressions, but does **not** estimate fresh online retrieval or model quality. The separate literature Gold Set command, `python -m evals.run_literature_eval`, exercises saved OpenAlex relevance acceptance data. A new online claim must record the OpenAlex response set, Ollama model digest and human labels instead of reusing the regression number.

## Ablation interpretation

The Skills and compact-context entries in dataset v1 are frozen reference fixtures used to keep the comparison schema and reporting stable. They are not rerun causal experiments in the release gate and must not be presented as new measurements. Before publishing model-quality claims, rerun both arms with the same prompts, model digest, sampling parameters and evaluator, then replace the fixture values with raw run artifacts.

## Reproducibility

The runner is deterministic for a given dataset file. `run_id` and `started_at` intentionally vary; aggregate and item metrics must remain identical. When the source directory is not a Git checkout, `git_commit` is recorded as `unavailable` rather than fabricated.

## Validated release result (2026-08-09)

- Clean Python 3.12 environment: editable `.[dev]` install succeeded; `pip check` reported no broken requirements; 8 clean-environment smoke tests passed.
- Fixed offline demo: 12 tests passed across all six scenarios in 5.97 seconds.
- Full suite: 77 tests passed in 8.20 seconds.
- Regression evaluation: 20/20 passed; overall frozen-snapshot score 0.99.
- Exact resolved dependencies are captured in `requirements-lock.txt`.
