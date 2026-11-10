"""PICO Core Native Bridge Startup Secret, Handshake & v2 Proof Verification Module.

Manages the per-launch ephemeral secret transferred from the native host supervisor
over an explicitly inherited anonymous pipe.
The secret is retained exclusively in process memory and never written to disk,
logs, environment variables, command-line arguments, or client-side bundles.
"""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import logging
import os
import secrets
import sys
import threading
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Protocol identifiers bound to HMAC challenge-response and per-request proofs
BRIDGE_PROTOCOL_VERSION = "pico-bridge-v1"
BRIDGE_V2_PROTOCOL_VERSION = "v2"
EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# Module-level transient memory storage for the ephemeral launch secret
_bridge_secret: Optional[str] = None


def set_bridge_secret(secret: Optional[str]) -> None:
    """Sets or clears the transient in-memory bridge secret reference.

    Note: Setting to None releases the module reference to the string.
    CPython manages underlying memory via its standard allocator; memory
    is deallocated when references reach zero and reclaimed upon process exit.
    """
    global _bridge_secret
    _bridge_secret = secret


def get_bridge_secret() -> Optional[str]:
    """Returns the current transient bridge secret, or None if unmanaged."""
    return _bridge_secret


def init_bridge_secret() -> Optional[str]:
    """Initializes the bridge secret from an inherited pipe handle if present in os.environ.

    Requirements:
    1. Reads handle integer from PICO_BRIDGE_PIPE_HANDLE.
    2. Deletes the environment variable immediately to prevent leakage to child processes.
    3. Reads the secret from the OS handle via msvcrt.open_osfhandle (Windows) or os.fdopen (POSIX).
    4. Closes the OS handle immediately after reading.
    5. Retains the secret strictly in process memory.
    """
    global _bridge_secret

    handle_str = os.environ.pop("PICO_BRIDGE_PIPE_HANDLE", None)
    if not handle_str:
        return _bridge_secret

    try:
        handle_int = int(handle_str.strip())
        if sys.platform == "win32":
            import msvcrt

            fd = msvcrt.open_osfhandle(handle_int, os.O_RDONLY)
            with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as pipe:
                secret = pipe.read().strip()
        else:
            with os.fdopen(handle_int, "r", encoding="utf-8", closefd=True) as pipe:
                secret = pipe.read().strip()

        if secret:
            _bridge_secret = secret
            logger.info("Native bridge launch secret successfully ingested into process memory.")
            return _bridge_secret
    except Exception as exc:
        logger.error("Failed to ingest bridge launch secret from inherited handle: %s", exc)

    return None


def compute_handshake_hmac(nonce: str) -> Optional[str]:
    """Computes the HMAC-SHA256 response for a given challenge nonce.

    Binds the computation to the protocol version and nonce string.
    Returns None if no launch secret is configured in process memory.
    """
    if not _bridge_secret:
        return None
    if not nonce or not isinstance(nonce, str):
        return None

    # Domain separation tag prevents cross-protocol reuse
    message = f"{BRIDGE_PROTOCOL_VERSION}:{nonce.strip()}".encode("utf-8")
    key = _bridge_secret.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_handshake_hmac(nonce: str, candidate_hmac: str) -> bool:
    """Verifies a candidate HMAC response using constant-time comparison."""
    if not nonce or not candidate_hmac:
        return False
    expected_hmac = compute_handshake_hmac(nonce)
    if not expected_hmac:
        return False
    return secrets.compare_digest(expected_hmac, candidate_hmac.strip())


def build_canonical_proof_v2_string(
    version: str,
    timestamp: str,
    nonce: str,
    method: str,
    request_target: str,
    body_digest: str,
) -> str:
    """Builds the length-prefixed canonical input string for ADR 0003 Section 8.1.

    Format:
    pico-bridge-v2:{len(version)}:{version}:{len(timestamp)}:{timestamp}:{len(nonce)}:{nonce}:{len(method)}:{method}:{len(request_target)}:{request_target}:{len(body_digest)}:{body_digest}

    All field lengths are decimal ASCII representations of exact UTF-8 byte lengths.
    """
    v_b = version.encode("utf-8")
    ts_b = timestamp.encode("utf-8")
    n_b = nonce.encode("utf-8")
    m_b = method.encode("utf-8")
    rt_b = request_target.encode("utf-8")
    bd_b = body_digest.encode("utf-8")

    return (
        f"pico-bridge-v2:{len(v_b)}:{version}:{len(ts_b)}:{timestamp}:"
        f"{len(n_b)}:{nonce}:{len(m_b)}:{method}:{len(rt_b)}:{request_target}:"
        f"{len(bd_b)}:{body_digest}"
    )


def compute_proof_v2_hmac(
    secret: str,
    version: str,
    timestamp: str,
    nonce: str,
    method: str,
    request_target: str,
    body_digest: str,
) -> str:
    """Computes HMAC-SHA256 signature for the canonical v2 proof string."""
    canonical_str = build_canonical_proof_v2_string(
        version=version,
        timestamp=timestamp,
        nonce=nonce,
        method=method,
        request_target=request_target,
        body_digest=body_digest,
    )
    key = secret.encode("utf-8")
    return hmac.new(key, canonical_str.encode("utf-8"), hashlib.sha256).hexdigest()


class ClockUnavailableError(Exception):
    """Raised when the host clock sources fail closed (e.g. monotonic clock regression)."""

    pass


def parse_timestamp_to_epoch_ns(timestamp_str: str) -> Optional[int]:
    """Parses a timestamp string into integer nanoseconds since Unix epoch.

    Supported formats:
    1. Native format: Decimal integer Unix epoch seconds (e.g. "1731150000").
    2. Server compatibility path: ISO 8601 UTC string (e.g. "2026-11-09T18:00:00Z" or "...+00:00").
       Naive / unqualified timestamps are rejected.

    Uses pure integer arithmetic throughout; never uses floating-point conversions.
    Returns integer nanoseconds or None if malformed.
    """
    if not timestamp_str or not isinstance(timestamp_str, str):
        return None

    cleaned = timestamp_str.strip()

    # 1. Decimal integer seconds (native format emitted by Tauri Rust host)
    if cleaned.isdigit():
        try:
            return int(cleaned) * 1_000_000_000
        except (ValueError, OverflowError):
            return None

    # 2. ISO 8601 UTC string (server compatibility path)
    # Must specify explicit timezone indicator (e.g. 'Z', '+00:00', '-05:00')
    has_tz = cleaned.endswith("Z") or "+" in cleaned or ("-" in cleaned[10:] if len(cleaned) > 10 else False)
    if not has_tz:
        return None

    try:
        iso_str = cleaned.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            return None
        utc_dt = dt.astimezone(timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = utc_dt - epoch
        total_seconds = delta.days * 86400 + delta.seconds
        return (total_seconds * 1_000_000_000) + (delta.microseconds * 1_000)
    except Exception:
        return None


class EffectiveUtcClock:
    """Non-decreasing effective UTC clock using integer nanoseconds.

    Invariant:
    For every successful observation, effective_now_ns >= previous_effective_now_ns.
    Fails closed if the monotonic source regresses (does not clamp to zero).
    """

    def __init__(
        self,
        time_source_ns: Optional[Callable[[], int]] = None,
        mono_source_ns: Optional[Callable[[], int]] = None,
    ) -> None:
        self._time_source_ns = time_source_ns or time.time_ns
        self._mono_source_ns = mono_source_ns or time.monotonic_ns
        self._lock = threading.Lock()

        initial_wall_ns = self._time_source_ns()
        initial_mono_ns = self._mono_source_ns()

        self._last_effective_ns: int = initial_wall_ns
        self._last_mono_ns: int = initial_mono_ns

    def now_ns(self) -> Tuple[int, int]:
        """Returns (effective_utc_ns, current_mono_ns).

        Raises ClockUnavailableError if monotonic clock regressed.
        """
        with self._lock:
            current_wall_ns = self._time_source_ns()
            current_mono_ns = self._mono_source_ns()

            if current_mono_ns < self._last_mono_ns:
                raise ClockUnavailableError(
                    f"Monotonic clock regressed: current={current_mono_ns} < last={self._last_mono_ns}"
                )

            elapsed_mono_ns = current_mono_ns - self._last_mono_ns
            expected_effective_ns = self._last_effective_ns + elapsed_mono_ns

            effective_ns = max(current_wall_ns, expected_effective_ns)

            self._last_effective_ns = effective_ns
            self._last_mono_ns = current_mono_ns

            return effective_ns, current_mono_ns

    def peek_effective_ns(self) -> int:
        """Returns last known effective UTC nanoseconds without advancing."""
        with self._lock:
            return self._last_effective_ns


class BridgeReplayProtector:
    """Atomic bridge proof replay protector and effective timestamp validator.

    Coordinates:
    - Effective UTC high-water mark state.
    - Monotonic clock observation state.
    - Nonce cache indexed by monotonic nanoseconds.
    - Single synchronization lock protecting ordering, validation, and consumption.
    """

    TTL_NS: int = 60_000_000_000  # 60 seconds
    SKEW_WINDOW_NS: int = 30_000_000_000  # 30 seconds
    DEFAULT_CAPACITY: int = 10_000

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        ttl_ns: Optional[int] = None,
        ttl_seconds: Optional[float] = None,
        skew_window_ns: int = SKEW_WINDOW_NS,
        clock: Optional[EffectiveUtcClock] = None,
    ) -> None:
        self.capacity = capacity
        if ttl_ns is not None:
            self.ttl_ns = ttl_ns
        elif ttl_seconds is not None:
            self.ttl_ns = int(ttl_seconds * 1_000_000_000)
        else:
            self.ttl_ns = self.TTL_NS
        self.skew_window_ns = skew_window_ns
        self.clock = clock or EffectiveUtcClock()
        self._entries: OrderedDict[str, int] = OrderedDict()  # nonce -> mono_receipt_ns
        self._lock = threading.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self.ttl_ns / 1_000_000_000.0

    def validate_and_consume(self, nonce: str, request_timestamp_ns: int) -> Tuple[bool, str]:
        """Atomically validates timestamp freshness against effective UTC and consumes the nonce.

        Returns (True, "") on success.
        Returns (False, reason) on rejection.
        """
        with self._lock:
            # 1. Sample clocks under lock
            try:
                effective_now_ns, now_mono_ns = self.clock.now_ns()
            except ClockUnavailableError as exc:
                return False, f"Clock unavailable: {exc}"

            # 2. Validate timestamp freshness against effective UTC (integer nanoseconds)
            skew_diff = abs(effective_now_ns - request_timestamp_ns)
            if skew_diff > self.skew_window_ns:
                return False, "Bridge proof timestamp expired or out of ±30s clock skew tolerance window."

            # 3. Prune expired entries from the head of the OrderedDict
            # Strict inequality: entry_mono_ns < cutoff_mono_ns
            # Nonce is retained at exact boundary age == ttl_ns!
            cutoff_mono_ns = now_mono_ns - self.ttl_ns
            while self._entries:
                first_nonce, entry_mono_ns = next(iter(self._entries.items()))
                if entry_mono_ns < cutoff_mono_ns:
                    self._entries.popitem(last=False)
                else:
                    break

            # 4. Check for replay
            if nonce in self._entries:
                return False, "Replay detected: nonce has already been used within active TTL window"

            # 5. Check capacity (fail closed)
            if len(self._entries) >= self.capacity:
                return False, "Anti-replay cache saturated: capacity exceeded"

            # 6. Insert fresh entry with monotonic receipt timestamp
            self._entries[nonce] = now_mono_ns
            return True, ""

    def check_and_insert(self, nonce: str, current_time: Optional[float] = None) -> Tuple[bool, str]:
        """Backward-compatibility helper for tests expecting legacy check_and_insert."""
        with self._lock:
            if current_time is not None:
                now_mono_ns = int(current_time * 1_000_000_000)
            else:
                try:
                    _, now_mono_ns = self.clock.now_ns()
                except ClockUnavailableError as exc:
                    return False, f"Clock unavailable: {exc}"

            cutoff_mono_ns = now_mono_ns - self.ttl_ns
            while self._entries:
                first_nonce, entry_mono_ns = next(iter(self._entries.items()))
                if entry_mono_ns < cutoff_mono_ns:
                    self._entries.popitem(last=False)
                else:
                    break

            if nonce in self._entries:
                return False, "Replay detected: nonce has already been used within active TTL window"

            if len(self._entries) >= self.capacity:
                return False, "Anti-replay cache saturated: capacity exceeded"

            self._entries[nonce] = now_mono_ns
            return True, ""

    def clear(self) -> None:
        """Purges all entries from the cache."""
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        """Returns the number of cached entries."""
        with self._lock:
            return len(self._entries)


# Singleton replay protector
_replay_protector = BridgeReplayProtector()


def get_replay_protector() -> BridgeReplayProtector:
    """Returns the singleton replay protector instance."""
    return _replay_protector


def set_replay_protector(protector: BridgeReplayProtector) -> None:
    """Sets the singleton replay protector instance (used for testing)."""
    global _replay_protector
    _replay_protector = protector


# Backward-compatibility aliases
AntiReplayCache = BridgeReplayProtector


def get_anti_replay_cache() -> BridgeReplayProtector:
    """Returns the singleton replay protector instance."""
    return _replay_protector
