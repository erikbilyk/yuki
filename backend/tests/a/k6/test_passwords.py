from wg.k6.passwords import hash_password, verify_password


def test_hash_is_salted_and_verifies():
    first, second = hash_password("wg-demo"), hash_password("wg-demo")
    assert first != second
    assert "wg-demo" not in first
    assert verify_password("wg-demo", first) and verify_password("wg-demo", second)
    assert not verify_password("wg-demo2", first)


def test_malformed_hash_never_verifies():
    for stored in ("", "plain", "md5$1$2$3$aa$bb", "scrypt$16384$8$1$zz$yy"):
        assert not verify_password("x", stored)
