# Acceptance report

- Build: `freeze-4`; frozen build: yes (tag: freeze-4)
- Generated (UTC): 2026-11-02T08:30:33Z; runs: 3
- Selection: gate -; only T39, T46, T47, T48; layers D, H, K, S
- Environment: Python 3.12.16, pytest 9.2.0, Linux-6.18.54.1-microsoft-standard-WSL2-x86_64-with-glibc2.39
- Fixture hash: `11049f3d1e62dff51695cf20c14cb236a36b71e457d7b75f4338448bd2fcac04`
- Features: s5

## Totals by set

| Set | Cases | P0 | Applicable | Implemented | Passed | Reproducible | Not applicable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | 16 | 7 | 15 | 0 | 0 | 0 | 1 |
| S2 | 22 | 8 | 21 | 0 | 0 | 0 | 1 |
| S3 | 21 | 12 | 21 | 4 | 4 | 4 | 0 |
| S6 | 18 | 9 | 18 | 0 | 0 | 0 | 0 |
| S4 | 19 | 9 | 0 | 0 | 0 | 0 | 19 |
| S5 | 19 | 8 | 19 | 0 | 0 | 0 | 0 |
| R | 10 | 0 | 10 | 0 | 0 | 0 | 0 |

## Cases run

| Case | Set | P | Status | Layers | Variants |
| --- | --- | --- | --- | --- | --- |
| T39 | S3 | P0 | reproducible | D passed, H passed | 4 |
| T46 | S3 | P0 | reproducible | D passed, H passed | 6 |
| T47 | S3 | P0 | reproducible | D passed, H passed, S passed | 6 |
| T48 | S3 | P0 | reproducible | K passed | 4 |

## Not applicable

- T14: ADD_PAYEE is not built (SPEC §25)
- T24: Out of scope: no credit card in the fixture
- T78–T96: S4 unscheduled: not started
