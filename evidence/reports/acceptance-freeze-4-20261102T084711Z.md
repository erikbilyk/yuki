# Acceptance report

- Build: `freeze-4`; frozen build: yes (tag: freeze-4)
- Generated (UTC): 2026-11-02T08:47:11Z; runs: 1
- Selection: gate G3; only all; layers D, H, K, S
- Environment: Python 3.12.16, pytest 9.2.0, Linux-6.18.54.1-microsoft-standard-WSL2-x86_64-with-glibc2.39
- Fixture hash: `11049f3d1e62dff51695cf20c14cb236a36b71e457d7b75f4338448bd2fcac04`
- Features: s5

## Gate G3: passed

Required case-layer pairs: 97; passed 97; failed 0; missing 0.
Not run by this harness (U, L): T1/L, T17/L, T39/L, T60/L, T60/U.

## Totals by set

| Set | Cases | P0 | Applicable | Implemented | Passed | Reproducible | Not applicable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | 16 | 7 | 15 | 15 | 15 | 0 | 1 |
| S2 | 22 | 8 | 21 | 21 | 21 | 0 | 1 |
| S3 | 21 | 12 | 21 | 21 | 21 | 0 | 0 |
| S6 | 18 | 9 | 18 | 18 | 18 | 0 | 0 |
| S4 | 19 | 9 | 0 | 0 | 0 | 0 | 19 |
| S5 | 19 | 8 | 19 | 0 | 0 | 0 | 0 |
| R | 10 | 0 | 10 | 10 | 10 | 0 | 0 |

## Cases run

| Case | Set | P | Status | Layers | Variants |
| --- | --- | --- | --- | --- | --- |
| T1 | S1 | P0 | passed | D passed, H passed, S passed | 3 |
| T2 | S1 | P1 | passed | S passed | 3 |
| T3 | S1 | P1 | passed | S passed | 2 |
| T4 | S1 | P1 | passed | S passed | 2 |
| T5 | S1 | P0 | passed | D passed, H passed | 8 |
| T6 | S1 | P0 | passed | D passed, H passed | 4 |
| T7 | S1 | P0 | passed | D passed, H passed, S passed | 13 |
| T8 | S1 | P1 | passed | D passed, H passed | 2 |
| T9 | S1 | P0 | passed | D passed, H passed, S passed | 20 |
| T10 | S1 | P0 | passed | D passed, H passed, S passed | 9 |
| T11 | S1 | P0 | passed | K passed | 4 |
| T12 | S1 | P1 | passed | D passed, H passed | 6 |
| T13 | S1 | P2 | passed | D passed, H passed | 8 |
| T15 | S1 | P1 | passed | S passed | 4 |
| T16 | S1 | P2 | passed | S passed | 2 |
| T17 | S2 | P0 | passed | D passed, H passed | 2 |
| T18 | S2 | P0 | passed | D passed, H passed | 6 |
| T19 | S2 | P1 | passed | D passed | 1 |
| T20 | S2 | P1 | passed | D passed | 1 |
| T21 | S2 | P0 | passed | D passed, H passed | 4 |
| T22 | S2 | P0 | passed | D passed, H passed | 2 |
| T23 | S2 | P2 | passed | D passed | 2 |
| T25 | S2 | P0 | passed | D passed, H passed | 6 |
| T26 | S2 | P0 | passed | D passed, H passed | 8 |
| T27 | S2 | P1 | passed | S passed | 3 |
| T28 | S2 | P2 | passed | S passed | 2 |
| T29 | S2 | P1 | passed | D passed, H passed, S passed | 12 |
| T30 | S2 | P1 | passed | S passed | 5 |
| T31 | S2 | P1 | passed | S passed | 2 |
| T32 | S2 | P1 | passed | D passed, H passed | 4 |
| T33 | S2 | P0 | passed | D passed, H passed, K passed | 6 |
| T34 | S2 | P2 | passed | D passed, H passed | 4 |
| T35 | S2 | P0 | passed | D passed, H passed | 6 |
| T36 | S2 | P1 | passed | D passed | 1 |
| T37 | S2 | P1 | passed | S passed | 3 |
| T38 | S2 | P1 | passed | D passed, H passed, S passed | 3 |
| T39 | S3 | P0 | passed | D passed, H passed | 4 |
| T40 | S3 | P1 | passed | S passed | 2 |
| T41 | S3 | P1 | passed | S passed | 2 |
| T42 | S3 | P1 | passed | D passed, H passed | 4 |
| T43 | S3 | P2 | passed | D passed | 1 |
| T44 | S3 | P2 | passed | D passed, S passed | 5 |
| T45 | S3 | P0 | passed | D passed, H passed | 2 |
| T46 | S3 | P0 | passed | D passed, H passed | 6 |
| T47 | S3 | P0 | passed | D passed, H passed, S passed | 6 |
| T48 | S3 | P0 | passed | K passed | 4 |
| T49 | S3 | P0 | passed | D passed, H passed | 2 |
| T50 | S3 | P0 | passed | D passed, H passed | 6 |
| T51 | S3 | P1 | passed | S passed | 2 |
| T52 | S3 | P1 | passed | D passed, H passed | 2 |
| T53 | S3 | P0 | passed | D passed, H passed | 4 |
| T54 | S3 | P0 | passed | H passed | 1 |
| T55 | S3 | P1 | passed | D passed | 3 |
| T56 | S3 | P0 | passed | D passed | 2 |
| T57 | S3 | P0 | passed | D passed, H passed | 6 |
| T58 | S3 | P1 | passed | D passed | 2 |
| T59 | S3 | P0 | passed | H passed | 1 |
| T60 | S6 | P0 | passed | D passed, H passed | 2 |
| T61 | S6 | P0 | passed | D passed, H passed | 2 |
| T62 | S6 | P0 | passed | D passed, H passed | 2 |
| T63 | S6 | P1 | passed | D passed | 1 |
| T64 | S6 | P1 | passed | D passed | 1 |
| T65 | S6 | P2 | passed | D passed | 1 |
| T66 | S6 | P0 | passed | K passed | 4 |
| T67 | S6 | P0 | passed | K passed | 2 |
| T68 | S6 | P1 | passed | D passed | 1 |
| T69 | S6 | P2 | passed | D passed | 1 |
| T70 | S6 | P1 | passed | D passed, H passed | 2 |
| T71 | S6 | P0 | passed | D passed, H passed, S passed | 3 |
| T72 | S6 | P1 | passed | D passed, S passed | 3 |
| T73 | S6 | P0 | passed | D passed, H passed | 2 |
| T74 | S6 | P0 | passed | D passed, H passed, K passed | 3 |
| T75 | S6 | P1 | passed | S passed | 3 |
| T76 | S6 | P0 | passed | D passed, H passed | 2 |
| T77 | S6 | P1 | passed | D passed, K passed | 2 |
| T97 | S5 | P1 | specified | S missing | 0 |
| T98 | S5 | P0 | specified | D missing, H missing | 0 |
| T99 | S5 | P0 | specified | D missing, H missing | 0 |
| T100 | S5 | P0 | specified | D missing, H missing | 0 |
| T101 | S5 | P0 | specified | D missing, H missing, S missing | 0 |
| T102 | S5 | P0 | specified | D missing, H missing | 0 |
| T103 | S5 | P0 | specified | D missing, H missing | 0 |
| T104 | S5 | P0 | specified | D missing, H missing | 0 |
| T105 | S5 | P0 | specified | K missing | 0 |
| T106 | S5 | P1 | specified | D missing, H missing | 0 |
| T107 | S5 | P1 | specified | D missing, H missing, S missing | 0 |
| T108 | S5 | P1 | specified | D missing, H missing, S missing | 0 |
| T109 | S5 | P1 | specified | S missing | 0 |
| T110 | S5 | P1 | specified | D missing | 0 |
| T111 | S5 | P1 | specified | D missing, H missing | 0 |
| T112 | S5 | P1 | specified | D missing | 0 |
| T113 | S5 | P2 | specified | S missing | 0 |
| R1 | R | - | passed | D passed | 1 |
| R2 | R | - | passed | D passed | 2 |
| R3 | R | - | passed | D passed, K passed | 3 |
| R4 | R | - | passed | D passed, K passed | 5 |
| R5 | R | - | passed | D passed | 4 |
| R6 | R | - | passed | D passed | 1 |
| R7 | R | - | passed | D passed | 2 |
| R8 | R | - | passed | D passed | 4 |
| R9 | R | - | passed | K passed | 2 |
| R10 | R | - | passed | D passed | 5 |
| R-S5-1 | S5 | - | specified | D missing | 0 |

## Not applicable

- T14: ADD_PAYEE is not built (SPEC §25)
- T24: Out of scope: no credit card in the fixture
- T78–T96: S4 unscheduled: not started
