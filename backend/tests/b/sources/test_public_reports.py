"""Public report parsers: synthetic tables, and the cached PDFs when they are present."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from wg.sources import fees, holdings, overlap, public, reconcile

PAGE_A = """\
7.3 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细
金额单位：人民币元
序号 股票代码 股票名称 数量（股） 公允价值 比例(％)
1 300308 中际旭创 100 6,000.00 60.00
2 600183 生益科技 200 3,000.00 30.00
第1页共2页
"""
PAGE_A2 = """\
3 000001 平安银行 50 1,000.00 10.00
7.4 报告期内股票投资组合的重大变动
1 999999 不应读取 1 1.00 0.00
"""
PAGE_B = """\
7.3 期末按公允价值占基金资产净值比例大小排序的股票投资明细
7.3.1 期末指数投资按公允价值占基金资产净值比例大小排序的所有股票投资明细
1 300308 中际旭创 10 2,000.00 20.00
2 00981 中芯国际 10 1,000.00 10.00
7.3.2 期末积极投资按公允价值占基金资产净值比例大小排序的所有股票投资明细
1 300308 中际旭创 10 1,000.00 10.00
7.4 报告期内股票投资组合的重大变动
"""


def test_to_fen_is_exact():
    assert holdings.to_fen("1,287,711,652.00") == 128771165200
    assert holdings.to_fen("0.05") == 5
    with pytest.raises(holdings.ReportFormatError):
        holdings.to_fen("1.234")


def test_parse_holdings_reads_rows_across_pages_and_stops_at_7_4():
    rows = holdings.parse_holdings([PAGE_A, PAGE_A2])
    assert [r.security_code for r in rows] == ["300308", "600183", "000001"]
    assert rows[0].value_fen == 600000 and rows[0].page == 1
    assert rows[2].page == 2
    assert rows[1].nav_pct == Decimal("30.00")


def test_parse_holdings_keeps_sections_and_marks_hong_kong():
    rows = holdings.parse_holdings([PAGE_B])
    assert [(r.section, r.seq) for r in rows] == [("7.3.1", 1), ("7.3.1", 2), ("7.3.2", 1)]
    assert [r.is_hong_kong for r in rows] == [False, True, False]


def test_parse_holdings_without_table_fails():
    with pytest.raises(holdings.ReportFormatError):
        holdings.parse_holdings(["nothing here"])


def test_sequence_check_detects_a_dropped_row():
    rows = holdings.parse_holdings([PAGE_A, PAGE_A2])
    assert all(c.ok for c in reconcile.sequence_check(rows))
    assert not all(c.ok for c in reconcile.sequence_check([rows[0], rows[2]]))


def test_stated_pct_rounds_half_up():
    assert holdings.stated_pct(5, 1000) == Decimal("0.50")
    assert holdings.stated_pct(1, 800) == Decimal("0.13")


def test_overlap_sums_sleeves_and_takes_the_smaller_weight():
    rows_a = holdings.parse_holdings([PAGE_A, PAGE_A2])
    rows_b = holdings.parse_holdings([PAGE_B])
    shared = overlap.overlap_rows(rows_a, 1000000, rows_b, 1000000)
    assert [r.security_code for r in shared] == ["300308"]
    assert shared[0].value_fen_b == 300000
    assert shared[0].shared_weight == Decimal("0.3")
    assert overlap.overlap_percent(shared) == Decimal("30.000000")


def test_overlap_uses_each_funds_own_net_assets():
    rows_a = holdings.parse_holdings([PAGE_A, PAGE_A2])
    rows_b = holdings.parse_holdings([PAGE_B])
    shared = overlap.overlap_rows(rows_a, 2000000, rows_b, 1000000)
    assert shared[0].weight_a == Decimal("0.3") and shared[0].weight_b == Decimal("0.3")
    shared = overlap.overlap_rows(rows_a, 800000, rows_b, 1000000)
    assert shared[0].weight_a == Decimal("0.75")
    assert shared[0].shared_weight == Decimal("0.3")


SUMMARY = """\
下属基金简称 易方达沪深 300 精选增强 C 下属基金代码 010737
费用类型 份额（S）或金额（M） 收费方式
0天 < N ≤ 6天 1.50%
赎回费 7天 ≤ N ≤ 29天 0.50%
N ≥ 30天 0.00%
"""


def test_fee_tiers_use_whole_days():
    tiers = fees.parse_redemption_tiers([SUMMARY])
    assert [(t.from_days, t.to_days, t.rate) for t in tiers] == [
        (1, 6, Decimal("0.015")),
        (7, 29, Decimal("0.005")),
        (30, None, Decimal("0")),
    ]
    assert fees.parse_share_class_code([SUMMARY]) == "010737"


def test_fee_tiers_missing_row_fails():
    with pytest.raises(holdings.ReportFormatError):
        fees.parse_redemption_tiers(["0天 < N ≤ 6天 1.50%"])


RAW = public.PUBLIC_DIR / "raw"
needs_pdfs = pytest.mark.skipif(not RAW.is_dir(), reason="cached PDFs not present")


@pytest.fixture(scope="module")
def cached_funds() -> dict[str, public.FundReport]:
    pytest.importorskip("pdfplumber")
    manifest = public.load_manifest(public.PUBLIC_DIR)
    try:
        funds = [public.load_fund(RAW, entry) for entry in manifest["funds"]]
    except public.IdentityError as error:
        if str(error).startswith("missing cached file"):
            pytest.skip(str(error))
        raise
    return {fund.code: fund for fund in funds}


@needs_pdfs
def test_cached_reports_reconcile_to_the_fen(cached_funds):
    for code, checks in public.run_checks(list(cached_funds.values())).items():
        failed = [c.name for c in checks if not c.ok]
        assert not failed, f"{code}: {failed}"


@needs_pdfs
def test_cached_reports_reproduce_the_documented_overlap(cached_funds, tmp_path):
    funds = [cached_funds["019018"], cached_funds["010737"]]
    percent = public.write_overlap(tmp_path, "2026-06-30", *funds)
    assert percent == public.EXPECTED_OVERLAP
    assert (tmp_path / "overlap.csv").read_text(encoding="utf-8").count("\n") == 62


@needs_pdfs
def test_cached_row_counts_and_pages(cached_funds):
    info = cached_funds["019018"].rows
    assert len(info) == 128 and {r.page for r in info} == {43, 44, 45, 46}
    enhanced = cached_funds["010737"].rows
    assert len(enhanced) == 108 and {r.section for r in enhanced} == {"7.3.1", "7.3.2"}


@needs_pdfs
def test_extract_output_is_deterministic(tmp_path):
    for name in ("reports.yaml",):
        (tmp_path / name).write_bytes((public.PUBLIC_DIR / name).read_bytes())
    (tmp_path / "raw").symlink_to(RAW)
    assert public.main(["extract", "--public-dir", str(tmp_path)]) == 0
    for code in ("019018", "010737"):
        name = f"holdings_{code}.csv"
        assert (tmp_path / name).read_bytes() == (public.PUBLIC_DIR / name).read_bytes()


def test_wrong_hash_is_rejected(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "x.pdf").write_bytes(b"not the report")
    entry = {"file": "x.pdf", "sha256": "0" * 64}
    with pytest.raises(public.IdentityError, match="SHA-256 mismatch"):
        public._verified_pages(raw, entry)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(public.IdentityError, match="missing cached file"):
        public._verified_pages(Path(tmp_path), {"file": "none.pdf", "sha256": "0" * 64})
