# Evidence

Results for build `freeze-1-2-g960ab88` (tag `freeze-1`, frozen code) from 2026-10-31. Every number in the README and in the submission documents comes from these files.

| File | Contents |
| --- | --- |
| `acceptance-report.md`, `acceptance-report.json` | Build, environment, fixture hash, counts by set, gates, repeat runs, excluded cases, limitations |
| `goldens.md` | Golden and fixture-consistency checks: 132 passed |
| `attack-summary.md` | Eight attack families: items per family, results in 3 runs, model-in-the-loop observations |
| `live-trials.md` | 24 category trials, 4 smoke episodes and 3 T60 repeats with the live model |
| `comparison.md` | Coordinator against flat agent, 42 episodes |
| `public-diagnostics.md` | Dated public fund diagnostics: status and limits |
| `reports/` | Raw acceptance reports (`.md`, `.json`): the clean 3-run set, the S3 repeat set, the G3 run, and the earlier 3-run set that contains one startup error |
| `runs/` | Scorer output (`score.md`, `score.json`) and run manifests for the live trials, the T60 repeat and the comparison |

Summary:

- Deterministic acceptance on the frozen build: 85 applicable cases, all reproducible over 3 runs (307 passed per run); G3 passed 97/97.
- Live model: 13 of 28 live episodes passed; the comparison passed 0 of 42 episodes on both systems. The release criterion of 3 consecutive live passes for T60, T61 and T66 is not met.
- Unauthorized effects: 0 in the deterministic suite and in the live trials; the comparison scorer reports 9, each executed under an unrevoked plan approval (`comparison.md`).
- Model spend for the live runs: 1.908017 CNY.

Transcripts are not included. The raw reports and run files contain no secrets and no account numbers.
