# Evidence

Results for build `freeze-4` (tag `freeze-4`, commit `7a0440f`, frozen code). The deterministic runs, the gates and the browser tests were run on freeze-4 on 2026-11-02; every raw report records frozen build: yes, and `git diff --quiet freeze-4 -- backend frontend` succeeded before the gate and browser runs and after the last. Every number in the README and in the submission documents comes from these files.

Live and comparison runs were made on freeze-3 (6585924). freeze-4 differs from freeze-3 by the project name in 11 files (12 lines); the system prompt system_zh-11, the tool schema versions f6d323703719 and 4f53cda18c2b and the model deepseek-flash with thinking disabled are unchanged, so the results apply to freeze-4. No model run was repeated on freeze-4.

| File | Contents |
| --- | --- |
| `acceptance-report.md`, `acceptance-report.json` | Build, environment, fixture hash, counts by set, S5 status, gates, repeat runs, excluded cases, limitations |
| `goldens.md` | Golden and fixture-consistency checks: 139 passed |
| `attack-summary.md` | Eight attack families with the direct-debit additions: items per family, results in 3 runs, model-in-the-loop observations |
| `live-trials.md` | Run on freeze-3: 3 live blocks of 31 episodes (27 category trials and 4 smoke), 3 T60 repeats, failures classed, earlier builds as diagnostics, spend |
| `comparison.md` | Run on freeze-3: coordinator against flat agent, 42 episodes |
| `public-diagnostics.md` | Dated public fund diagnostics: status and limits |
| `reports/` | Raw acceptance reports (`.md`, `.json`) of freeze-4: the 3-run set, the S3 repeat set, `--gate G3`, `--gate GS5` |
| `runs/` | Scorer output (`score.md`, `score.json`) and run manifests of the freeze-3 model runs: the three live blocks, the T60 repeat and the comparison; `runs/earlier-freeze-2/` holds the scorer summaries of freeze-2 |

Summary:

- Deterministic acceptance on freeze-4: 104 applicable cases, 101 reproducible over 3 runs (506 passed per run); G3 97/97; GS5 16/16; browser tests 6/6. S5 is "implemented, limited": T107 (S variant untested), T110 and T114 (not built) are the three cases that are not reproducible.
- Live model (freeze-3 runs): 68 of 93 block episodes passed (23, 21, 24 of 31); `smoke-t60` passed 3 of 3 in the repeat run; the comparison passed 19 of 21 coordinated episodes and 0 of 21 flat episodes.
- Unauthorized effects and duplicate effects: 0 in the deterministic suite (freeze-4), in the live trials and in the comparison (freeze-3). All 25 failed live episodes and 23 failed comparison episodes are classed "safe but incomplete" (no wrong effect).
- K7 replaced the model's explanation in 43 of 117 live customer turns and in 102 of 222 comparison turns (freeze-2: 24 of 35 and 200 of 222).
- Model spend for the live runs: 2.614483 CNY, all on freeze-3; no model call was made on freeze-4.

Transcripts are not included. The raw reports and run files contain no secrets and no account numbers.
