# Evidence

Results for build `freeze-3` (tag `freeze-3`, commit `6585924`, frozen code) from 2026-11-01. `git diff --quiet freeze-3 -- backend frontend` succeeded before the first run and after the last. Every number in the README and in the submission documents comes from these files.

| File | Contents |
| --- | --- |
| `acceptance-report.md`, `acceptance-report.json` | Build, environment, fixture hash, counts by set, S5 status, gates, repeat runs, excluded cases, limitations |
| `goldens.md` | Golden and fixture-consistency checks: 139 passed |
| `attack-summary.md` | Eight attack families with the direct-debit additions: items per family, results in 3 runs, model-in-the-loop observations |
| `live-trials.md` | 3 live blocks of 31 episodes (27 category trials and 4 smoke), 3 T60 repeats, failures classed, earlier builds as diagnostics, spend |
| `comparison.md` | Coordinator against flat agent, 42 episodes |
| `public-diagnostics.md` | Dated public fund diagnostics: status and limits |
| `reports/` | Raw acceptance reports (`.md`, `.json`): the 3-run set, the S3 repeat set, `--gate G3`, `--gate GS5` |
| `runs/` | Scorer output (`score.md`, `score.json`) and run manifests for the three live blocks, the T60 repeat and the comparison; `runs/earlier-freeze-2/` holds the scorer summaries of the earlier build |

Summary:

- Deterministic acceptance on the frozen build: 104 applicable cases, 101 reproducible over 3 runs (506 passed per run); G3 97/97; GS5 16/16; browser tests 6/6. S5 is "implemented, limited": T107 (S variant untested), T110 and T114 (not built) are the three cases that are not reproducible.
- Live model: 68 of 93 block episodes passed (23, 21, 24 of 31); `smoke-t60` passed 3 of 3 in the repeat run; the comparison passed 19 of 21 coordinated episodes and 0 of 21 flat episodes.
- Unauthorized effects and duplicate effects: 0 in the deterministic suite, in the live trials and in the comparison. All 25 failed live episodes and 23 failed comparison episodes are classed "safe but incomplete" (no wrong effect).
- K7 replaced the model's explanation in 43 of 117 live customer turns and in 102 of 222 comparison turns (freeze-2: 24 of 35 and 200 of 222).
- Model spend for the live runs: 2.614483 CNY.

Transcripts are not included. The raw reports and run files contain no secrets and no account numbers.
