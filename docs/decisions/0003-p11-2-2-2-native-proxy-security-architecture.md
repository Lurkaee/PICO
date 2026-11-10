# ADR 0003: P11.2.2.2 Native Loopback Proxy & Operator Session Security Architecture

- **Status**: Accepted (Milestone P11.2.2.2 Slice 1A Qualified)
- **Date**: 2026-11-09
- **Authors**: PICO Core & Desktop Architecture Team
- **Deciders**: Systems Architect, Security Lead
- **Applies to**: Milestone P11.2.2.2 (Tauri Native Loopback Proxy & Operator Session Security)
- **Supersedes**: ADR 0003 Stage 0.3.1 Draft
- **Related ADRs**:
  - [ADR 0001: Windows Desktop Companion Architecture](0001-windows-desktop-companion-architecture.md)
  - [ADR 0002: P11.2.2 Secure Loopback Bridge Qualification](0002-p11-2-2-secure-loopback-bridge-qualification.md)

---

## 1. Context and Problem Statement

Milestone P11.2.2.1 successfully established native child process supervision on Windows 11:
- The Tauri native host compiles natively under MSVC (`cl.exe 19.44`, `link.exe 14.44`, Windows SDK `10.0.26100.0`).
- Process containment is enforced via a Win32 Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
- Startup bootstrap uses an anonymous Win32 pipe with explicit handle inheritance (`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`) to deliver an ephemeral 256-bit cryptographic secret generated via Windows CNG `BCryptGenRandom`.
- Identity verification is enforced prior to entering `Online` state via loopback TCP `POST /api/bridge/handshake` with fresh 32-character challenge nonces and constant-time HMAC-SHA256 validation.
- Dual-handle liveness monitoring (`launcher_handle` + `listener_handle`) tracks both `.venv\Scripts\python.exe` and the active child `python.exe` listening on `127.0.0.1:8000`.

In Milestone P11.2.2.2, the desktop companion must interact with PICO Core's protected endpoints without violating core security invariants:
1. **Secrets must never reach models or the browser** (Core Invariant #4). Operator session cookies and CSRF tokens must never exist in frontend DOM, webview memory, or browser persistence.
2. **Untrusted external content cannot grant permissions** (Core Invariant #8). The webview must never act as an arbitrary HTTP forwarder.
3. **Consequential actions require explicit authorization** (Core Invariant #7). Policy A human approval cannot be bypassed.

### The Fundamental Dilemma: Request Authentication vs. Credential Confidentiality
In Stage 0.1, a per-request HMAC proof protocol (`X-Pico-Bridge-Proof`) was proposed. However, a critical distinction exists between **authenticating a request** and **protecting the confidentiality of credentials in transit**:
- An HMAC header proves that the native supervisor generated the request.
- However, when standard HTTP over TCP is used, the client must transmit the HTTP request headers (including `Cookie: pico_operator_session=...` and `X-CSRF-Token`) across the TCP connection *before* receiving any response from the server.
- If a rogue local process has bound `127.0.0.1:8000` (for example, following an unobserved Core crash or a race condition during port release), the client writing an HTTP request to that socket sends the plaintext operator session credentials directly into the rogue process's read buffer!
- The rogue process does not need to forge an HMAC response; it has already captured the session cookie and CSRF token.

### Precise Definition of the Zero-Payload Invariant
Establishing a TCP connection necessarily exchanges network-layer protocol packets (SYN, SYN-ACK, ACK). Therefore, the security invariant is precisely defined as:
> **Zero HTTP/application payload bytes, session credentials, CSRF tokens or user data are transmitted before peer identity verification.**

---

## 2. Evaluation of Windows Transport Alternatives for Identity & Confidentiality

To establish server identity and guarantee credential confidentiality on Windows 11, four transport alternatives were evaluated against the PICO runtime environment (Tauri v2 in Rust, Python 3.12+, Uvicorn 0.54.0, FastAPI 0.143.3, Starlette 1.8.0).

### Alternative A: Per-Launch TLS with Pinning
- **Mechanism**: At startup, supervisor generates self-signed X.509 certificate and private key in memory; pinned by Rust client via `rustls`. Uvicorn launched with TLS enabled.
- **Drawback & Failure Mode**: Python's standard library `ssl.SSLContext.load_cert_chain(certfile, keyfile)` strictly requires filesystem paths on disk. Writing ephemeral TLS private keys to the Windows filesystem introduces an unprivileged disk-inspection attack surface (antivirus scans, unprivileged local tools, incomplete file deletion on crash).
- **Verdict**: **Rejected**. Introduces disk-based secret leakage risks.

### Alternative B: Parent-Owned Pre-Bound Winsock Socket Handoff (Revisited)
- **Mechanism**: Tauri parent binds `127.0.0.1:8000` with `SO_EXCLUSIVEADDRUSE`, duplicates socket handle via `WSADuplicateSocketW` / `socket.share()`, and Python reconstructs it via `socket.fromshare()`.
- **Empirical Feasibility Findings**:
  1. Python `socket.share()` and `socket.fromshare()` succeed on Windows 11.
  2. Uvicorn can serve on a reconstructed socket via `server.serve(sockets=[sock])`.
  3. **Launcher Shim Obstacle**: Spawning via `.venv\Scripts\python.exe` executes a launcher shim that spawns `python.exe` as a child. `WSADuplicateSocketW` requires the *target* worker PID. Supplying the launcher PID to `WSADuplicateSocketW` causes `WSAEINVAL` when the actual interpreter tries to import it.
  4. **Direct Launch Complexity**: Direct launch of `python.exe` requires manual configuration of `VIRTUAL_ENV`, `PYTHONPATH`, and `sys.prefix`, creating risks of runtime environment drift.
- **Verdict**: **Secondary Concept Retained** (exclusive port check), but not selected as primary transport due to child-shim process coordination complexity.

### Alternative C: Connection-Specific Physical Socket Verification (SELECTED)
- **Mechanism**:
  1. Native client establishes a raw TCP stream to `127.0.0.1:8000`.
  2. Windows kernel assigns a local ephemeral client port (`client_port`).
  3. **Before transmitting any application byte**, the native client queries `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_ALL)` via `iphlpapi.dll`.
  4. Client locates the exact **reverse server-side four-tuple**: `(Local: 127.0.0.1:8000, Remote: 127.0.0.1:client_port)`.
  5. Client validates that `dwOwningPid` belongs to the supervisor's private Win32 Job Object (`job_object.contains_process(dwOwningPid)`).
  6. **Fail-Closed Gate**: If `dwOwningPid` is NOT in the Job Object, or if lookup fails, the native client immediately closes the socket with **zero application bytes transmitted**.
  7. **Verified Channel**: If `dwOwningPid` matches the Job Object child, the peer process is proven genuine. The OS kernel enforces socket isolation, preventing connection hijacking.
- **Verdict**: **SELECTED AS PRIMARY TRANSPORT ARCHITECTURE**. Requires zero disk writes, works natively with standard Uvicorn/FastAPI CLI, and guarantees zero credential transmission to unauthenticated listeners.

### Alternative D: Windows Named Pipes (`\\.\pipe\...`)
- **Mechanism**: IPC routed over Windows Named Pipes secured by kernel DACLs.
- **Drawback & Failure Mode**: Uvicorn on Windows has no native Named Pipe transport. Would require writing a custom Python ASGI server adapter from scratch.
- **Verdict**: **Rejected**. Unacceptable implementation complexity.

### Windows Transport Comparison Matrix

| Evaluation Dimension | Alt A: Per-Launch TLS | Alt B: Winsock Handoff | Alt C: TCP Four-Tuple Verification (Selected) | Alt D: Named Pipes |
|---|---|---|---|---|
| **Zero App Bytes Before Verification** | Yes (TLS Handshake) | Yes (Kernel Lock) | **Yes (Pre-flight PID check before write)** | Yes (DACL Check) |
| **No Secrets on Disk** | No (Python requires file paths) | Yes | **Yes (Pure in-memory OS query)** | Yes |
| **Uvicorn Windows Compatibility** | Yes | Partial (requires custom script) | **Yes (Standard Uvicorn TCP listener)** | No (No ASGI Named Pipe server) |
| **Protection Against Port Stealing** | High | Maximum | **High (Fails closed on rogue PID)** | Maximum |
| **Implementation Complexity** | High | High | **Moderate (Win32 `GetExtendedTcpTable`)** | Extreme |
| **SSE Streaming Support** | Yes | Yes | **Yes (Standard HTTP/1.1 SSE)** | Custom |

---

## 3. Actual Windows TCP Table Semantics & Reverse Four-Tuple Matching

### 3.1 Empirical TCP Table Inspection (`GetExtendedTcpTable`)
In a controlled Windows 11 experiment connecting Client (PID 35016) to Server (PID 46388):

```
Client-side row:
  Local IP:    127.0.0.1
  Local Port:  61362 (ephemeral)
  Remote IP:   127.0.0.1
  Remote Port: 61361 (server)
  State:       ESTABLISHED (5)
  dwOwningPid: 35016 (CLIENT PID)

Server-side row (Reverse Four-Tuple):
  Local IP:    127.0.0.1
  Local Port:  61361 (server)
  Remote IP:   127.0.0.1
  Remote Port: 61362 (ephemeral)
  State:       ESTABLISHED (5)
  dwOwningPid: 46388 (SERVER PID)
```

**Critical Architectural Truth**:
The client-side TCP table row's `dwOwningPid` is the **client itself**. It does not identify the remote peer. To identify the server process, the client must search for the **reverse four-tuple** on the server endpoint.

### 3.2 Exact Four-Tuple Matching Algorithm
Given the client socket's `local_addr = (127.0.0.1, client_port)` and `peer_addr = (127.0.0.1, server_port)`:
1. Query `GetExtendedTcpTable(buffer, &size, TRUE, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)`.
2. Iterate through `MIB_TCPROW_OWNER_PID` entries.
3. Check state: `row.dwState == 5` (`MIB_TCP_STATE_ESTAB`).
4. Convert ports and addresses with explicit endianness handling:
   - `row_local_port = ntohs((row.dwLocalPort & 0xFFFF) as u16)`
   - `row_remote_port = ntohs((row.dwRemotePort & 0xFFFF) as u16)`
   - `row_local_addr = row.dwLocalAddr` (matched against `peer_addr.ip()` little-endian DWORD)
   - `row_remote_addr = row.dwRemoteAddr` (matched against `local_addr.ip()` little-endian DWORD)
5. Exact Match Predicate:
   - `row_local_addr == peer_addr.ip()` AND `row_local_port == peer_addr.port()`
   - `row_remote_addr == local_addr.ip()` AND `row_remote_port == local_addr.port()`
6. **Uniqueness Requirement**: There must be **exactly one** matching row. If 0 rows match (e.g. race during connection teardown) or multiple rows match, the connection is rejected immediately.
7. **Address Family Limitation**: Production listener is strictly restricted to IPv4 loopback (`127.0.0.1`) until IPv6 qualification (`AF_INET6` / `MIB_TCP6ROW_OWNER_PID`) is complete.

---

## 4. Process Identity, Handle Retention & Runtime Creation-Time Model

### 4.1 Clarified Worker Creation-Time Model
P11.2.2.1 manages two process layers:
- The `.venv` launcher stub (`.venv\Scripts\python.exe`).
- The actual Python runtime worker (`python.exe` listening on `127.0.0.1:8000`).

The worker's creation timestamp cannot be compared directly to the launcher's creation timestamp. Instead:
1. **Initial Discovery**: Supervisor discovers the server-side PID via reverse four-tuple lookup.
2. **Open & Retain Handle**: Supervisor calls `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, FALSE, server_pid)`.
3. **Verify Liveness**: Call `GetExitCodeProcess(hProcess, &exit_code)`. Must equal `STILL_ACTIVE` (259).
4. **Job Object Membership**: Call `IsProcessInJob(hProcess, job_object.raw_handle(), &is_in_job)`. Must equal `TRUE`.
5. **Initial Core Challenge**: Complete the credential-free server HMAC challenge on that exact physical connection.
6. **Record Pinned Worker**: Once the challenge succeeds, record the verified runtime worker PID, creation time (via `GetProcessTimes`), and retained process handle.
7. **Future Connection Verification**: Subsequent physical connections verify that the server-side PID matches the pinned runtime worker handle and creation timestamp. Any unexpected PID change triggers immediate fail-closed shutdown.

---

## 5. Single-Socket Hyper HTTP/1 Transport Proof (Stage 0.3.1 Prototype Results)

An isolated prototype was developed and verified in `scratch/hyper_prototype` using the MSVC toolchain:
- **Rust Toolchain**: `rustc 1.89.0`, MSVC `cl.exe 19.44`, `link.exe 14.44`.
- **Exact Dependency Versions**: `tokio 1.54.1`, `hyper 1.12.0`, `hyper-util 0.1.21`, `http-body-util 0.1.5`, `http 1.5.0`, `bytes 1.12.1`.
- **Production Isolation**: Tested strictly in scratch; **zero** dependencies added to PICO's production `Cargo.toml`.

### 5.1 Same-Socket Proof Mechanics
1. Supervisor creates one raw `tokio::net::TcpStream::connect("127.0.0.1:8000").await`.
2. Extracts `stream.local_addr()?` and `stream.peer_addr()?`.
3. Calls Win32 `GetExtendedTcpTable` to verify the reverse four-tuple and checks `IsProcessInJob`.
4. Wraps the **exact same `TcpStream`** with `hyper_util::rt::TokioIo::new(stream)`.
5. Drives the connection using Hyper's single-connection driver:
   ```rust
   let (mut sender, conn) = hyper::client::conn::http1::handshake(io).await?;
   tokio::spawn(async move {
       if let Err(err) = conn.await {
           // Connection terminated
       }
   });
   ```
6. **Zero Connection Pooling**: No connection pool is used.
7. **Zero Silent Reconnect**: If the socket disconnects, Hyper's `sender.send_request()` returns an error immediately. No replacement socket is opened automatically.
8. **No Proxies / No Redirects**: Direct loopback socket connection completely bypasses proxy and redirect logic.

### 5.2 Connection-Specific Core Identity Challenge Protocol
Every new physical TCP connection must complete a credential-free server identity challenge:
1. Establish TCP connection.
2. Verify reverse four-tuple owning PID and Job Object containment.
3. Pass stream into `hyper::client::conn::http1::handshake`.
4. Send only a fresh, credential-free challenge: `POST /api/bridge/handshake` with `{ "nonce": "<fresh_32_char_nonce>" }`.
5. Verify the response HMAC against the launch secret.
6. **Only after successful response verification**, permit authenticated requests (carrying cookies and CSRF tokens) to be dispatched across that same connection.

The initial handshake contains **zero** credentials: no session cookies, no CSRF tokens, no pairing codes, and no user data.

---

## 6. Empirical Adversarial Scenario Outcomes (Stage 0.3.1 Proof)

The isolated prototype was subjected to all required adversarial scenarios:

1. **Scenario A — Legitimate Managed Server**:
   - Server spawned in Job Object on port `54265`.
   - Reverse four-tuple matched owning PID `19512`.
   - Handshake challenge sent and verified over the Hyper connection.
   - Harmless authenticated test request dispatched on the exact same connection.
   - **Result: PASSED.**
2. **Scenario B — Rogue Listener (Outside Job Object)**:
   - Rogue listener spawned on port `54267`.
   - Reverse four-tuple lookup resolved to PID `19512` (not in Job Object).
   - Security gate triggered: connection closed before any HTTP request was written.
   - Rogue listener received **zero** application/HTTP bytes.
   - **Result: PASSED.**
3. **Scenario C — Rogue Process Inside Job Object**:
   - Harmless process simulated inside Job Object but lacking the 256-bit launch secret.
   - Four-tuple and Job Object checks passed, but server challenge returned an invalid HMAC.
   - Security gate triggered: connection closed immediately before credential dispatch.
   - Rogue process received **zero** application credential bytes.
   - **Result: PASSED.**
4. **Scenario D — Server Termination & Port Takeover**:
   - Connection established and verified; server terminated abruptly.
   - Rogue takeover listener bound to the released port.
   - Subsequent `sender.send_request()` on the verified connection failed closed.
   - Hyper did **not** silently reconnect; takeover listener received **zero** bytes.
   - **Result: PASSED.**
5. **Scenario E — New Physical Connection Re-Verification**:
   - Demonstrated that every new `TcpStream` executes the full reverse four-tuple and challenge handshake. Verified identity is never blindly inherited across sockets.
   - **Result: PASSED.**
6. **Scenario F — Process PID Reuse & Retained Handle Liveness**:
   - Retaining the process handle ensured `GetExitCodeProcess` accurately reflected process state (`STILL_ACTIVE` vs terminated), preventing stale trust on recycled PIDs.
   - **Result: PASSED.**

---

## 7. Transport Security Wording, Credential-Release Gate & Threat Model Limitations

The loopback HTTP transport is **not** encrypted on the wire and does **not** claim cryptographic confidentiality. Its security guarantees are precisely bounded as a **credential-release gate**:

1. **Same-Socket Peer Verification**: Physical OS peer identity is resolved on the exact Windows TCP socket via reverse four-tuple lookup (`GetExtendedTcpTable`) and bound to the supervisor's private Job Object before transmitting any application data.
2. **Authenticated Core Process**: Execution of the mutual HMAC startup challenge over that exact socket proves that the server process holds the ephemeral 256-bit launch secret, establishing it as the genuine, authenticated Core process.
3. **Credential-Release Gate**: The transport serves strictly as a credential-release gate: **no credentials before Core authentication**. Operator session cookies, CSRF tokens, and application payloads are released to the socket if and only if OS peer verification and Core HMAC challenge verification have both succeeded.
4. **No Wire Encryption**: Local loopback TCP packets are transmitted as plaintext across the Windows loopback virtual interface. The Windows kernel network stack (`tcpip.sys`) prevents unprivileged cross-process socket snooping in normal user mode, but privileged administrators or processes with raw socket/packet capture capabilities (`pcap`, ETW packet tracing) can inspect loopback traffic.
5. **Per-Request HMAC (Future Slice)**: Per-request HMAC proofs (`X-Pico-Bridge-Proof: v2`), when introduced in Slice 2, provide request authenticity, integrity, and anti-replay protection. They do **not** provide wire encryption or wire confidentiality.
6. **Threat Model Boundary**: The threat model specifically mitigates unauthenticated local listeners, port-squatting, port-takeover, uncontained helper processes, and accidental credential leakage to rogue processes. Full wire confidentiality against an administrative adversary on the same host requires OS-level virtualization or hardware isolation, which is outside the user-mode desktop threat model.

---

## 8. HMAC v2 Canonicalization & Single-Worker Invariant

### 8.1 Netstring / Length-Prefixed Canonical Input Format (UTF-8 Byte Lengths)
```text
pico-bridge-v2:{len(version)}:{version}:{len(timestamp)}:{timestamp}:{len(nonce)}:{nonce}:{len(method)}:{method}:{len(request_target)}:{request_target}:{len(body_digest)}:{body_digest}
```
- All field lengths (`{len(...)}`) are decimal ASCII representations of exact **UTF-8 byte lengths**.
- **`request_target`**: Exact raw URI path **including raw query string** as transmitted (e.g. `/api/conversations?limit=20&offset=0`).
  - **Duplicate Query Keys**: Preserved exactly in original wire order; never re-sorted or deduplicated.
  - **Percent-Encoding**: Preserves exact raw byte representation from wire; no decoding or double-encoding.
- **`body_digest`**: Hexadecimal SHA-256 hash of exact wire body bytes (matching Starlette/ASGI raw receive body).
- **Content-Type & Encoding**: Constrained to fixed per-operation values (`application/json`, empty). Unexpected encodings (e.g. gzip) are disallowed unless explicitly implemented.
- **Anti-Replay Cache**:
  - In-memory sliding window cache of observed nonces (30s TTL, 10,000 capacity, atomic check-and-insert, fail-closed on saturation).
- **Single-Worker Invariant**: PICO managed desktop strictly enforces `--workers 1` until distributed replay cache state is architected.
- **Secret Lifecycle**: New 256-bit launch secret generated on every Core restart; old secrets discarded.

---

## 9. Server-Sent Events (SSE) Byte-Level Backpressure & Cancellation

- **Explicit Queue Limits**:
  - Max event size: 16 KB per SSE chunk.
  - Max queued bytes: 64 KB total across all buffered chunks.
  - Max item count: 64 events.
- **Backpressure Propagation**:
  - If the WebView render loop lags, the bounded queue fills.
  - Once the 64 KB or 64-item limit is reached, the native TCP socket reader halts reading from Uvicorn, shrinking the Windows TCP receive window (`Window Size = 0`) and exerting kernel-level flow control back to Uvicorn.
  - **Slow-Consumer Timeout**: If the channel remains blocked for >10 seconds, the bridge aborts the stream.
- **Cancellation**:
  - When the user clicks "Stop", the frontend aborts the Tauri Channel.
  - The native reader drops the response body stream, closing the TCP socket.
  - Starlette detects `http.disconnect` in `pico/api/chat.py:186-189` (`await request.is_disconnected()`), raising `asyncio.CancelledError()`, updating outcome to `"cancelled"`, and releasing the scheduler slot within 100ms.

---

## 10. Complete API Route Matrix (95 Endpoints)

Full 95-route inventory:
- **33 Future Bridge-Eligible Routes**: General desktop companion operations.
- **7 Blocked Routes**: Internal daemons, autonomous ledgers, background telemetry, autonomous dispatch.
- **55 Deferred Routes**: Specialized features (Home Assistant, vision, voice, workflows, tool approval).
- **9 Initial Operations (Slices 1–3)**:
  1. `pico_get_system_status` (`GET /health`)
  2. `pico_pair_operator` (`POST /api/operator/pair`)
  3. `pico_get_operator_session` (`GET /api/operator/session`)
  4. `pico_logout_operator` (`POST /api/operator/logout`)
  5. `pico_list_conversations` (`GET /api/conversations`)
  6. `pico_create_conversation` (`POST /api/conversations`)
  7. `pico_get_messages` (`GET /api/conversations/{id}/messages`)
  8. `pico_chat_stream` (`POST /api/chat/stream`)
  9. `pico_list_tool_executions` (`GET /api/tools/executions`)

---

## 11. Milestone P11.2.2.2 Slice 1A Implementation & Verification Records

Milestone P11.2.2.2 Slice 1A has been implemented and empirically validated against the production FastAPI Core:

1. **Production Dependencies Added**:
   - `frontend/src-tauri/Cargo.toml` configured with minimal Hyper HTTP/1 stack:
     - `tokio = { version = "1.43", features = ["rt-multi-thread", "macros", "net", "io-util", "time"] }`
     - `hyper = { version = "1.5", features = ["client", "http1"] }`
     - `hyper-util = { version = "0.1", features = ["tokio"] }`
     - `http-body-util = "0.1"`
     - `http = "1.2"`
     - `bytes = "1.12"`
   - `reqwest` strictly avoided.

2. **Windows Physical Reverse Four-Tuple Resolution**:
   - Implemented `find_server_peer_pid(local: SocketAddr, peer: SocketAddr) -> Result<u32, String>`.
   - Strict loopback invariant validation: validates both local and peer IPs are IPv4 loopback (127.0.0.1) and non-zero ports.
   - Dynamic buffer resizing retry loop (handling `ERROR_INSUFFICIENT_BUFFER`) and 8-byte aligned buffer (`Vec<u64>`) with bounds and overflow checking.
   - Queries `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_ALL, AF_INET)` via `iphlpapi.dll`.
   - Matches reverse four-tuple where `dwState == MIB_TCP_STATE_ESTAB`:
     - `row.dwLocalAddr == peer.ip()`
     - `row.dwLocalPort == peer.port()`
     - `row.dwRemoteAddr == local.ip()`
     - `row.dwRemotePort == local.port()`
   - Strictly enforces uniqueness: returns `Ok(pid)` only when exactly 1 established row matches.

3. **Process Handle Retention, TOCTOU Resistance & Job Object Enforcement**:
   - `SafeProcessHandle(pub HANDLE)` encapsulates Win32 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, server_pid)`.
   - **TOCTOU Resistance**: While holding `SafeProcessHandle` open, `VerifiedConnection::connect` re-validates the exact reverse four-tuple from the Windows TCP table. Because the handle is open, the Windows kernel cannot free or recycle the Process Object, proving that the established connection belongs to the exact process entity pinned by the handle.
   - Verifies process liveness via `GetExitCodeProcess` and `IsProcessInJob` against the supervisor's `JobObject`.
   - Records process creation timestamp via `GetProcessTimes` to detect process replacement.

4. **Same-Socket Hyper HTTP/1 & Dedicated 32-Byte CNG Challenge Handshake**:
   - `VerifiedConnection` passes the exact verified `tokio::net::TcpStream` via `hyper_util::rt::TokioIo` directly into `hyper::client::conn::http1::handshake`.
   - Dedicated CNG challenge nonce generator (`generate_challenge_nonce()`) generates 32 bytes (256 bits) of entropy, formatted as 64 lowercase hex characters, keeping the launch secret and challenge nonces conceptually separate.
   - Executes genuine `POST /api/bridge/handshake` challenge over this exact socket:
     - Request body: `{"nonce":"<nonce>"}`.
     - Expected HMAC: `compute_handshake_hmac(secret, &nonce)` matching canonical `HMAC-SHA256(secret, "pico-bridge-v1:" + nonce)`.
     - Validates HTTP status 200 OK and compares HMAC in constant time via `constant_time_compare`.
   - **Credential-Release Gate**: Strictly zero operator cookies, zero CSRF tokens, zero pairing codes, and zero application payloads are transmitted before Core challenge verification succeeds.

5. **Connection State Machine, Driver Lifecycle & Fail-Closed Semantics**:
   - Explicit state transitions: `Disconnected -> Connecting -> VerifyingPeer -> VerifyingCore -> Ready`.
   - Retains background connection driver `JoinHandle`; `is_ready()` continuously verifies that the driver task is running, the socket is open, and the server process is alive.
   - Dropping `VerifiedConnection` aborts the background driver handle, releasing all socket and OS resources.
   - If server process terminates or socket returns I/O error, connection immediately transitions to `Disconnected` and fails closed.
   - No automatic reconnection: subsequent operations require explicit re-verification.

6. **Empirical Adversarial Verification Matrix (Scenarios A–G & Driver Lifecycle)**:
   - **Scenario A (Genuine Core)**: Verified connection against live managed FastAPI Core on port 8994; diagnostic `GET /health` request succeeded with status 200.
   - **Scenario B (Wrong Secret)**: Core on port 8995 rejected connection with wrong launch secret; failed closed with HMAC mismatch.
   - **Scenario C (Rogue External Listener)**: Standalone listener on ephemeral port outside Job Object rejected before any HTTP dispatch.
   - **Scenario D (Rogue In-Job Process)**: Listener in Job Object with bad HMAC signature failed closed at handshake phase.
   - **Scenario E (Backend Termination)**: Killing backend while connection is active causes subsequent request to fail closed immediately with transition to `Disconnected`.
   - **Scenario F (Port Takeover)**: Killing backend and binding rogue listener on same port is rejected; no silent reconnect.
   - **Scenario G (New Connection Re-verification)**: Consecutive connections independently execute full reverse four-tuple, Job Object check, and fresh HMAC challenge.
   - **Driver Lifecycle**: Verified that dropping `VerifiedConnection` aborts the background driver task and invalidates readiness.

---

## 12. Milestone P11.2.2.2 Slice 1B Implementation & Verification Records

Milestone P11.2.2.2 Slice 1B has been implemented, tested, and empirically validated against the production FastAPI Core:

1. **Rust In-Memory `OperatorSessionStore`**:
   - Implemented volatile, process-memory-only session container (`Arc<RwLock<Option<StoredSession>>>`).
   - Stores `session_cookie`, `csrf_token`, `expires_at_epoch`, `core_pid`, and `core_creation_time`.
   - Never written to disk, SQLite, persistent files, environment variables, or logs.
   - Automatically bound to the active Core PID and OS process creation timestamp; mismatched queries return false / None.
   - Immediate invalidation on Core shutdown, restart, PID change, or application exit.

2. **Per-Request HMAC Canonicalization & Anti-Replay (`X-Pico-Bridge-Proof: v2`)**:
   - Implemented length-prefixed canonical input format in Rust (`build_canonical_proof_v2_string`) and Python (`pico.core.bridge`):
     `pico-bridge-v2:{len(version)}:{version}:{len(timestamp)}:{timestamp}:{len(nonce)}:{nonce}:{len(method)}:{method}:{len(request_target)}:{request_target}:{len(body_digest)}:{body_digest}`
   - All lengths are decimal ASCII UTF-8 byte lengths.
   - Cross-language test vectors validated identically across Rust and Python test suites.
   - Implemented Starlette/FastAPI `BridgeAuthMiddleware` in `pico.api.bridge`:
     - Enforces loopback caller invariant.
     - Verifies timestamp freshness (±30s clock skew tolerance).
     - Atomic sliding-window `AntiReplayCache` with operator-approved 60.0s TTL (= 2 × 30s clock skew tolerance, 10,000 capacity, fail-closed on saturation).
     - Digests exact raw wire body bytes with SHA-256.
     - Constant-time HMAC verification (`secrets.compare_digest`).
   - **Timestamp Freshness & Replay-Cache Security Proof**:
     Let $T_{\text{recv}}$ be server wall-clock time when a request with client timestamp $T_{\text{req}}$ is accepted and cached. Under the freshness policy $|T_{\text{recv}} - T_{\text{req}}| \le 30.0\text{s}$, the maximum future timestamp allowed is $T_{\text{req}} = T_{\text{recv}} + 30.0\text{s}$. The nonce is retained until $T_{\text{expiry}} = T_{\text{recv}} + 60.0\text{s}$. For any replay attempt at $T > T_{\text{expiry}}$:
     $$T - T_{\text{req}} \ge (T_{\text{recv}} + 60.0) - (T_{\text{recv}} + 30.0) = 30.0\text{s}$$
     Thus $T - T_{\text{req}} > 30.0\text{s}$ for all $T > T_{\text{expiry}}$, ensuring the timestamp is strictly expired once the nonce leaves the cache. Within the cache window ($T \le T_{\text{expiry}}$), the nonce is tracked and replay is rejected.
   - **Effective UTC Clock & Monotonic Replay Protection Invariant**:
     To eliminate the risk of host wall-clock backward adjustment (e.g., NTP step, manual clock rollback) re-validating an expired timestamp after its nonce has been pruned, PICO implements an in-memory `EffectiveUtcClock` and atomic `BridgeReplayProtector`:
     - **Effective UTC Clock**: Operates in pure integer nanoseconds. The effective UTC time is calculated as $\max(T_{\text{wall}}, T_{\text{effective\_prev}} + \Delta T_{\text{mono}})$, guaranteeing that the effective clock never regresses even if the host wall clock jumps backwards arbitrarily.
     - **Clock Regression Fail-Closed**: If the monotonic clock source regresses ($\Delta T_{\text{mono}} < 0$), `ClockUnavailableError` is raised immediately and the server fails closed without clamping or updating state.
     - **Windows Platform Clock Source**: In Windows 11, Python's `time.monotonic_ns()` is backed by `QueryPerformanceCounter` (100ns resolution), providing non-decreasing monotonic timestamps across thread and process scheduling.
     - **Atomic Replay & Pre-HMAC Nonce Consumption**: The `BridgeAuthMiddleware` strictly validates and verifies the HMAC-SHA256 signature **before** invoking `BridgeReplayProtector.validate_and_consume(nonce, req_ts_ns)`. Invalid HMAC requests never consume nonces or pollute the cache. Nonce validation, timestamp skew verification (±30s), pruning of expired nonces strictly older than 60s, and insertion are executed atomically under a single re-entrant lock.
     - **Core Restart & Secret Rotation**: Upon Core restart or recreation, a new cryptographic launch secret is minted by the supervisor via Windows CNG and passed over the private pipe handle. Any proofs signed with an older secret fail closed with HTTP 401 signature verification errors.
   - **Single-Worker Enforcement Invariant**:
     Because `AntiReplayCache` is an in-memory process-local cache, multiple Uvicorn workers would partition the cache, creating an inter-worker replay vulnerability. `LaunchConfig::default_local` and `ManagedChild::spawn` strictly enforce `--workers 1`, rejecting configurations with `--workers != 1`, missing `--workers`, or `--reload`.

3. **Approved Native Operations, Tauri IPC Commands & Explicit Desktop Profiles**:
   - Strictly exposed only two operations over the verified same-socket loopback transport:
     - `pico_get_system_status`: calls `GET /health` with v2 proof; returns sanitized status JSON.
     - `pico_pair_operator`: accepts 6-digit numeric code; dispatches `POST /api/operator/pair` with v2 proof; extracts `pico_operator_session` cookie and `csrf_token` into `OperatorSessionStore`; returns sanitized status JSON `{"status":"paired","approver":"..."}` without releasing credentials.
   - Zero raw cookies, zero CSRF tokens, and zero launch secrets exposed across the Tauri IPC boundary or to client-side bundles.
   - Native `SecretBuffer` implements volatile memory zeroization (`std::ptr::write_volatile(b, 0)`) on drop.
   - Starlette raw ASGI scope (`raw_path` and `query_string`) preserves duplicate and percent-encoded query parameters.
   - Frontend `frontend/src/api/client.ts` implements explicit desktop runtime profiles:
     - `browser-development`: Configured via `pnpm run dev` (`vite --mode browser-development`). Uses standard browser loopback `fetch` against FastAPI Core. Fails closed with `ApiError` if run inside a Tauri host environment (profile conflict).
     - `managed-desktop`: Configured via `pnpm run dev:tauri` (`vite --mode managed-desktop --host localhost --port 1420 --strictPort`). Strictly routes `pairOperator` and `getHealth` over native Tauri IPC commands (`pico_pair_operator`, `pico_get_system_status`). Fails closed without fallback if `isTauri()` is false or if IPC fails.
     - `packaged-production`: Configured via `pnpm run build` (`tsc -b && vite build --mode packaged-production`). Strictly enforces Tauri native IPC; fails closed without browser fallback.
     - Missing, unapproved, or unknown profiles fail closed immediately at runtime. Verified with Vitest in `tauri_bridge_integration.test.ts`.

4. **Empirical Real-Core Verification**:
   - `test_slice_1b_native_get_system_status_real_core`: Launched real supervised FastAPI Core; executed native status query over same-socket transport with v2 proof; verified HTTP 200 OK and healthy response.
   - `test_slice_1b_native_pair_operator_real_core`: Validated format rejection (client-side), invalid code rejection (backend), and genuine pairing with minted challenge code; verified session storage, credential confidentiality, and invalidation lifecycle.

5. **Remaining Boundaries for Future Slices**:
   - No conversation operations.
   - No tool execution operations.
   - No policy approval operations.
   - No chat streaming or SSE backpressure channels.
   - No WebSockets.

---

## 13. Preservations and Invariants Confirmation

- **ADR 0001 Integrity**: [`docs/decisions/0001-windows-desktop-companion-architecture.md`](0001-windows-desktop-companion-architecture.md) preserved byte-for-byte (`git diff HEAD` returns 0 diff).
- **ADR 0002 Integrity**: [`docs/decisions/0002-p11-2-2-secure-loopback-bridge-qualification.md`](0002-p11-2-2-secure-loopback-bridge-qualification.md) preserved intact.
- **User Local Tooling**: Untracked configurations (`.agents/`, `.gemini/`, `.gsd/`, `scripts/`, adapters, scratch prototype) preserved without modification.
- **Production Source Isolation**: No unnecessary dependencies added; zero git commits, pushes, or PRs executed.
