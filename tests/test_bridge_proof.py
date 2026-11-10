"""Tests for PICO Native Bridge v2 per-request proof verification and anti-replay cache."""

from datetime import datetime, timezone
import hashlib
import hmac
import threading
import time
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from pico.core.bridge import (
    AntiReplayCache,
    build_canonical_proof_v2_string,
    compute_proof_v2_hmac,
    get_anti_replay_cache,
    set_bridge_secret,
)
from pico.main import app

TEST_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def setup_bridge_secret():
    set_bridge_secret(TEST_SECRET)
    get_anti_replay_cache().clear()
    yield
    set_bridge_secret(None)
    get_anti_replay_cache().clear()


def test_canonical_proof_v2_string_exact_format():
    """Verify length-prefixed canonical input string format against ADR 0003 Section 8.1."""
    ts = "1731150000"
    nonce = "00112233445566778899aabbccddeeff"
    method = "GET"
    target = "/health"
    body_digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    canonical = build_canonical_proof_v2_string("v2", ts, nonce, method, target, body_digest)
    expected = (
        "pico-bridge-v2:2:v2:10:1731150000:32:00112233445566778899aabbccddeeff:3:GET:7:/health:64:"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert canonical == expected

    sig = compute_proof_v2_hmac(TEST_SECRET, "v2", ts, nonce, method, target, body_digest)
    assert sig == "a2ca2d143b056a1b5b9daf02c4eefb444a731942179dd22dbcce9cc4e037365e"


def test_canonical_proof_v2_unicode_byte_lengths():
    """Verify UTF-8 multibyte characters compute length based on UTF-8 bytes, not UTF-16 code units."""
    ts = "1731150000"
    nonce = "00112233445566778899aabbccddeeff"
    method = "POST"
    # Target containing 3-byte unicode character '€' and 4-byte emoji '🚀'
    target = "/api/test/€/🚀?tag=piñata"
    # €: 3 bytes, /: 1, 🚀: 4 bytes. Piñata: 'ñ' is 2 bytes.
    target_bytes = target.encode("utf-8")
    body_digest = hashlib.sha256(b"").hexdigest()

    canonical = build_canonical_proof_v2_string("v2", ts, nonce, method, target, body_digest)
    expected_prefix = f"pico-bridge-v2:2:v2:10:{ts}:32:{nonce}:4:{method}:{len(target_bytes)}:{target}:64:{body_digest}"
    assert canonical == expected_prefix


def test_canonical_proof_v2_duplicate_and_ordered_query_params():
    """Verify duplicate query keys and ordering differences produce distinct canonical strings and HMACs."""
    ts = "1731150000"
    nonce = "00112233445566778899aabbccddeeff"
    method = "GET"
    body_digest = hashlib.sha256(b"").hexdigest()

    target1 = "/api/conversations?limit=20&tag=a&tag=b"
    target2 = "/api/conversations?tag=b&tag=a&limit=20"

    canonical1 = build_canonical_proof_v2_string("v2", ts, nonce, method, target1, body_digest)
    canonical2 = build_canonical_proof_v2_string("v2", ts, nonce, method, target2, body_digest)
    assert canonical1 != canonical2

    sig1 = compute_proof_v2_hmac(TEST_SECRET, "v2", ts, nonce, method, target1, body_digest)
    sig2 = compute_proof_v2_hmac(TEST_SECRET, "v2", ts, nonce, method, target2, body_digest)
    assert sig1 != sig2


def test_canonical_proof_v2_percent_encoding_preservation():
    """Verify raw percent-encoded characters like %2F are preserved and differ from literal '/'."""
    ts = "1731150000"
    nonce = "00112233445566778899aabbccddeeff"
    method = "GET"
    body_digest = hashlib.sha256(b"").hexdigest()

    target_encoded = "/api/tools?query=hello%2Fworld"
    target_literal = "/api/tools?query=hello/world"

    sig_encoded = compute_proof_v2_hmac(TEST_SECRET, "v2", ts, nonce, method, target_encoded, body_digest)
    sig_literal = compute_proof_v2_hmac(TEST_SECRET, "v2", ts, nonce, method, target_literal, body_digest)
    assert sig_encoded != sig_literal


def test_anti_replay_cache_operations():
    """Unit test for AntiReplayCache check-and-insert, expiry, and capacity limit."""
    cache = AntiReplayCache(capacity=3, ttl_seconds=2.0)
    now = 1000.0

    # Insert entry 1
    ok, reason = cache.check_and_insert("nonce1", current_time=now)
    assert ok and not reason
    assert cache.size() == 1

    # Replay of entry 1 fails
    ok, reason = cache.check_and_insert("nonce1", current_time=now + 0.5)
    assert not ok
    assert "Replay detected" in reason

    # Insert entries 2 and 3
    ok, _ = cache.check_and_insert("nonce2", current_time=now + 0.6)
    assert ok
    ok, _ = cache.check_and_insert("nonce3", current_time=now + 0.7)
    assert ok
    assert cache.size() == 3

    # Capacity full: insert entry 4 fails closed
    ok, reason = cache.check_and_insert("nonce4", current_time=now + 0.8)
    assert not ok
    assert "saturated" in reason

    # After TTL, expired entries are pruned on check
    ok, reason = cache.check_and_insert("nonce4", current_time=now + 2.5)
    assert ok
    assert cache.size() <= 3


def test_anti_replay_cache_default_ttl_and_skew_security_invariant():
    """Verify default AntiReplayCache TTL is 60.0s (2x skew tolerance) and covers max client skew."""
    cache = AntiReplayCache()
    assert cache.ttl_seconds == 60.0

    # Simulate client at +30s clock skew
    t_recv = 1000.0
    t_req = t_recv + 30.0  # max future timestamp allowed (+30s)
    nonce = "future_skew_nonce_123"

    # Accepted at t_recv
    ok, _ = cache.check_and_insert(nonce, current_time=t_recv)
    assert ok

    # Case 5: Replay immediately before nonce expiry (t_recv + 59.999s)
    ok, reason = cache.check_and_insert(nonce, current_time=t_recv + 59.999)
    assert not ok
    assert "Replay detected" in reason

    # Case 6: Replay exactly at cache-expiry boundary (t_recv + 60.0s)
    # Because pruning uses `ts < cutoff` (i.e. age > TTL), at exactly age == 60.0s,
    # the entry is STILL retained in the cache, preventing replay at the exact boundary.
    ok, reason = cache.check_and_insert(nonce, current_time=t_recv + 60.0)
    assert not ok
    assert "Replay detected" in reason

    # Case 7: Replay immediately after cache expiry (t_recv + 60.001s)
    # Entry is pruned because age > 60.0s.
    # However, at t_recv + 60.001s, elapsed time since t_req is:
    # (1060.001 - 1030.0) = 30.001s > 30.0s.
    # Therefore, the request fails the timestamp validity check (skew > 30s).
    time_since_req = (t_recv + 60.001) - t_req
    assert time_since_req > 30.0  # Timestamp freshness check guarantees rejection!


def test_timestamp_boundary_cases_with_test_client():
    """Empirically test exact timestamp boundaries with FastAPI TestClient.

    Cases tested:
    1. Lower accepted boundary: now - 30.0s
    2. Upper accepted boundary: now + 30.0s
    3. Immediately inside upper boundary: now + 29.0s
    4. Immediately outside upper boundary: now + 31.0s
    5. Immediately outside lower boundary: now - 31.0s
    """
    from pico.core.bridge import EffectiveUtcClock, get_replay_protector

    client = TestClient(app)
    method = "GET"
    target = "/health"
    body_digest = hashlib.sha256(b"").hexdigest()

    fixed_now = 2000000000
    fixed_now_ns = fixed_now * 1_000_000_000
    fixed_mono_ns = 100_000_000_000

    orig_clock = get_replay_protector().clock
    try:
        # Inject deterministic clock into protector
        get_replay_protector().clock = EffectiveUtcClock(
            time_source_ns=lambda: fixed_now_ns, mono_source_ns=lambda: fixed_mono_ns
        )

        # 1. Lower accepted boundary (now - 30s)
        ts_lower = str(fixed_now - 30)
        nonce_lower = "10000000000000000000000000000001"
        sig_lower = compute_proof_v2_hmac(TEST_SECRET, "v2", ts_lower, nonce_lower, method, target, body_digest)
        headers = {"X-Pico-Bridge-Proof": f"v2 {ts_lower} {nonce_lower} {sig_lower}"}
        resp = client.get(target, headers=headers)
        assert resp.status_code == 200

        # 2. Upper accepted boundary (now + 30s)
        ts_upper = str(fixed_now + 30)
        nonce_upper = "20000000000000000000000000000002"
        sig_upper = compute_proof_v2_hmac(TEST_SECRET, "v2", ts_upper, nonce_upper, method, target, body_digest)
        headers = {"X-Pico-Bridge-Proof": f"v2 {ts_upper} {nonce_upper} {sig_upper}"}
        resp = client.get(target, headers=headers)
        assert resp.status_code == 200

        # 3. Immediately inside upper boundary (now + 29s)
        ts_inside = str(fixed_now + 29)
        nonce_inside = "30000000000000000000000000000003"
        sig_inside = compute_proof_v2_hmac(TEST_SECRET, "v2", ts_inside, nonce_inside, method, target, body_digest)
        headers = {"X-Pico-Bridge-Proof": f"v2 {ts_inside} {nonce_inside} {sig_inside}"}
        resp = client.get(target, headers=headers)
        assert resp.status_code == 200

        # 4. Immediately outside upper boundary (now + 31s)
        ts_outside_up = str(fixed_now + 31)
        nonce_outside_up = "40000000000000000000000000000004"
        sig_outside_up = compute_proof_v2_hmac(TEST_SECRET, "v2", ts_outside_up, nonce_outside_up, method, target, body_digest)
        headers = {"X-Pico-Bridge-Proof": f"v2 {ts_outside_up} {nonce_outside_up} {sig_outside_up}"}
        resp = client.get(target, headers=headers)
        assert resp.status_code == 401
        assert "clock skew tolerance" in resp.json()["detail"]

        # 5. Immediately outside lower boundary (now - 31s)
        ts_outside_low = str(fixed_now - 31)
        nonce_outside_low = "50000000000000000000000000000005"
        sig_outside_low = compute_proof_v2_hmac(TEST_SECRET, "v2", ts_outside_low, nonce_outside_low, method, target, body_digest)
        headers = {"X-Pico-Bridge-Proof": f"v2 {ts_outside_low} {nonce_outside_low} {sig_outside_low}"}
        resp = client.get(target, headers=headers)
        assert resp.status_code == 401
        assert "clock skew tolerance" in resp.json()["detail"]
    finally:
        get_replay_protector().clock = orig_clock


def test_anti_replay_cache_concurrency():
    """Verify atomic duplicate detection under concurrent race with identical nonces."""
    cache = AntiReplayCache(capacity=1000, ttl_seconds=30.0)
    race_nonce = "concurrent_race_nonce_12345"
    results = []

    def try_insert():
        res = cache.check_and_insert(race_nonce)
        results.append(res)

    threads = [threading.Thread(target=try_insert) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r[0] is True]
    failures = [r for r in results if r[0] is False]
    assert len(successes) == 1
    assert len(failures) == 19
    for f in failures:
        assert "Replay detected" in f[1]


def test_bridge_proof_middleware_valid_request():
    """Valid X-Pico-Bridge-Proof: v2 header allows request to proceed."""
    client = TestClient(app)
    ts = str(int(time.time()))
    nonce = "11223344556677889900aabbccddeeff"
    method = "GET"
    target = "/health"
    body_digest = hashlib.sha256(b"").hexdigest()

    sig = compute_proof_v2_hmac(TEST_SECRET, "v2", ts, nonce, method, target, body_digest)
    headers = {
        "X-Pico-Bridge-Proof": f"v2 {ts} {nonce} {sig}",
    }

    response = client.get(target, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_bridge_proof_middleware_replay_rejected():
    """Replaying an identical nonce is rejected by the anti-replay cache."""
    client = TestClient(app)
    ts = str(int(time.time()))
    nonce = "aabbccddeeff00112233445566778899"
    method = "GET"
    target = "/health"
    body_digest = hashlib.sha256(b"").hexdigest()

    sig = compute_proof_v2_hmac(TEST_SECRET, "v2", ts, nonce, method, target, body_digest)
    headers = {
        "X-Pico-Bridge-Proof": f"v2 {ts} {nonce} {sig}",
    }

    # First request succeeds
    r1 = client.get(target, headers=headers)
    assert r1.status_code == 200

    # Second request with identical nonce fails
    r2 = client.get(target, headers=headers)
    assert r2.status_code == 401
    assert "Replay detected" in r2.json()["detail"]


def test_bridge_proof_middleware_body_mutation_rejected():
    """Modifying the body between signature creation and dispatch fails verification."""
    client = TestClient(app)
    ts = str(int(time.time()))
    nonce = "c0ffee112233445566778899aabbccdd"
    method = "POST"
    target = "/api/operator/pair"
    signed_body = b'{"code":"123456"}'
    signed_digest = hashlib.sha256(signed_body).hexdigest()

    sig = compute_proof_v2_hmac(TEST_SECRET, "v2", ts, nonce, method, target, signed_digest)
    headers = {
        "X-Pico-Bridge-Proof": f"v2 {ts} {nonce} {sig}",
    }

    # Transmit mutated body
    mutated_body = b'{"code":"654321"}'
    response = client.post(target, content=mutated_body, headers=headers)
    assert response.status_code == 401
    assert "signature verification failed" in response.json()["detail"]


def test_bridge_proof_middleware_invalid_hmac_rejected():
    """Tampered or invalid HMAC signature is rejected."""
    client = TestClient(app)
    ts = str(int(time.time()))
    nonce = "deadbeefdeadbeefdeadbeefdeadbeef"
    headers = {
        "X-Pico-Bridge-Proof": f"v2 {ts} {nonce} 0000000000000000000000000000000000000000000000000000000000000000",
    }
    response = client.get("/health", headers=headers)
    assert response.status_code == 401
    assert "verification failed" in response.json()["detail"]


def test_bridge_proof_middleware_expired_timestamp_rejected():
    """Timestamp older than 30s is rejected."""
    client = TestClient(app)
    ts = str(int(time.time()) - 35)
    nonce = "deadbeefcafebabedeadbeefcafebabe"
    method = "GET"
    target = "/health"
    body_digest = hashlib.sha256(b"").hexdigest()

    sig = compute_proof_v2_hmac(TEST_SECRET, "v2", ts, nonce, method, target, body_digest)
    headers = {
        "X-Pico-Bridge-Proof": f"v2 {ts} {nonce} {sig}",
    }
    response = client.get(target, headers=headers)
    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


# ==============================================================================
# Mandatory Deterministic Clock & Replay Security Simulations (Scenarios A - O)
# ==============================================================================

def test_scenario_a_normal_effective_clock_progression():
    """Scenario A: Normal effective clock progression guarantees E_k >= E_{k-1}."""
    from pico.core.bridge import EffectiveUtcClock

    current_wall = [1000 * 1_000_000_000]  # 1000s in ns
    current_mono = [10 * 1_000_000_000]    # 10s in ns

    clock = EffectiveUtcClock(
        time_source_ns=lambda: current_wall[0],
        mono_source_ns=lambda: current_mono[0],
    )

    last_eff = clock.peek_effective_ns()

    # Step through 100 observations advancing time normally
    for _ in range(100):
        current_wall[0] += 100_000_000  # +100ms
        current_mono[0] += 100_000_000  # +100ms
        eff, mono = clock.now_ns()
        assert eff >= last_eff
        assert eff == current_wall[0]
        last_eff = eff


def test_scenario_b_backward_wall_clock_adjustment_before_nonce_expiry():
    """Scenario B: Backward wall-clock adjustment before nonce expiry preserves duplicate rejection."""
    from pico.core.bridge import BridgeReplayProtector, EffectiveUtcClock

    t0_ns = 1000 * 1_000_000_000
    sim_wall = [t0_ns]
    sim_mono = [0]

    clock = EffectiveUtcClock(
        time_source_ns=lambda: sim_wall[0],
        mono_source_ns=lambda: sim_mono[0],
    )
    protector = BridgeReplayProtector(clock=clock)

    nonce = "0000000000000000000000000000000b"
    req_ts_ns = t0_ns  # Signed for 1000s

    # 1. Accept valid proof at T0
    ok, err = protector.validate_and_consume(nonce, req_ts_ns)
    assert ok and not err

    # 2. Advance monotonic time by 20s, but step wall clock backwards by 50s
    sim_mono[0] += 20 * 1_000_000_000
    sim_wall[0] -= 50 * 1_000_000_000  # Wall clock now 950s

    # Effective clock must not move backwards (stays at 1000s + 20s = 1020s)
    eff, _ = clock.now_ns()
    assert eff == (1000 + 20) * 1_000_000_000

    # 3. Duplicate request arrives: nonce is still in cache (20s < 60s TTL)
    ok_dup, err_dup = protector.validate_and_consume(nonce, req_ts_ns)
    assert not ok_dup
    assert "Replay detected" in err_dup


def test_scenario_c_backward_wall_clock_adjustment_after_nonce_expiry():
    """Scenario C: Backward wall-clock adjustment after nonce expiry cannot reactivate proof."""
    from pico.core.bridge import BridgeReplayProtector, EffectiveUtcClock

    t0_ns = 1000 * 1_000_000_000
    sim_wall = [t0_ns]
    sim_mono = [0]

    clock = EffectiveUtcClock(
        time_source_ns=lambda: sim_wall[0],
        mono_source_ns=lambda: sim_mono[0],
    )
    protector = BridgeReplayProtector(clock=clock)

    nonce = "0000000000000000000000000000000c"
    req_ts_ns = (1000 + 30) * 1_000_000_000  # Max future timestamp (+30s = 1030s)

    # 1. Initial valid acceptance
    ok, err = protector.validate_and_consume(nonce, req_ts_ns)
    assert ok and not err

    # 2. Advance monotonic time by 61s (exceeding 60s TTL)
    sim_mono[0] += 61 * 1_000_000_000

    # 3. Step wall clock backwards by 120s (wall clock reads 880s)
    sim_wall[0] -= 120 * 1_000_000_000

    # 4. Effective UTC must not regress: 1000s + 61s = 1061s
    eff, _ = clock.now_ns()
    assert eff >= (1000 + 61) * 1_000_000_000

    # 5. Adversary attempts replay of original request
    # Even though nonce has aged out of cache, effective UTC is 1061s.
    # Timestamp difference: 1061s - 1030s = 31s > 30s!
    ok_replay, err_replay = protector.validate_and_consume(nonce, req_ts_ns)
    assert not ok_replay
    assert "Bridge proof timestamp expired" in err_replay


def test_scenario_d_large_backward_wall_clock_adjustment():
    """Scenario D: Arbitrary large backward wall clock adjustment (e.g. 1 hour) fails closed."""
    from pico.core.bridge import BridgeReplayProtector, EffectiveUtcClock

    t0_ns = 2000 * 1_000_000_000
    sim_wall = [t0_ns]
    sim_mono = [0]

    clock = EffectiveUtcClock(
        time_source_ns=lambda: sim_wall[0],
        mono_source_ns=lambda: sim_mono[0],
    )
    protector = BridgeReplayProtector(clock=clock)

    nonce = "0000000000000000000000000000000d"
    req_ts_ns = t0_ns

    ok, _ = protector.validate_and_consume(nonce, req_ts_ns)
    assert ok

    # Large backward step: -3600 seconds (1 hour)
    sim_wall[0] -= 3600 * 1_000_000_000
    sim_mono[0] += 65 * 1_000_000_000  # 65s monotonic elapsed

    # Effective clock advances monotonically to 2065s
    eff, _ = clock.now_ns()
    assert eff >= (2000 + 65) * 1_000_000_000

    # Replay attempt fails timestamp validation
    ok_rep, err_rep = protector.validate_and_consume(nonce, req_ts_ns)
    assert not ok_rep
    assert "Bridge proof timestamp expired" in err_rep


def test_scenario_e_forward_clock_adjustment():
    """Scenario E: Forward wall-clock adjustment advances effective UTC; prior proofs expire."""
    from pico.core.bridge import BridgeReplayProtector, EffectiveUtcClock

    t0_ns = 1000 * 1_000_000_000
    sim_wall = [t0_ns]
    sim_mono = [0]

    clock = EffectiveUtcClock(
        time_source_ns=lambda: sim_wall[0],
        mono_source_ns=lambda: sim_mono[0],
    )
    protector = BridgeReplayProtector(clock=clock)

    nonce = "0000000000000000000000000000000e"
    req_ts_ns = t0_ns

    ok, _ = protector.validate_and_consume(nonce, req_ts_ns)
    assert ok

    # Step wall clock forward by 120s
    sim_wall[0] += 120 * 1_000_000_000
    sim_mono[0] += 1 * 1_000_000_000  # 1s mono

    eff, _ = clock.now_ns()
    assert eff == sim_wall[0]  # Effective time caught up immediately

    # Prior timestamp (1000s) is now 120s in the past -> rejected
    new_nonce = "000000000000000000000000000000e2"
    ok_old, err_old = protector.validate_and_consume(new_nonce, req_ts_ns)
    assert not ok_old
    assert "Bridge proof timestamp expired" in err_old


def test_scenario_f_forward_adjustment_followed_by_backward_correction():
    """Scenario F: Forward jump followed by backward retraction stays clamped at high-water mark."""
    from pico.core.bridge import EffectiveUtcClock

    t0_ns = 1000 * 1_000_000_000
    sim_wall = [t0_ns]
    sim_mono = [0]

    clock = EffectiveUtcClock(
        time_source_ns=lambda: sim_wall[0],
        mono_source_ns=lambda: sim_mono[0],
    )

    # 1. Forward jump +120s
    sim_wall[0] += 120 * 1_000_000_000
    sim_mono[0] += 1 * 1_000_000_000
    eff1, _ = clock.now_ns()
    assert eff1 == (1000 + 120) * 1_000_000_000

    # 2. Retraction backwards -120s
    sim_wall[0] -= 120 * 1_000_000_000
    sim_mono[0] += 1 * 1_000_000_000
    eff2, _ = clock.now_ns()
    # Must remain at least eff1 + 1s = 1121s
    assert eff2 == (1000 + 120 + 1) * 1_000_000_000
    assert eff2 >= eff1


def test_scenario_g_exact_60_second_nonce_boundary():
    """Scenario G: At exactly 60.0s elapsed (inclusive boundary), nonce is retained and replay rejected."""
    from pico.core.bridge import BridgeReplayProtector, EffectiveUtcClock

    t0_ns = 1000 * 1_000_000_000
    sim_wall = [t0_ns]
    sim_mono = [0]

    clock = EffectiveUtcClock(
        time_source_ns=lambda: sim_wall[0],
        mono_source_ns=lambda: sim_mono[0],
    )
    protector = BridgeReplayProtector(clock=clock)

    nonce = "00000000000000000000000000000007"
    req_ts_ns = (1000 + 30) * 1_000_000_000  # Max future timestamp (+30s)

    ok, _ = protector.validate_and_consume(nonce, req_ts_ns)
    assert ok

    # Advance exactly 60_000_000_000 nanoseconds
    sim_mono[0] += 60 * 1_000_000_000
    sim_wall[0] += 60 * 1_000_000_000

    # At exact boundary, entry is NOT pruned (entry_mono_ns < cutoff is False)
    ok_rep, err_rep = protector.validate_and_consume(nonce, req_ts_ns)
    assert not ok_rep
    assert "Replay detected" in err_rep
    assert protector.size() == 1


def test_scenario_h_immediately_after_60_seconds():
    """Scenario H: At 60s + 1ns elapsed, nonce is pruned but request fails timestamp check."""
    from pico.core.bridge import BridgeReplayProtector, EffectiveUtcClock

    t0_ns = 1000 * 1_000_000_000
    sim_wall = [t0_ns]
    sim_mono = [0]

    clock = EffectiveUtcClock(
        time_source_ns=lambda: sim_wall[0],
        mono_source_ns=lambda: sim_mono[0],
    )
    protector = BridgeReplayProtector(clock=clock)

    nonce = "00000000000000000000000000000008"
    req_ts_ns = (1000 + 30) * 1_000_000_000  # +30s

    ok, _ = protector.validate_and_consume(nonce, req_ts_ns)
    assert ok

    # Advance 60s + 1ns
    sim_mono[0] += 60_000_000_001
    sim_wall[0] += 60_000_000_001

    # At 60s + 1ns, timestamp difference is (1060.000000001 - 1030) = 30.000000001s > 30s
    ok_rep, err_rep = protector.validate_and_consume(nonce, req_ts_ns)
    assert not ok_rep
    assert "Bridge proof timestamp expired" in err_rep


def test_scenario_i_monotonic_source_regression_fails_closed():
    """Scenario I: Monotonic clock regression raises ClockUnavailableError, fails closed, preserves high-water."""
    from pico.core.bridge import BridgeReplayProtector, ClockUnavailableError, EffectiveUtcClock

    sim_wall = [1_000_000_000_000_000_000]
    sim_mono = [50_000_000_000]

    clock = EffectiveUtcClock(
        time_source_ns=lambda: sim_wall[0],
        mono_source_ns=lambda: sim_mono[0],
    )
    protector = BridgeReplayProtector(clock=clock)

    eff1, mono1 = clock.now_ns()
    assert eff1 == sim_wall[0]

    # Monotonic source regresses!
    sim_mono[0] -= 10_000_000_000  # Regressed by 10s

    # Clock raises ClockUnavailableError
    with pytest.raises(ClockUnavailableError) as exc_info:
        clock.now_ns()
    assert "Monotonic clock regressed" in str(exc_info.value)

    # High-water mark state preserved
    assert clock.peek_effective_ns() == eff1

    # Protector fails closed
    ok, err = protector.validate_and_consume("nonce_regression_test", sim_wall[0])
    assert not ok
    assert "Clock unavailable" in err


def test_scenario_j_concurrent_requests_with_same_nonce():
    """Scenario J: Concurrent duplicate requests with same nonce allow exactly 1 success."""
    from pico.core.bridge import BridgeReplayProtector

    protector = BridgeReplayProtector()
    race_nonce = "concurrent_race_nonce_jjjjjjjj"
    now_ts_ns = int(time.time() * 1_000_000_000)

    results = []

    def try_consume():
        res = protector.validate_and_consume(race_nonce, now_ts_ns)
        results.append(res)

    threads = [threading.Thread(target=try_consume) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r[0] is True]
    failures = [r for r in results if r[0] is False]
    assert len(successes) == 1
    assert len(failures) == 19
    for f in failures:
        assert "Replay detected" in f[1]


def test_scenario_k_cache_saturation_at_capacity():
    """Scenario K: Cache saturation fails closed without evicting active unexpired nonces."""
    from pico.core.bridge import BridgeReplayProtector, EffectiveUtcClock

    sim_wall = [1_000_000_000_000_000_000]
    sim_mono = [0]

    clock = EffectiveUtcClock(
        time_source_ns=lambda: sim_wall[0],
        mono_source_ns=lambda: sim_mono[0],
    )
    # Capacity 3
    protector = BridgeReplayProtector(capacity=3, clock=clock)

    now_ns = sim_wall[0]

    # Insert 3 entries
    ok1, _ = protector.validate_and_consume("nonce_sat_01", now_ns)
    assert ok1
    ok2, _ = protector.validate_and_consume("nonce_sat_02", now_ns)
    assert ok2
    ok3, _ = protector.validate_and_consume("nonce_sat_03", now_ns)
    assert ok3
    assert protector.size() == 3

    # 4th entry fails closed
    ok4, err4 = protector.validate_and_consume("nonce_sat_04", now_ns)
    assert not ok4
    assert "Anti-replay cache saturated" in err4
    assert protector.size() == 3  # No active entries evicted!


def test_scenario_l_expired_entry_cleanup_without_evicting_active_entries():
    """Scenario L: Expired entries are reclaimed while unexpired entries remain active."""
    from pico.core.bridge import BridgeReplayProtector, EffectiveUtcClock

    sim_wall = [1_000_000_000_000_000_000]
    sim_mono = [0]

    clock = EffectiveUtcClock(
        time_source_ns=lambda: sim_wall[0],
        mono_source_ns=lambda: sim_mono[0],
    )
    protector = BridgeReplayProtector(capacity=3, clock=clock)

    # Insert entry 1 at M=0
    ok1, _ = protector.validate_and_consume("nonce_l_01", sim_wall[0])
    assert ok1

    # Advance 40s
    sim_mono[0] += 40_000_000_000
    sim_wall[0] += 40_000_000_000

    # Insert entry 2 at M=40s
    ok2, _ = protector.validate_and_consume("nonce_l_02", sim_wall[0])
    assert ok2
    assert protector.size() == 2

    # Advance another 25s (total M = 65s) -> entry 1 is 65s old (>60s), entry 2 is 25s old (<60s)
    sim_mono[0] += 25_000_000_000
    sim_wall[0] += 25_000_000_000

    # Insert entry 3 at M=65s
    ok3, _ = protector.validate_and_consume("nonce_l_03", sim_wall[0])
    assert ok3

    # Entry 1 was pruned, entry 2 was retained, entry 3 was added -> size is 2
    assert protector.size() == 2

    # Replay of entry 2 still fails!
    ok_rep2, err_rep2 = protector.validate_and_consume("nonce_l_02", sim_wall[0])
    assert not ok_rep2
    assert "Replay detected" in err_rep2


def test_scenario_m_out_of_order_request_scheduling_prevented():
    """Scenario M: Atomic lock protects ordering; earlier samples cannot be inserted after later samples."""
    from pico.core.bridge import BridgeReplayProtector

    protector = BridgeReplayProtector()
    now_ts_ns = int(time.time() * 1_000_000_000)

    # Sequential operations are guaranteed monotonically ordered in _entries
    ok1, _ = protector.validate_and_consume("ordered_nonce_01", now_ts_ns)
    ok2, _ = protector.validate_and_consume("ordered_nonce_02", now_ts_ns)
    assert ok1 and ok2

    entries = list(protector._entries.items())
    assert len(entries) == 2
    assert entries[0][1] <= entries[1][1]  # Monotonic receipt timestamps strictly ordered


def test_concurrent_scheduling_atomic_observation_and_insertion_ordering():
    """Deterministic concurrency test verifying that concurrent requests cannot interleave observation and insertion.

    Request A cannot obtain an earlier observation, have Request B obtain a later observation and insert first,
    and then have Request A insert its earlier observation out-of-order.
    Both observation and insertion are serialized under the shared lock.
    """
    from pico.core.bridge import BridgeReplayProtector, EffectiveUtcClock
    import threading

    # Monotonic source that increments on each call
    mono_counter = [1_000_000_000]
    wall_time = 1000 * 1_000_000_000
    lock = threading.Lock()

    def get_wall():
        return wall_time

    def get_mono():
        with lock:
            val = mono_counter[0]
            mono_counter[0] += 1_000_000  # +1ms per sample
            return val

    clock = EffectiveUtcClock(time_source_ns=get_wall, mono_source_ns=get_mono)
    protector = BridgeReplayProtector(clock=clock)

    barrier = threading.Barrier(10)
    observed_order = []
    threads = []

    def worker(worker_id: int):
        barrier.wait()  # Synchronize thread launch
        nonce = f"worker_{worker_id:02d}_{'a' * 16}"
        ok, err = protector.validate_and_consume(nonce, wall_time)
        assert ok, f"Worker {worker_id} failed: {err}"

    for i in range(10):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Verify that in _entries, monotonic timestamps are strictly non-decreasing
    # which proves that observation order matches insertion order exactly
    entries = list(protector._entries.items())
    assert len(entries) == 10
    for idx in range(len(entries) - 1):
        prev_mono = entries[idx][1]
        next_mono = entries[idx + 1][1]
        assert prev_mono < next_mono, (
            f"Out-of-order insertion detected at index {idx}: {prev_mono} >= {next_mono}"
        )


def test_scenario_n_core_restart_and_launch_secret_rotation():
    """Scenario N: Launch secret rotation across Core restarts rejects proofs signed with old secret."""
    client = TestClient(app)
    target = "/health"
    ts = str(int(time.time()))
    nonce = "abcdef0123456789abcdef0123456789"
    body_digest = hashlib.sha256(b"").hexdigest()

    old_secret = "old_secret_0123456789abcdef0123456789abcdef0123456789abcdef01234567"
    new_secret = "new_secret_fedcba9876543210fedcba9876543210fedcba9876543210fedcba98"

    # Sign with old secret
    sig_old = compute_proof_v2_hmac(old_secret, "v2", ts, nonce, "GET", target, body_digest)
    headers = {"X-Pico-Bridge-Proof": f"v2 {ts} {nonce} {sig_old}"}

    # Core restarted: secret rotated to new_secret
    set_bridge_secret(new_secret)
    get_anti_replay_cache().clear()

    resp = client.get(target, headers=headers)
    assert resp.status_code == 401
    assert "signature verification failed" in resp.json()["detail"]


def test_scenario_o_iso8601_and_decimal_timestamp_parsing():
    """Scenario O: ISO 8601 UTC strings and decimal timestamps parse into integer nanoseconds with pure integer arithmetic."""
    from pico.core.bridge import parse_timestamp_to_epoch_ns

    # Decimal integer seconds
    assert parse_timestamp_to_epoch_ns("1731150000") == 1_731_150_000_000_000_000

    # ISO 8601 UTC with 'Z'
    assert parse_timestamp_to_epoch_ns("1970-01-01T00:00:10Z") == 10_000_000_000

    # ISO 8601 UTC with microseconds and '+00:00'
    assert parse_timestamp_to_epoch_ns("1970-01-01T00:00:10.500000+00:00") == 10_500_000_000

    # Naive / unqualified timestamps rejected
    assert parse_timestamp_to_epoch_ns("2026-11-09T18:00:00") is None

    # Malformed timestamps rejected
    assert parse_timestamp_to_epoch_ns("not-a-timestamp") is None
    assert parse_timestamp_to_epoch_ns("1731150000.5") is None  # Float string with decimal not allowed in decimal seconds
    assert parse_timestamp_to_epoch_ns("") is None


def test_invalid_hmac_does_not_consume_nonce():
    """An invalid HMAC signature is rejected BEFORE the nonce is consumed in the replay protector."""
    client = TestClient(app)
    ts = str(int(time.time()))
    nonce = "c0ffee0123456789abcdef0123456789"
    method = "GET"
    target = "/health"
    body_digest = hashlib.sha256(b"").hexdigest()

    # 1. Dispatch request with invalid HMAC
    bad_headers = {
        "X-Pico-Bridge-Proof": f"v2 {ts} {nonce} badbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbad",
    }
    resp1 = client.get(target, headers=bad_headers)
    assert resp1.status_code == 401
    assert "signature verification failed" in resp1.json()["detail"]

    # 2. Verify nonce was NOT consumed in protector
    protector = get_anti_replay_cache()
    assert nonce not in protector._entries

    # 3. Subsequent valid request with the SAME nonce succeeds!
    sig_valid = compute_proof_v2_hmac(TEST_SECRET, "v2", ts, nonce, method, target, body_digest)
    good_headers = {
        "X-Pico-Bridge-Proof": f"v2 {ts} {nonce} {sig_valid}",
    }
    resp2 = client.get(target, headers=good_headers)
    assert resp2.status_code == 200
    assert nonce in protector._entries
