# Acceptance report

- Build: `freeze-4`; frozen build: yes (tag: freeze-4)
- Generated (UTC): 2026-11-02T08:50:17Z; runs: 1
- Selection: gate GS5; only all; layers D, H, K, S
- Environment: Python 3.12.16, pytest 9.2.0, Linux-6.18.54.1-microsoft-standard-WSL2-x86_64-with-glibc2.39
- Fixture hash: `11049f3d1e62dff51695cf20c14cb236a36b71e457d7b75f4338448bd2fcac04`
- Features: s5

## Gate GS5: passed

Required case-layer pairs: 16; passed 16; failed 0; missing 0.

## Totals by set

| Set | Cases | P0 | Applicable | Implemented | Passed | Reproducible | Not applicable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | 16 | 7 | 15 | 0 | 0 | 0 | 1 |
| S2 | 22 | 8 | 21 | 0 | 0 | 0 | 1 |
| S3 | 21 | 12 | 21 | 0 | 0 | 0 | 0 |
| S6 | 18 | 9 | 18 | 0 | 0 | 0 | 0 |
| S4 | 19 | 9 | 0 | 0 | 0 | 0 | 19 |
| S5 | 19 | 8 | 19 | 9 | 8 | 0 | 0 |
| R | 10 | 0 | 10 | 0 | 0 | 0 | 0 |

## Cases run

| Case | Set | P | Status | Layers | Variants |
| --- | --- | --- | --- | --- | --- |
| T1 | S1 | P0 | specified | D missing, H missing, S missing | 0 |
| T2 | S1 | P1 | specified | S missing | 0 |
| T3 | S1 | P1 | specified | S missing | 0 |
| T4 | S1 | P1 | specified | S missing | 0 |
| T5 | S1 | P0 | specified | D missing, H missing | 0 |
| T6 | S1 | P0 | specified | D missing, H missing | 0 |
| T7 | S1 | P0 | specified | D missing, H missing, S missing | 0 |
| T8 | S1 | P1 | specified | D missing, H missing | 0 |
| T9 | S1 | P0 | specified | D missing, H missing, S missing | 0 |
| T10 | S1 | P0 | specified | D missing, H missing, S missing | 0 |
| T11 | S1 | P0 | specified | K missing | 0 |
| T12 | S1 | P1 | specified | D missing, H missing | 0 |
| T13 | S1 | P2 | specified | D missing, H missing | 0 |
| T15 | S1 | P1 | specified | S missing | 0 |
| T16 | S1 | P2 | specified | S missing | 0 |
| T17 | S2 | P0 | specified | D missing, H missing | 0 |
| T18 | S2 | P0 | specified | D missing, H missing | 0 |
| T19 | S2 | P1 | specified | D missing | 0 |
| T20 | S2 | P1 | specified | D missing | 0 |
| T21 | S2 | P0 | specified | D missing, H missing | 0 |
| T22 | S2 | P0 | specified | D missing, H missing | 0 |
| T23 | S2 | P2 | specified | D missing | 0 |
| T25 | S2 | P0 | specified | D missing, H missing | 0 |
| T26 | S2 | P0 | specified | D missing, H missing | 0 |
| T27 | S2 | P1 | specified | S missing | 0 |
| T28 | S2 | P2 | specified | S missing | 0 |
| T29 | S2 | P1 | specified | D missing, H missing, S missing | 0 |
| T30 | S2 | P1 | specified | S missing | 0 |
| T31 | S2 | P1 | specified | S missing | 0 |
| T32 | S2 | P1 | specified | D missing, H missing | 0 |
| T33 | S2 | P0 | specified | D missing, H missing, K missing | 0 |
| T34 | S2 | P2 | specified | D missing, H missing | 0 |
| T35 | S2 | P0 | specified | D missing, H missing | 0 |
| T36 | S2 | P1 | specified | D missing | 0 |
| T37 | S2 | P1 | specified | S missing | 0 |
| T38 | S2 | P1 | specified | D missing, H missing, S missing | 0 |
| T39 | S3 | P0 | specified | D missing, H missing | 0 |
| T40 | S3 | P1 | specified | S missing | 0 |
| T41 | S3 | P1 | specified | S missing | 0 |
| T42 | S3 | P1 | specified | D missing, H missing | 0 |
| T43 | S3 | P2 | specified | D missing | 0 |
| T44 | S3 | P2 | specified | D missing, S missing | 0 |
| T45 | S3 | P0 | specified | D missing, H missing | 0 |
| T46 | S3 | P0 | specified | D missing, H missing | 0 |
| T47 | S3 | P0 | specified | D missing, H missing, S missing | 0 |
| T48 | S3 | P0 | specified | K missing | 0 |
| T49 | S3 | P0 | specified | D missing, H missing | 0 |
| T50 | S3 | P0 | specified | D missing, H missing | 0 |
| T51 | S3 | P1 | specified | S missing | 0 |
| T52 | S3 | P1 | specified | D missing, H missing | 0 |
| T53 | S3 | P0 | specified | D missing, H missing | 0 |
| T54 | S3 | P0 | specified | H missing | 0 |
| T55 | S3 | P1 | specified | D missing | 0 |
| T56 | S3 | P0 | specified | D missing | 0 |
| T57 | S3 | P0 | specified | D missing, H missing | 0 |
| T58 | S3 | P1 | specified | D missing | 0 |
| T59 | S3 | P0 | specified | H missing | 0 |
| T60 | S6 | P0 | specified | D missing, H missing | 0 |
| T61 | S6 | P0 | specified | D missing, H missing | 0 |
| T62 | S6 | P0 | specified | D missing, H missing | 0 |
| T63 | S6 | P1 | specified | D missing | 0 |
| T64 | S6 | P1 | specified | D missing | 0 |
| T65 | S6 | P2 | specified | D missing | 0 |
| T66 | S6 | P0 | specified | K missing | 0 |
| T67 | S6 | P0 | specified | K missing | 0 |
| T68 | S6 | P1 | specified | D missing | 0 |
| T69 | S6 | P2 | specified | D missing | 0 |
| T70 | S6 | P1 | specified | D missing, H missing | 0 |
| T71 | S6 | P0 | specified | D missing, H missing, S missing | 0 |
| T72 | S6 | P1 | specified | D missing, S missing | 0 |
| T73 | S6 | P0 | specified | D missing, H missing | 0 |
| T74 | S6 | P0 | specified | D missing, H missing, K missing | 0 |
| T75 | S6 | P1 | specified | S missing | 0 |
| T76 | S6 | P0 | specified | D missing, H missing | 0 |
| T77 | S6 | P1 | specified | D missing, K missing | 0 |
| T97 | S5 | P1 | specified | S missing | 0 |
| T98 | S5 | P0 | passed | D passed, H passed | 12 |
| T99 | S5 | P0 | passed | D passed, H passed | 14 |
| T100 | S5 | P0 | passed | D passed, H passed | 24 |
| T101 | S5 | P0 | implemented | D passed, H passed, S missing | 8 |
| T102 | S5 | P0 | passed | D passed, H passed | 14 |
| T103 | S5 | P0 | passed | D passed, H passed | 16 |
| T104 | S5 | P0 | passed | D passed, H passed | 12 |
| T105 | S5 | P0 | passed | K passed | 5 |
| T106 | S5 | P1 | specified | D missing, H missing | 0 |
| T107 | S5 | P1 | specified | D missing, H missing, S missing | 0 |
| T108 | S5 | P1 | specified | D missing, H missing, S missing | 0 |
| T109 | S5 | P1 | specified | S missing | 0 |
| T110 | S5 | P1 | specified | D missing | 0 |
| T111 | S5 | P1 | specified | D missing, H missing | 0 |
| T112 | S5 | P1 | specified | D missing | 0 |
| T113 | S5 | P2 | specified | S missing | 0 |
| R1 | R | - | specified | D missing | 0 |
| R2 | R | - | specified | D missing | 0 |
| R3 | R | - | specified | D missing, K missing | 0 |
| R4 | R | - | specified | D missing, K missing | 0 |
| R5 | R | - | specified | D missing | 0 |
| R6 | R | - | specified | D missing | 0 |
| R7 | R | - | specified | D missing | 0 |
| R8 | R | - | specified | D missing | 0 |
| R9 | R | - | specified | K missing | 0 |
| R10 | R | - | specified | D missing | 0 |
| R-S5-1 | S5 | - | passed | D passed | 5 |

## Not applicable

- T14: ADD_PAYEE is not built (SPEC §25)
- T24: Out of scope: no credit card in the fixture
- T78–T96: S4 unscheduled: not started
