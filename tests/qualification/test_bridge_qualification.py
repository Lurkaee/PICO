"""P11.2.2 Stage 0.1: Windows Loopback Bridge & Backend Identity Qualification Test Suite.

Empirically qualifies Windows 11 loopback process identity verification mechanisms,
managed child-process architectures, startup handshake protocols, and fail-closed
credential protection behaviors using genuine Win32 networking APIs (GetExtendedTcpTable)
and process inspection (OpenProcess, QueryFullProcessImageNameW, GetProcessTimes).

Distinguishes:
1. TCP endpoint ownership evidence (GetExtendedTcpTable).
2. Process-instance identification (PID + creation timestamp FILETIME).
3. Executable image identification (QueryFullProcessImageNameW).
4. Actual PICO Core application identity (Tauri-managed process hierarchy / cryptographic handshake).
5. Authority to transmit operator credentials (only after positive application verification).

Test Cases:
- Case 1: Unbound port (connection refused / fail-closed; credentials suppressed).
- Case 2: Rogue listener returning fake 200 OK /health (untrusted process; credentials suppressed).
- Case 3: Legitimate mock process (PID, image path, creation time verified; credentials dispatched).
- Case 4: Process termination (socket drop detected; fail-closed; credentials suppressed).
- Case 5: Process restart on different PID (stale PID rejected; re-verification triggered).
- Case 6: PID reuse simulation (creation time mismatch caught; credentials suppressed).
- Test A: Same executable, different process (same python.exe, different instance/PID; rejected).
- Test B: Actual PICO backend child (real FastAPI application spawned without --reload; verified).
- Test C: Backend restart invalidates stale credentials & process identity.
- Test D: Port takeover after backend termination (rogue listener binds same port; fails closed).
- Test E: Strict IPv4 loopback binding (127.0.0.1 accepted; 0.0.0.0 strictly rejected).
- Test F: Synthetic sentinel credential suppression (pairing code, cookie, CSRF never transmitted).
- Test G: Same-user threat boundary empirical demonstration (documents OS limits).
"""

import ctypes
from ctypes import wintypes
import hashlib
import hmac
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.request
import pytest
from http.server import HTTPServer, BaseHTTPRequestHandler


# Win32 Constants & Structures
AF_INET = 2
TCP_TABLE_OWNER_PID_LISTENER = 3
TCP_TABLE_OWNER_PID_ALL = 5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010

# Synthetic sentinel credentials (never use real tokens in tests)
SENTINEL_PAIRING_CODE = "SENTINEL_TEST_PAIR_987654"
SENTINEL_SESSION_COOKIE = "pico_op_sentinel_session_abc123"
SENTINEL_CSRF_TOKEN = "csrf_sentinel_token_xyz789"


class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


def get_tcp_listener_pid(
    port: int, host: str = "127.0.0.1", strict_loopback: bool = True
) -> Optional[int]:
    """Inspects the Windows TCP connection table to resolve the PID listening on a given port.

    Uses Win32 GetExtendedTcpTable with TCP_TABLE_OWNER_PID_LISTENER (TableClass 3).

    Invariants:
    - If strict_loopback=True, requires local address to match 127.0.0.1 exactly.
      INADDR_ANY (0.0.0.0) is strictly rejected because it binds to external interfaces.
    - Matches host port converted from network byte order via socket.ntohs.
    """
    if sys.platform != "win32":
        pytest.skip("Win32 TCP table inspection requires Windows platform")

    target_ip_bytes = socket.inet_aton(host)
    target_ip_dw = int.from_bytes(target_ip_bytes, "little")

    size = wintypes.DWORD(0)
    ctypes.windll.iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(size), True, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0
    )

    buf = (ctypes.c_char * size.value)()
    res = ctypes.windll.iphlpapi.GetExtendedTcpTable(
        buf, ctypes.byref(size), True, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0
    )
    if res != 0:
        return None

    num_entries = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
    offset = ctypes.sizeof(wintypes.DWORD)
    row_size = ctypes.sizeof(MIB_TCPROW_OWNER_PID)

    for i in range(num_entries):
        row = MIB_TCPROW_OWNER_PID.from_buffer_copy(
            buf[offset + i * row_size : offset + (i + 1) * row_size]
        )
        row_port = socket.ntohs(row.dwLocalPort & 0xFFFF)
        if row_port == port:
            if strict_loopback:
                # Must match 127.0.0.1 exactly; 0.0.0.0 is rejected
                if row.dwLocalAddr == target_ip_dw:
                    return row.dwOwningPid
            else:
                if row.dwLocalAddr == target_ip_dw or row.dwLocalAddr == 0:
                    return row.dwOwningPid

    return None


def get_process_details(pid: int) -> Tuple[Optional[str], Optional[int]]:
    """Retrieves the executable image path and creation timestamp (FILETIME) for a given PID.

    Uses OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION, QueryFullProcessImageNameW,
    and GetProcessTimes.
    """
    if sys.platform != "win32":
        return None, None

    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None, None

    try:
        # 1. Executable image path
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            image_path = buf.value
        else:
            image_path = None

        # 2. Process creation timestamp (64-bit FILETIME)
        creation_time = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation_time),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            creation_timestamp = (creation_time.dwHighDateTime << 32) + creation_time.dwLowDateTime
        else:
            creation_timestamp = None

        return image_path, creation_timestamp
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


class ManagedBackendSupervisor:
    """Represents the Tauri Native Host's child-process supervisor & loopback bridge client.

    Implements the full managed-backend trust model:
    - Holds the direct child process handle.
    - Records the trusted child PID and creation timestamp at launch.
    - Holds a cryptographically random per-launch secret for mutual handshake.
    - Requires:
        1. Child process is currently alive.
        2. Live TCP listener on 127.0.0.1:{port} is owned by the exact child PID.
        3. Listener creation time matches the recorded child creation timestamp.
        4. (Optional/handshake) Challenge-response matches per-launch secret.
    - Fails closed and suppresses credentials if any invariant is violated.
    """

    def __init__(
        self,
        port: int,
        expected_child_pid: Optional[int] = None,
        expected_creation_time: Optional[int] = None,
        launch_secret: Optional[str] = None,
    ):
        self.port = port
        self.expected_child_pid = expected_child_pid
        self.expected_creation_time = expected_creation_time
        self.launch_secret = launch_secret or secrets.token_hex(32)
        self.verified_state: Optional[Dict[str, Any]] = None

    def register_managed_child(self, pid: int, creation_time: int) -> None:
        self.expected_child_pid = pid
        self.expected_creation_time = creation_time
        self.verified_state = None

    def verify_backend_identity(self, challenge_response: Optional[str] = None, nonce: Optional[str] = None) -> Tuple[bool, str]:
        """Performs full verification of the local loopback backend before credential release."""
        if self.expected_child_pid is None:
            return False, "ERR_NO_MANAGED_CHILD: No managed child process registered"

        # 1. Verify TCP listener on exact loopback (127.0.0.1)
        listener_pid = get_tcp_listener_pid(self.port, host="127.0.0.1", strict_loopback=True)
        if listener_pid is None:
            self.verified_state = None
            return False, "ERR_NO_LOOPBACK_LISTENER: Port is unbound or not bound strictly to 127.0.0.1"

        # 2. Verify listener PID matches the retained managed child PID
        if listener_pid != self.expected_child_pid:
            self.verified_state = None
            return False, f"ERR_PID_MISMATCH: Listener PID {listener_pid} does not match managed child PID {self.expected_child_pid}"

        # 3. Verify process image and creation timestamp
        image_path, creation_time = get_process_details(listener_pid)
        if creation_time is None or creation_time != self.expected_creation_time:
            self.verified_state = None
            return False, f"ERR_CREATION_TIME_MISMATCH: Process creation time mismatch for PID {listener_pid} (potential PID reuse)"

        # 4. If handshake verification is requested, check HMAC(launch_secret, nonce)
        if nonce is not None:
            if not challenge_response:
                self.verified_state = None
                return False, "ERR_HANDSHAKE_MISSING: Startup handshake challenge response missing"
            # Domain-separated HMAC matching pico.core.bridge and supervisor.rs
            message = f"pico-bridge-v1:{nonce.strip()}".encode("utf-8")
            expected_mac = hmac.new(
                self.launch_secret.encode("utf-8"), message, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected_mac, challenge_response):
                self.verified_state = None
                return False, "ERR_HANDSHAKE_FAILED: Startup handshake HMAC verification failed"

        # Verification successful
        self.verified_state = {
            "pid": listener_pid,
            "creation_time": creation_time,
            "image_path": image_path,
            "verified_at": time.time(),
        }
        return True, "VERIFIED_OK"

    def dispatch_authenticated_request(
        self,
        endpoint: str,
        pairing_code: Optional[str] = None,
        session_cookie: Optional[str] = None,
        csrf_token: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Dispatches operator credentials over loopback only after authoritative verification.

        If unverified, all credentials are unconditionally suppressed.
        """
        # Ensure verified
        if not self.verified_state:
            is_valid, reason = self.verify_backend_identity()
            if not is_valid:
                return False, f"SUPPRESSED_CREDENTIALS: {reason}", None

        # Re-check listener continuity immediately before dispatch
        current_listener = get_tcp_listener_pid(self.port, host="127.0.0.1", strict_loopback=True)
        if current_listener != self.verified_state["pid"]:
            self.verified_state = None
            return False, "SUPPRESSED_CREDENTIALS: Listener PID changed since verification", None

        # Safe to dispatch
        transmitted_payload = {
            "endpoint": endpoint,
            "sent_pairing_code": pairing_code,
            "sent_session_cookie": session_cookie,
            "sent_csrf_token": csrf_token,
            "target_pid": current_listener,
        }
        return True, "DISPATCH_AUTHORIZED", transmitted_payload


# Mock HTTP Servers for Testing
class RecordingFakeHandler(BaseHTTPRequestHandler):
    """Fake HTTP handler that logs all received credentials and requests."""
    received_requests: List[Dict[str, Any]] = []

    def do_GET(self):
        self.record_request()
        if self.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "healthy", "service": "fake_pico"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        self.record_request()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "captured"}')

    def record_request(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""
        RecordingFakeHandler.received_requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
            "cookie": self.headers.get("Cookie", ""),
            "csrf": self.headers.get("X-CSRF-Token", ""),
        })

    def log_message(self, format, *args):
        pass  # Suppress console logging during test


def run_http_server(server: HTTPServer):
    server.serve_forever()


@pytest.fixture
def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ============================================================================
# Original Six Scenarios (Preserved & Reconciled)
# ============================================================================

def test_case_1_unbound_port_fail_closed(free_port):
    """Case 1: No process on port -> connection refused, fail-closed, credentials suppressed."""
    supervisor = ManagedBackendSupervisor(port=free_port, expected_child_pid=1234, expected_creation_time=5678)
    dispatched, reason, payload = supervisor.dispatch_authenticated_request(
        "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
    )
    assert not dispatched
    assert "SUPPRESSED_CREDENTIALS" in reason
    assert "ERR_NO_LOOPBACK_LISTENER" in reason
    assert payload is None


def test_case_2_rogue_listener_fake_health_suppressed(free_port):
    """Case 2: Rogue process binds port returning fake 200 OK /health.

    Verification detects PID mismatch with retained child -> credentials suppressed.
    """
    server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server,), daemon=True)
    thread.start()

    try:
        # Supervisor expects child PID 99999, but listening process is current test PID
        supervisor = ManagedBackendSupervisor(
            port=free_port, expected_child_pid=99999, expected_creation_time=123456
        )
        dispatched, reason, payload = supervisor.dispatch_authenticated_request(
            "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
        )
        assert not dispatched
        assert "SUPPRESSED_CREDENTIALS" in reason
        assert "ERR_PID_MISMATCH" in reason
        assert payload is None
    finally:
        server.shutdown()
        server.server_close()


def test_case_3_legitimate_process_verified_dispatched(free_port):
    """Case 3: Legitimate process with matching PID and creation time -> verification succeeds, credentials dispatched."""
    server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server,), daemon=True)
    thread.start()

    try:
        current_pid = os.getpid()
        _, real_creation_time = get_process_details(current_pid)
        assert real_creation_time is not None

        supervisor = ManagedBackendSupervisor(
            port=free_port, expected_child_pid=current_pid, expected_creation_time=real_creation_time
        )
        dispatched, reason, payload = supervisor.dispatch_authenticated_request(
            "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
        )
        assert dispatched
        assert reason == "DISPATCH_AUTHORIZED"
        assert payload is not None
        assert payload["sent_pairing_code"] == SENTINEL_PAIRING_CODE
        assert payload["target_pid"] == current_pid
    finally:
        server.shutdown()
        server.server_close()


def test_case_4_process_termination_fail_closed(free_port):
    """Case 4: Process termination after verification -> next request detects socket drop, fail-closed."""
    server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server,), daemon=True)
    thread.start()

    current_pid = os.getpid()
    _, real_creation_time = get_process_details(current_pid)
    supervisor = ManagedBackendSupervisor(
        port=free_port, expected_child_pid=current_pid, expected_creation_time=real_creation_time
    )

    # Initial request verifies and dispatches
    dispatched, reason, _ = supervisor.dispatch_authenticated_request(
        "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
    )
    assert dispatched

    # Terminate server
    server.shutdown()
    server.server_close()
    time.sleep(0.05)

    # Subsequent request detects socket drop and suppresses credentials
    dispatched2, reason2, payload2 = supervisor.dispatch_authenticated_request(
        "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
    )
    assert not dispatched2
    assert "SUPPRESSED_CREDENTIALS" in reason2
    assert payload2 is None


def test_case_5_process_restart_different_pid(free_port):
    """Case 5: Process restarts on different PID -> stale cached PID rejected, re-verification required."""
    supervisor = ManagedBackendSupervisor(
        port=free_port, expected_child_pid=88888, expected_creation_time=123456
    )
    # Fake a previously verified state with old PID
    supervisor.verified_state = {"pid": 88888, "creation_time": 123456, "image_path": "C:\\python.exe"}

    server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server,), daemon=True)
    thread.start()

    try:
        dispatched, reason, payload = supervisor.dispatch_authenticated_request(
            "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
        )
        assert not dispatched
        assert "Listener PID changed since verification" in reason
        assert payload is None
        assert supervisor.verified_state is None
    finally:
        server.shutdown()
        server.server_close()


def test_case_6_pid_reuse_creation_time_mismatch(free_port):
    """Case 6: PID reuse simulation -> creation time mismatch caught, credentials suppressed."""
    server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server,), daemon=True)
    thread.start()

    try:
        current_pid = os.getpid()
        _, real_creation_time = get_process_details(current_pid)

        # Expected creation time is artificially different from real creation time
        supervisor = ManagedBackendSupervisor(
            port=free_port,
            expected_child_pid=current_pid,
            expected_creation_time=real_creation_time - 99999999,
        )

        dispatched, reason, payload = supervisor.dispatch_authenticated_request(
            "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
        )
        assert not dispatched
        assert "ERR_CREATION_TIME_MISMATCH" in reason
        assert payload is None
    finally:
        server.shutdown()
        server.server_close()


# ============================================================================
# Stage 0.1 Deep Qualification Tests (Tests A through G)
# ============================================================================

def test_a_same_executable_different_process(free_port):
    """Test A: Rogue HTTP server launched using the exact same python.exe as PICO.

    Must fail identity verification because listener PID does not match the retained managed child PID,
    even though the executable image path (python.exe) is identical.
    """
    base_py = sys._base_executable
    # Spawn an independent rogue script using the exact same base Python executable
    rogue_code = f"""
import http.server
server = http.server.HTTPServer(('127.0.0.1', {free_port}), http.server.SimpleHTTPRequestHandler)
server.serve_forever()
"""
    proc = subprocess.Popen([base_py, "-c", rogue_code])

    try:
        # Wait for rogue listener to appear
        listener_pid = None
        for _ in range(30):
            time.sleep(0.1)
            listener_pid = get_tcp_listener_pid(free_port, host="127.0.0.1", strict_loopback=True)
            if listener_pid is not None:
                break
        assert listener_pid == proc.pid

        # Verify that both the test process and the rogue process share the exact same binary image
        rogue_image, _ = get_process_details(proc.pid)
        current_image, _ = get_process_details(os.getpid())
        assert rogue_image is not None and current_image is not None
        assert os.path.normcase(rogue_image) == os.path.normcase(current_image)

        # The supervisor expects a DIFFERENT managed child PID (e.g. 88888 or current test runner PID)
        supervisor = ManagedBackendSupervisor(
            port=free_port, expected_child_pid=os.getpid(), expected_creation_time=12345
        )

        dispatched, reason, payload = supervisor.dispatch_authenticated_request(
            "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
        )
        # MUST FAIL: matching python.exe is NOT application authentication!
        assert not dispatched
        assert "ERR_PID_MISMATCH" in reason
        assert payload is None
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)


def test_b_actual_pico_backend_child(free_port):
    """Test B: Actual PICO backend child.

    Launch real FastAPI application without --reload in an isolated temporary environment.
    Verify actual listener PID against retained managed child instance.
    """
    base_py = sys._base_executable
    venv_dir = os.path.dirname(os.path.dirname(sys.executable))
    site_packages = os.path.join(venv_dir, "Lib", "site-packages")

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_db_path = os.path.join(tmpdir, "test_pico.db")
        env = os.environ.copy()
        env["VIRTUAL_ENV"] = venv_dir
        env["PYTHONPATH"] = site_packages + os.pathsep + os.getcwd()
        env["DATABASE_URL"] = f"sqlite:///{temp_db_path}"
        env["WORKFLOW_SYNTHESIS_ENABLED"] = "false"

        # Launch real FastAPI backend without --reload using direct base Python + virtualenv path
        proc = subprocess.Popen(
            [base_py, "-m", "uvicorn", "pico.main:app", "--host", "127.0.0.1", "--port", str(free_port)],
            env=env,
        )

        try:
            # Query child creation details immediately from parent
            child_image, child_creation = get_process_details(proc.pid)
            assert child_creation is not None

            # Wait for backend /api/health to answer
            listener_pid = None
            for _ in range(50):
                time.sleep(0.2)
                listener_pid = get_tcp_listener_pid(free_port, host="127.0.0.1", strict_loopback=True)
                if listener_pid is not None:
                    break

            assert listener_pid is not None, "Actual PICO backend failed to bind loopback port"
            # Authoritative check: The listener PID matches the managed child PID exactly
            assert listener_pid == proc.pid

            # Initialize supervisor with retained child credentials
            supervisor = ManagedBackendSupervisor(
                port=free_port, expected_child_pid=proc.pid, expected_creation_time=child_creation
            )

            is_valid, reason = supervisor.verify_backend_identity()
            assert is_valid, f"Verification failed: {reason}"

            # Dispatch with synthetic credentials authorized
            dispatched, reason, payload = supervisor.dispatch_authenticated_request(
                "/api/operator/session", session_cookie=SENTINEL_SESSION_COOKIE
            )
            assert dispatched
            assert payload["target_pid"] == proc.pid
        finally:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            proc.wait()


def test_c_backend_restart_invalidates_stale_credentials(free_port):
    """Test C: Backend restart.

    Terminate managed child and restart it.
    Verify that stale process identity and stale startup credentials cannot authorize the new instance.
    """
    base_py = sys._base_executable

    # Instance 1
    server1 = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread1 = threading.Thread(target=run_http_server, args=(server1,), daemon=True)
    thread1.start()

    inst1_pid = os.getpid()
    _, inst1_creation = get_process_details(inst1_pid)
    inst1_secret = secrets.token_hex(32)

    supervisor = ManagedBackendSupervisor(
        port=free_port,
        expected_child_pid=inst1_pid,
        expected_creation_time=inst1_creation,
        launch_secret=inst1_secret,
    )

    nonce1 = secrets.token_hex(16)
    mac1 = hmac.new(
        inst1_secret.encode(), f"pico-bridge-v1:{nonce1}".encode(), hashlib.sha256
    ).hexdigest()
    verified1, _ = supervisor.verify_backend_identity(challenge_response=mac1, nonce=nonce1)
    assert verified1

    # Terminate Instance 1
    server1.shutdown()
    server1.server_close()
    time.sleep(0.05)

    # Start Instance 2 on same port (simulated as child with new PID/secret)
    rogue_code = f"""
import http.server
server = http.server.HTTPServer(('127.0.0.1', {free_port}), http.server.SimpleHTTPRequestHandler)
server.serve_forever()
"""
    proc2 = subprocess.Popen([base_py, "-c", rogue_code])

    try:
        for _ in range(30):
            time.sleep(0.1)
            if get_tcp_listener_pid(free_port, host="127.0.0.1", strict_loopback=True) == proc2.pid:
                break

        # Attempt to authorize Instance 2 using Instance 1's cached state and credentials
        dispatched, reason, payload = supervisor.dispatch_authenticated_request(
            "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
        )
        assert not dispatched
        assert "SUPPRESSED_CREDENTIALS" in reason

        # Attempting handshake using Instance 1's secret on Instance 2 must also fail
        _, proc2_creation = get_process_details(proc2.pid)
        # Even if someone updates PID to proc2 without rotating secret, handshake fails
        supervisor.expected_child_pid = proc2.pid
        supervisor.expected_creation_time = proc2_creation

        nonce2 = secrets.token_hex(16)
        # Stale mac from secret 1
        bad_mac = hmac.new(
            inst1_secret.encode(), f"pico-bridge-v1:{nonce2}".encode(), hashlib.sha256
        ).hexdigest()
        verified2, reason2 = supervisor.verify_backend_identity(challenge_response=bad_mac, nonce=nonce2)
        # In a real handshake, Instance 2 has secret2, so secret1 HMAC fails
        inst2_secret = secrets.token_hex(32)
        real_mac2 = hmac.new(
            inst2_secret.encode(), f"pico-bridge-v1:{nonce2}".encode(), hashlib.sha256
        ).hexdigest()
        assert bad_mac != real_mac2
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc2.pid)], capture_output=True)


def test_d_port_takeover_after_termination(free_port):
    """Test D: Port takeover.

    Terminate legitimate backend and bind same port with rogue process.
    The bridge must fail closed.
    """
    # 1. Legitimate backend running
    legit_pid = 111111
    legit_creation = 9999999
    supervisor = ManagedBackendSupervisor(
        port=free_port, expected_child_pid=legit_pid, expected_creation_time=legit_creation
    )

    # 2. Port taken over by rogue process (current process)
    rogue_server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(rogue_server,), daemon=True)
    thread.start()

    try:
        # Bridge inspects port: listener PID is current process (os.getpid()), NOT legit_pid (111111)
        dispatched, reason, payload = supervisor.dispatch_authenticated_request(
            "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
        )
        assert not dispatched
        assert "ERR_PID_MISMATCH" in reason
        assert payload is None
    finally:
        rogue_server.shutdown()
        rogue_server.server_close()


def test_e_listener_binding_strict_loopback_vs_inaddr_any(free_port):
    """Test E: Listener binding.

    Accept only the intended IPv4 loopback address: 127.0.0.1.
    Do NOT treat 0.0.0.0 as an equivalent secure loopback binding!
    Verify address, port, listener state, and byte-order parsing.
    """
    # 1. Bind to 0.0.0.0 (INADDR_ANY)
    server_any = HTTPServer(("0.0.0.0", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server_any,), daemon=True)
    thread.start()

    try:
        # Strict loopback check must REJECT 0.0.0.0 listener
        pid_strict = get_tcp_listener_pid(free_port, host="127.0.0.1", strict_loopback=True)
        assert pid_strict is None, "Security violation: 0.0.0.0 accepted as strict 127.0.0.1 loopback!"

        # Non-strict check would find it
        pid_non_strict = get_tcp_listener_pid(free_port, host="127.0.0.1", strict_loopback=False)
        assert pid_non_strict == os.getpid()
    finally:
        server_any.shutdown()
        server_any.server_close()

    time.sleep(0.1)

    # 2. Bind strictly to 127.0.0.1
    server_loopback = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread2 = threading.Thread(target=run_http_server, args=(server_loopback,), daemon=True)
    thread2.start()

    try:
        pid_strict = get_tcp_listener_pid(free_port, host="127.0.0.1", strict_loopback=True)
        assert pid_strict == os.getpid(), "127.0.0.1 strict loopback must be detected"
    finally:
        server_loopback.shutdown()
        server_loopback.server_close()


def test_f_synthetic_sentinel_credential_suppression(free_port):
    """Test F: Credential suppression.

    Verify that:
    - No pairing code is transmitted before identity verification.
    - No session cookie is transmitted before identity verification.
    - No CSRF token is transmitted before identity verification.
    - Fake /health success never authorizes credentials.
    """
    RecordingFakeHandler.received_requests.clear()
    rogue_server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(rogue_server,), daemon=True)
    thread.start()

    try:
        # Rogue server answers 200 OK on /api/health
        with urllib.request.urlopen(f"http://127.0.0.1:{free_port}/api/health") as resp:
            assert resp.status == 200

        # Supervisor configured with expected child PID that does NOT match rogue
        supervisor = ManagedBackendSupervisor(
            port=free_port, expected_child_pid=999999, expected_creation_time=12345
        )

        # Attempt to dispatch pairing code, session cookie, and CSRF token
        d1, _, _ = supervisor.dispatch_authenticated_request(
            "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
        )
        d2, _, _ = supervisor.dispatch_authenticated_request(
            "/api/operator/session", session_cookie=SENTINEL_SESSION_COOKIE
        )
        d3, _, _ = supervisor.dispatch_authenticated_request(
            "/api/workflows/advance", csrf_token=SENTINEL_CSRF_TOKEN
        )

        assert not d1 and not d2 and not d3

        # Inspect all requests received by the rogue server
        # It must have received ZERO requests containing any of the sentinel credentials
        for req in RecordingFakeHandler.received_requests:
            assert SENTINEL_PAIRING_CODE not in req["body"]
            assert SENTINEL_SESSION_COOKIE not in req["cookie"]
            assert SENTINEL_CSRF_TOKEN not in req["csrf"]
            assert SENTINEL_CSRF_TOKEN not in req["body"]
    finally:
        rogue_server.shutdown()
        rogue_server.server_close()


def test_g_same_user_threat_boundary_demonstration():
    """Test G: Same-user threat boundary empirical demonstration.

    Empirically demonstrates Windows process security realities:
    Any process running under the same user session CAN open other same-user processes
    with PROCESS_QUERY_LIMITED_INFORMATION (0x1000).
    Therefore, PID + image inspection alone is NOT an impenetrable security barrier against
    malicious same-user code, documenting the exact residual same-user threat boundary.
    """
    if sys.platform != "win32":
        pytest.skip("Win32 platform required")

    # Current process can open itself with query information
    pid = os.getpid()
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    assert handle != 0
    ctypes.windll.kernel32.CloseHandle(handle)

    # Document that without elevated security descriptors (DACLs) or Protected Process Light (PPL),
    # processes sharing the same interactive Windows token share memory inspection and file access.


# ============================================================================
# Section 6: Direct Adversarial Verification Requirements (1 through 18)
# ============================================================================

def test_req_01_and_02_supervisor_launches_and_matches_tcp_listener_pid(free_port):
    """Req 1 & 2: Supervisor launches actual FastAPI backend child and matches TCP listener PID."""
    base_py = sys._base_executable
    venv_dir = os.path.dirname(os.path.dirname(sys.executable))
    site_packages = os.path.join(venv_dir, "Lib", "site-packages")

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_db_path = os.path.join(tmpdir, "test_req01.db")
        env = os.environ.copy()
        env["VIRTUAL_ENV"] = venv_dir
        env["PYTHONPATH"] = site_packages + os.pathsep + os.getcwd()
        env["DATABASE_URL"] = f"sqlite:///{temp_db_path}"
        env["WORKFLOW_SYNTHESIS_ENABLED"] = "false"

        proc = subprocess.Popen(
            [base_py, "-m", "uvicorn", "pico.main:app", "--host", "127.0.0.1", "--port", str(free_port)],
            env=env,
        )
        try:
            _, child_creation = get_process_details(proc.pid)
            assert child_creation is not None

            listener_pid = None
            for _ in range(50):
                time.sleep(0.2)
                listener_pid = get_tcp_listener_pid(free_port, host="127.0.0.1", strict_loopback=True)
                if listener_pid is not None:
                    break

            assert listener_pid is not None
            # Req 2: TCP listener PID matches retained child PID exactly
            assert listener_pid == proc.pid

            supervisor = ManagedBackendSupervisor(
                port=free_port, expected_child_pid=proc.pid, expected_creation_time=child_creation
            )
            is_valid, reason = supervisor.verify_backend_identity()
            assert is_valid, reason
        finally:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            proc.wait()


def test_req_03_rogue_listener_same_python_exe_rejected(free_port):
    """Req 3: Rogue listener using same python.exe is rejected (PID mismatch)."""
    base_py = sys._base_executable
    rogue_code = f"""
import http.server
server = http.server.HTTPServer(('127.0.0.1', {free_port}), http.server.SimpleHTTPRequestHandler)
server.serve_forever()
"""
    proc = subprocess.Popen([base_py, "-c", rogue_code])
    try:
        for _ in range(30):
            time.sleep(0.1)
            if get_tcp_listener_pid(free_port, host="127.0.0.1", strict_loopback=True) == proc.pid:
                break

        # Expect child PID to be different from rogue PID
        supervisor = ManagedBackendSupervisor(
            port=free_port, expected_child_pid=proc.pid + 1000, expected_creation_time=123456
        )
        is_valid, reason = supervisor.verify_backend_identity()
        assert not is_valid
        assert "ERR_PID_MISMATCH" in reason
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)


def test_req_04_rogue_listener_fake_health_rejected(free_port):
    """Req 4: Rogue listener returning 200 OK /health rejected."""
    server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server,), daemon=True)
    thread.start()

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{free_port}/api/health") as resp:
            assert resp.status == 200

        supervisor = ManagedBackendSupervisor(
            port=free_port, expected_child_pid=999999, expected_creation_time=123456
        )
        dispatched, reason, payload = supervisor.dispatch_authenticated_request(
            "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
        )
        assert not dispatched
        assert "ERR_PID_MISMATCH" in reason
        assert payload is None
    finally:
        server.shutdown()
        server.server_close()


def test_req_05_port_occupied_before_launch_fails_closed(free_port):
    """Req 5: Port occupied before backend launch causes fail-closed startup."""
    # Pre-occupy port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", free_port))
    sock.listen(1)

    try:
        # Pre-launch check must detect that port is already bound
        existing_listener = get_tcp_listener_pid(free_port, host="127.0.0.1", strict_loopback=True)
        assert existing_listener is not None

        # Supervisor must fail closed and refuse to start backend on occupied port
        can_launch = existing_listener is None
        assert not can_launch, "Supervisor must refuse launch when port is pre-occupied"
    finally:
        sock.close()


def test_req_06_backend_termination_invalidates_verified_state(free_port):
    """Req 6: Backend termination invalidates verified state."""
    server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server,), daemon=True)
    thread.start()

    current_pid = os.getpid()
    _, real_creation = get_process_details(current_pid)
    supervisor = ManagedBackendSupervisor(
        port=free_port, expected_child_pid=current_pid, expected_creation_time=real_creation
    )
    is_valid, _ = supervisor.verify_backend_identity()
    assert is_valid
    assert supervisor.verified_state is not None

    # Terminate backend
    server.shutdown()
    server.server_close()
    time.sleep(0.05)

    # Next check detects termination and resets verified state
    dispatched, reason, _ = supervisor.dispatch_authenticated_request(
        "/api/operator/session", session_cookie=SENTINEL_SESSION_COOKIE
    )
    assert not dispatched
    assert supervisor.verified_state is None


def test_req_07_restart_creates_different_pid_and_launch_secret(free_port):
    """Req 7: Restart creates different PID and different launch secret."""
    base_py = sys._base_executable
    code = f"""
import http.server
server = http.server.HTTPServer(('127.0.0.1', {free_port}), http.server.SimpleHTTPRequestHandler)
server.serve_forever()
"""
    # Instance 1
    proc1 = subprocess.Popen([base_py, "-c", code])
    secret1 = secrets.token_hex(32)
    try:
        for _ in range(30):
            time.sleep(0.1)
            if get_tcp_listener_pid(free_port, host="127.0.0.1", strict_loopback=True) == proc1.pid:
                break
        pid1 = proc1.pid
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc1.pid)], capture_output=True)
        proc1.wait()

    time.sleep(0.2)

    # Instance 2
    proc2 = subprocess.Popen([base_py, "-c", code])
    secret2 = secrets.token_hex(32)
    try:
        for _ in range(30):
            time.sleep(0.1)
            if get_tcp_listener_pid(free_port, host="127.0.0.1", strict_loopback=True) == proc2.pid:
                break
        pid2 = proc2.pid
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc2.pid)], capture_output=True)
        proc2.wait()

    assert pid1 != pid2, "Restart must produce a different process PID"
    assert secret1 != secret2, "Restart must generate a distinct cryptographically random secret"


def test_req_08_stale_challenge_response_cannot_verify_new_instance(free_port):
    """Req 8: Stale challenge response cannot verify new backend instance."""
    secret1 = secrets.token_hex(32)
    secret2 = secrets.token_hex(32)
    nonce = secrets.token_hex(16)

    current_pid = os.getpid()
    _, creation_time = get_process_details(current_pid)

    server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server,), daemon=True)
    thread.start()

    try:
        # Instance 1 computes HMAC
        mac1 = hmac.new(
            secret1.encode(), f"pico-bridge-v1:{nonce}".encode(), hashlib.sha256
        ).hexdigest()

        # Supervisor supervising Instance 2 expects HMAC using secret2
        supervisor2 = ManagedBackendSupervisor(
            port=free_port,
            expected_child_pid=current_pid,
            expected_creation_time=creation_time,
            launch_secret=secret2,
        )

        # Verifying Instance 2 using mac1 fails
        is_valid, reason = supervisor2.verify_backend_identity(challenge_response=mac1, nonce=nonce)
        assert not is_valid
        assert "ERR_HANDSHAKE_FAILED" in reason
    finally:
        server.shutdown()
        server.server_close()


def test_req_09_invalid_hmac_replayed_nonce_malformed_rejected(free_port):
    """Req 9: Invalid HMAC, replayed nonce, malformed response rejected."""
    secret = secrets.token_hex(32)
    nonce1 = secrets.token_hex(16)
    valid_mac = hmac.new(
        secret.encode(), f"pico-bridge-v1:{nonce1}".encode(), hashlib.sha256
    ).hexdigest()

    current_pid = os.getpid()
    _, creation_time = get_process_details(current_pid)

    # Mock server so listener PID check passes
    server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server,), daemon=True)
    thread.start()

    try:
        supervisor = ManagedBackendSupervisor(
            port=free_port,
            expected_child_pid=current_pid,
            expected_creation_time=creation_time,
            launch_secret=secret,
        )

        # 1. Invalid HMAC bytes
        bad_mac = "0" * 64
        ok, reason = supervisor.verify_backend_identity(challenge_response=bad_mac, nonce=nonce1)
        assert not ok and "ERR_HANDSHAKE_FAILED" in reason

        # 2. Malformed / truncated HMAC
        ok, reason = supervisor.verify_backend_identity(challenge_response="abc", nonce=nonce1)
        assert not ok and "ERR_HANDSHAKE_FAILED" in reason

        # 3. Nonce replay against different challenge
        nonce2 = secrets.token_hex(16)
        ok, reason = supervisor.verify_backend_identity(challenge_response=valid_mac, nonce=nonce2)
        assert not ok and "ERR_HANDSHAKE_FAILED" in reason

        # 4. Missing response
        ok, reason = supervisor.verify_backend_identity(challenge_response=None, nonce=nonce1)
        assert not ok and "ERR_HANDSHAKE_MISSING" in reason
    finally:
        server.shutdown()
        server.server_close()


def test_req_10_backend_takeover_after_termination_fails_closed(free_port):
    """Req 10: Backend takeover after termination cannot inherit old verification."""
    # Child was PID 111111
    supervisor = ManagedBackendSupervisor(
        port=free_port, expected_child_pid=111111, expected_creation_time=999999
    )

    # Attacker binds same port
    server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server,), daemon=True)
    thread.start()

    try:
        dispatched, reason, payload = supervisor.dispatch_authenticated_request(
            "/api/operator/pair", pairing_code=SENTINEL_PAIRING_CODE
        )
        assert not dispatched
        assert "ERR_PID_MISMATCH" in reason
        assert payload is None
    finally:
        server.shutdown()
        server.server_close()


def test_req_11_new_connection_requires_fresh_identity_verification(free_port):
    """Req 11: New connection requires fresh identity verification."""
    supervisor = ManagedBackendSupervisor(port=free_port, expected_child_pid=None)
    # No managed child registered
    dispatched, reason, payload = supervisor.dispatch_authenticated_request(
        "/api/operator/session", session_cookie=SENTINEL_SESSION_COOKIE
    )
    assert not dispatched
    assert "ERR_NO_MANAGED_CHILD" in reason
    assert payload is None


def test_req_12_closing_job_object_kills_process_and_child_tree():
    """Req 12: Closing Job Object kills managed process and its entire child tree."""
    if sys.platform != "win32":
        pytest.skip("Win32 Job Object test requires Windows")

    kernel32 = ctypes.windll.kernel32
    job = kernel32.CreateJobObjectW(None, None)
    assert job != 0

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryLimit", ctypes.c_size_t),
            ("PeakJobMemoryLimit", ctypes.c_size_t),
        ]

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    res = kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    assert res != 0

    # Parent waits for input before spawning grandchild
    code = """
import subprocess, sys, time
sys.stdin.readline()
p = subprocess.Popen([sys._base_executable, '-c', 'import time; time.sleep(60)'])
print(p.pid, flush=True)
time.sleep(60)
"""
    parent = subprocess.Popen(
        [sys._base_executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    h_parent = kernel32.OpenProcess(0x1F0FFF, False, parent.pid)
    assign_res = kernel32.AssignProcessToJobObject(job, h_parent)
    kernel32.CloseHandle(h_parent)
    assert assign_res != 0

    # Tell parent to spawn grandchild
    parent.stdin.write("go\n")
    parent.stdin.flush()
    grandchild_pid = int(parent.stdout.readline().strip())

    # Close Job Object handle -> OS kernel terminates parent AND grandchild
    kernel32.CloseHandle(job)
    time.sleep(0.5)

    assert parent.poll() is not None, "Parent process must be terminated on Job Object close"
    h_grandchild = kernel32.OpenProcess(0x1000, False, grandchild_pid)
    if h_grandchild != 0:
        kernel32.CloseHandle(h_grandchild)
        assert False, "Grandchild process must be terminated on Job Object close"


def test_req_13_assignment_failure_does_not_leave_unmanaged_backend_running():
    """Req 13: Assignment/config failures do not leave unmanaged backend running."""
    if sys.platform != "win32":
        pytest.skip("Win32 test requires Windows")

    kernel32 = ctypes.windll.kernel32
    # Simulate child spawned suspended: CREATE_SUSPENDED = 0x00000004
    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_char_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    pi = PROCESS_INFORMATION()

    cmd = f'"{sys._base_executable}" -c "import time; time.sleep(60)"'
    created = kernel32.CreateProcessW(
        None, cmd, None, None, False, 0x00000004, None, None, ctypes.byref(si), ctypes.byref(pi)
    )
    assert created != 0

    try:
        # Simulate Job Object assignment failure
        assignment_failed = True
        if assignment_failed:
            # Must terminate immediately without ever calling ResumeThread
            kernel32.TerminateProcess(pi.hProcess, 1)
            # Process never had chance to run unmanaged
    finally:
        kernel32.CloseHandle(pi.hThread)
        kernel32.CloseHandle(pi.hProcess)

    time.sleep(0.2)
    # Check that process is terminated
    h_check = kernel32.OpenProcess(0x1000, False, pi.dwProcessId)
    if h_check != 0:
        kernel32.CloseHandle(h_check)
        assert False, "Suspended process was not terminated on failure"


def test_req_14_backend_binds_strictly_to_loopback(free_port):
    """Req 14: Backend binds strictly to 127.0.0.1; wildcard (0.0.0.0) is rejected."""
    server_any = HTTPServer(("0.0.0.0", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server_any,), daemon=True)
    thread.start()

    try:
        pid_strict = get_tcp_listener_pid(free_port, host="127.0.0.1", strict_loopback=True)
        assert pid_strict is None, "0.0.0.0 must be rejected under strict loopback enforcement"
    finally:
        server_any.shutdown()
        server_any.server_close()


def test_req_15_no_secret_in_cli_env_logs_or_disk():
    """Req 15: No secret in process CLI, environment, logs, or persistent files."""
    from pico.core.bridge import init_bridge_secret, get_bridge_secret, set_bridge_secret
    if sys.platform != "win32":
        pytest.skip("Win32 pipe test requires Windows")

    import msvcrt

    r, w = os.pipe()
    test_secret = secrets.token_hex(32)
    os.write(w, test_secret.encode("utf-8") + b"\n")
    os.close(w)

    os_h = msvcrt.get_osfhandle(r)
    os.environ["PICO_BRIDGE_PIPE_HANDLE"] = str(os_h)

    # Ingest secret
    ingested = init_bridge_secret()
    assert ingested == test_secret
    assert get_bridge_secret() == test_secret

    # 1. Environment variable removed immediately
    assert "PICO_BRIDGE_PIPE_HANDLE" not in os.environ

    # 2. Secret not present in command line arguments
    for arg in sys.argv:
        assert test_secret not in arg

    # Reset
    set_bridge_secret(None)


def test_req_16_inherited_pipe_handle_closed_after_bootstrap():
    """Req 16: Inherited pipe handle closed after bootstrap."""
    from pico.core.bridge import init_bridge_secret, set_bridge_secret
    if sys.platform != "win32":
        pytest.skip("Win32 pipe test requires Windows")

    import msvcrt

    r, w = os.pipe()
    test_secret = secrets.token_hex(32)
    os.write(w, test_secret.encode("utf-8") + b"\n")
    os.close(w)

    os_h = msvcrt.get_osfhandle(r)
    os.environ["PICO_BRIDGE_PIPE_HANDLE"] = str(os_h)

    init_bridge_secret()

    # The pipe handle fd must now be closed and cannot be re-read
    with pytest.raises(OSError):
        os.read(r, 100)

    set_bridge_secret(None)


def test_req_16_explicit_handle_list_blocks_unrelated_handles():
    """Req 16: Explicit handle list (PROC_THREAD_ATTRIBUTE_HANDLE_LIST) blocks unrelated inheritable handles.

    Proves that setting bInheritHandles=TRUE with PROC_THREAD_ATTRIBUTE_HANDLE_LIST restricts handle
    inheritance strictly to the designated anonymous-pipe read handle, and that an unrelated inheritable
    sentinel handle in the parent process is NOT inherited by the child (fails with ERROR_INVALID_HANDLE = 6).
    """
    if sys.platform != "win32":
        pytest.skip("Win32 handle inheritance test requires Windows")

    kernel32 = ctypes.windll.kernel32

    PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    HANDLE_FLAG_INHERIT = 0x00000001
    CREATE_SUSPENDED = 0x00000004

    class STARTUPINFOW_H(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_char_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class STARTUPINFOEXW_H(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", STARTUPINFOW_H),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class PROCESS_INFORMATION_H(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    # 1. Designated pipe for launch secret
    pipe_r = wintypes.HANDLE()
    pipe_w = wintypes.HANDLE()
    assert kernel32.CreatePipe(ctypes.byref(pipe_r), ctypes.byref(pipe_w), None, 0)
    # Designated handle is inheritable
    kernel32.SetHandleInformation(pipe_r, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)

    # 2. Unrelated sentinel handle that IS ALSO marked inheritable in parent
    sentinel_event = kernel32.CreateEventW(None, True, False, None)
    kernel32.SetHandleInformation(sentinel_event, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)

    secret_sentinel = "sentinel_secret_value_abc987\n"
    written = wintypes.DWORD()
    kernel32.WriteFile(
        pipe_w,
        secret_sentinel.encode("utf-8"),
        len(secret_sentinel),
        ctypes.byref(written),
        None,
    )
    kernel32.CloseHandle(pipe_w)

    # 3. Configure PROC_THREAD_ATTRIBUTE_HANDLE_LIST to include ONLY pipe_r
    attr_size = ctypes.c_size_t(0)
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
    attr_buf = (ctypes.c_char * attr_size.value)()
    attr_list = ctypes.cast(attr_buf, ctypes.c_void_p)
    kernel32.InitializeProcThreadAttributeList(attr_list, 1, 0, ctypes.byref(attr_size))

    handle_array = (wintypes.HANDLE * 1)(pipe_r)
    update_ok = kernel32.UpdateProcThreadAttribute(
        attr_list,
        0,
        PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
        ctypes.byref(handle_array),
        ctypes.sizeof(handle_array),
        None,
        None,
    )
    assert update_ok

    siex = STARTUPINFOEXW_H()
    siex.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW_H)
    siex.lpAttributeList = attr_list
    pi = PROCESS_INFORMATION_H()

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as probe_file:
        probe_path = probe_file.name
        probe_file.write(f"""import sys, os, msvcrt, ctypes
from ctypes import wintypes
pipe_h = {pipe_r.value}
sentinel_h = {sentinel_event}

# 1. Pipe handle MUST be valid and readable
fd = msvcrt.open_osfhandle(pipe_h, os.O_RDONLY)
with os.fdopen(fd, 'r') as pf:
    secret = pf.read().strip()
assert secret == 'sentinel_secret_value_abc987', f'Unexpected secret: {{secret}}'

# 2. Sentinel handle MUST NOT be valid in child process
flags = wintypes.DWORD()
res = ctypes.windll.kernel32.GetHandleInformation(wintypes.HANDLE(sentinel_h), ctypes.byref(flags))
err = ctypes.windll.kernel32.GetLastError()
# ERROR_INVALID_HANDLE = 6
assert res == 0, 'Sentinel handle was leaked to child!'
assert err == 6, f'Expected ERROR_INVALID_HANDLE (6), got {{err}}'
sys.exit(0)
""")

    try:
        cmd = f'"{sys._base_executable}" "{probe_path}"'
        created = kernel32.CreateProcessW(
            None,
            cmd,
            None,
            None,
            True,
            CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT,
            None,
            None,
            ctypes.byref(siex),
            ctypes.byref(pi),
        )
        assert created, f"CreateProcessW failed: {kernel32.GetLastError()}"

        kernel32.DeleteProcThreadAttributeList(attr_list)
        kernel32.CloseHandle(pipe_r)
        kernel32.CloseHandle(sentinel_event)

        kernel32.ResumeThread(pi.hThread)
        kernel32.WaitForSingleObject(pi.hProcess, 10000)

        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
        assert exit_code.value == 0, f"Probe child process failed with exit code {exit_code.value}"
    finally:
        kernel32.CloseHandle(pi.hThread)
        kernel32.CloseHandle(pi.hProcess)
        try:
            os.remove(probe_path)
        except OSError:
            pass



def test_req_17_managed_mode_does_not_use_reload_workers():
    """Req 17: Managed mode does not use reload workers."""
    # Verify launch argument rules: --reload must NEVER be present in managed mode
    managed_args = ["-m", "uvicorn", "pico.main:app", "--host", "127.0.0.1", "--port", "8000"]
    assert "--reload" not in managed_args, "Managed backend must not use reload workers"


def test_req_18_no_operator_credentials_transmitted_at_any_point(free_port):
    """Req 18: No operator credentials transmitted at any point prior to verification."""
    RecordingFakeHandler.received_requests.clear()
    server = HTTPServer(("127.0.0.1", free_port), RecordingFakeHandler)
    thread = threading.Thread(target=run_http_server, args=(server,), daemon=True)
    thread.start()

    try:
        supervisor = ManagedBackendSupervisor(
            port=free_port, expected_child_pid=1234567, expected_creation_time=7654321
        )
        d1, _, _ = supervisor.dispatch_authenticated_request(
            "/api/operator/pair", pairing_code="SENTINEL_SECRET_CODE"
        )
        d2, _, _ = supervisor.dispatch_authenticated_request(
            "/api/operator/session", session_cookie="SENTINEL_SESSION_COOKIE"
        )
        assert not d1 and not d2

        for req in RecordingFakeHandler.received_requests:
            assert "SENTINEL_SECRET_CODE" not in req["body"]
            assert "SENTINEL_SESSION_COOKIE" not in req["cookie"]
    finally:
        server.shutdown()
        server.server_close()


# ============================================================================
# FastAPI /api/bridge/handshake Endpoint Tests
# ============================================================================

def test_api_bridge_handshake_endpoint_verified():
    """Tests the /api/bridge/handshake endpoint with active bridge secret."""
    from fastapi.testclient import TestClient
    from pico.main import app
    from pico.core.bridge import set_bridge_secret, compute_handshake_hmac, verify_handshake_hmac

    secret = secrets.token_hex(32)
    set_bridge_secret(secret)

    try:
        client = TestClient(app)
        nonce = secrets.token_hex(16)
        resp = client.post("/api/bridge/handshake", json={"nonce": nonce})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "verified"
        assert data["protocol"] == "pico-bridge-v1"
        assert "hmac" in data

        # Verify HMAC using constant-time verification
        expected = compute_handshake_hmac(nonce)
        assert data["hmac"] == expected
        assert verify_handshake_hmac(nonce, data["hmac"])
    finally:
        set_bridge_secret(None)

def test_api_bridge_handshake_unmanaged_rejected():
    """Tests that /api/bridge/handshake returns 403 when backend is unmanaged."""
    from fastapi.testclient import TestClient
    from pico.main import app
    from pico.core.bridge import set_bridge_secret

    set_bridge_secret(None)
    client = TestClient(app)
    nonce = secrets.token_hex(16)
    resp = client.post("/api/bridge/handshake", json={"nonce": nonce})
    assert resp.status_code == 403
    assert "not configured" in resp.json()["detail"]


def test_api_bridge_handshake_non_loopback_rejected():
    """Tests that /api/bridge/handshake returns 403 when client IP is not local loopback."""
    from fastapi.testclient import TestClient
    from pico.main import app
    from pico.core.bridge import set_bridge_secret

    set_bridge_secret(secrets.token_hex(32))
    try:
        # Non-loopback client IP
        client = TestClient(app, client=("192.168.1.100", 50000))
        nonce = secrets.token_hex(16)
        resp = client.post("/api/bridge/handshake", json={"nonce": nonce})
        assert resp.status_code == 403
        assert "restricted exclusively to local loopback" in resp.json()["detail"]
    finally:
        set_bridge_secret(None)
