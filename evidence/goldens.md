# Golden values

Build `freeze-1-2-g960ab88` (tag `freeze-1`, frozen build: yes), run on 2026-10-31 (CST) with `cd backend && uv run pytest tests/b/goldens`: **132 passed, 0 failed**.

The expected values are in `backend/tests/b/goldens/expected.yaml`, recomputed independently from the fixture inputs (version 1). Money is integer fen; percentages and shares are strings with four decimal places. The checks compare fixture and engine output to the fen and to four decimal places of a percent, except values marked `approx`, which are compared to the stated precision.

| Test file | Tests | What it checks |
| --- | --- | --- |
| `test_cash_goldens.py` | 41 | Cash engine against the cash values |
| `test_funds_goldens.py` | 23 | Funds engine against the fund values |
| `test_revision_goldens.py` | 9 | Purchase revision sequence |
| `test_fixture_consistency.py` | 59 | The fixture values agree with `expected.yaml` |

## Values quoted by the acceptance cases

| Value | Expected |
| --- | --- |
| Start: posted cash C / protected / headroom | ¥18,000.00 / ¥16,000.00 / ¥2,000.00 |
| After reserve raised to ¥5,000.00 | protected ¥17,000.00, headroom ¥1,000.00 |
| After rent ¥3,400.00 paid | C ¥14,600.00, protected ¥13,600.00, headroom ¥1,000.00 |
| Technology share before the plan | 45.5000% of ¥50,000.00 |
| Redemption of fund C, 5,000.0000 shares | gross ¥5,000.00, fee ¥15.00, net ¥4,985.00 |
| Plan purchase of fund D | ¥4,985.00; technology share at the checkpoint 39.5089% |
| Revision after bills ¥800 then ¥400 | purchase ¥4,985.00 → ¥4,785.00, share 39.6274% |
| Third increase (¥400) | pause; headroom ¥4,385.00 |
| Purchase ¥4,385.00 hypothetical | share 39.8674% (outside the approved bounds) |
| No purchase | share 42.7778% |
| Swap C to D, ¥4,000.00 | 40.7074%, does not reach 40% |
| Public overlap of funds 019018 and 010737 | 26.709382% over 61 shared securities |

The same values are exercised end to end by the S3 and S6 acceptance cases (T39 to T77), which passed in all 3 repeat runs (`acceptance-report.md`).
