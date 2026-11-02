# Public fund diagnostics

Status: implemented, read-only, never executable.

- Inputs: the 2026 interim reports (period end 2026-06-30) of funds 010737 and 019018 and their product summaries, retrieved on 2026-10-29. File names and SHA-256 are recorded in `data/public/reports.yaml`; the original PDFs are not in the repository (`data/public/raw/` is ignored). Extracted holdings tables, reconciliation results and the fee schedule are committed in `data/public/`.
- Result: overlap of the two funds 26.709382% over 61 shared securities, computed as the sum over shared securities of the smaller of the two weights (value divided by fund net assets). Fund net assets: 010737 ¥6,635,686,578.57 and 019018 ¥21,143,604,453.72.
- Import: `python -m wg.services.public_import` refuses unless every reconciliation check is ok, the hashes match `reports.yaml`, and the overlap recomputed from the holdings tables equals `overlap.json`. It stores one `public_diagnostics` result with `executable=false` and period end 2026-06-30. The route `GET /v1/funds/public-diagnostics` returns it, or 409 `DATA_INCOMPLETE` before the import.
- Tests: `backend/tests/a/api/test_public_diagnostics.py` (7 passed on `freeze-4`); T41 ("Guarantee 019018's current share") and T59 (no float in any response, including this route) reproducible in all 3 runs of the `freeze-4` set. The retrieved reports, extracted tables and the overlap figure are the data of 2026-10-29 and are not re-derived.

Limits:
- `source_url` is null for all four files in `data/public/reports.yaml`; the original download addresses were not recorded at retrieval.
- The diagnostics describe holdings as of 2026-06-30. They back no order or plan target.
- Public holdings carry no sector or issuer mapping, so they are not loaded as holdings snapshots.
