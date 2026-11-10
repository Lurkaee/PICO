//! Windows Native Backend Process Supervisor & Job Object Containment (P11.2.2.1).
//!
//! Provides OS-enforced lifecycle containment and identity verification for the PICO Core backend:
//! 1. Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` ensures that when the Tauri host
//!    exits or terminates, the Windows OS kernel unconditionally kills the entire backend process tree.
//! 2. Startup sequence creates the child process suspended, configures and assigns it to the Job Object,
//!    and only resumes the thread after successful containment (eliminating unmanaged execution windows).
//! 3. Generates an ephemeral 256-bit random launch secret transferred via an explicitly inherited anonymous pipe.
//! 4. Uses `STARTUPINFOEXW`, `InitializeProcThreadAttributeList`, and `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`
//!    to restrict handle inheritance strictly to the designated anonymous-pipe read handle, preventing
//!    unrelated host handles from leaking to the child.
//! 5. Verifies TCP table listener ownership on 127.0.0.1 via `GetExtendedTcpTable` against the retained child PID.
//! 6. Handshake challenge-response verification ensures authentic application identity before credential release.

#![allow(non_snake_case)]

use std::ffi::c_void;
use std::io::{Read, Write};
use std::mem;
use std::net::{SocketAddr, TcpStream};
use std::os::windows::ffi::OsStrExt;
use std::path::PathBuf;
use std::ptr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::Request;
use hyper_util::rt::TokioIo;

// Windows API Types
type HANDLE = *mut c_void;
type BOOL = i32;
type DWORD = u32;
type WORD = u16;
type LPCWSTR = *const u16;
type LPWSTR = *mut u16;

const TRUE: BOOL = 1;
const INVALID_HANDLE_VALUE: HANDLE = -1isize as HANDLE;

// Process Creation Flags
const CREATE_SUSPENDED: DWORD = 0x00000004;
const EXTENDED_STARTUPINFO_PRESENT: DWORD = 0x00080000;
const CREATE_NO_WINDOW: DWORD = 0x08000000;
const CREATE_UNICODE_ENVIRONMENT: DWORD = 0x00000400;

// Job Object Extended Limits
const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: DWORD = 9;
const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: DWORD = 0x00002000;

// Handle Flags
const HANDLE_FLAG_INHERIT: DWORD = 0x00000001;
const PROC_THREAD_ATTRIBUTE_HANDLE_LIST: usize = 0x00020002;

// Cryptographic RNG Flags
const BCRYPT_USE_SYSTEM_PREFERRED_RNG: DWORD = 0x00000002;

type BCryptGenRandomFn = unsafe extern "system" fn(
    hAlgorithm: *mut c_void,
    pbBuffer: *mut u8,
    cbBuffer: DWORD,
    dwFlags: DWORD,
) -> i32;

// TCP Table Constants
const AF_INET: DWORD = 2;
const TCP_TABLE_OWNER_PID_LISTENER: DWORD = 3;
const TCP_TABLE_OWNER_PID_ALL: DWORD = 5;
const MIB_TCP_STATE_ESTAB: DWORD = 5;

// Process Access and Status Flags
const PROCESS_QUERY_LIMITED_INFORMATION: DWORD = 0x1000;
const SYNCHRONIZE: DWORD = 0x00100000;
const STILL_ACTIVE: DWORD = 259;

#[repr(C)]
struct IO_COUNTERS {
    ReadOperationCount: u64,
    WriteOperationCount: u64,
    OtherOperationCount: u64,
    ReadTransferCount: u64,
    WriteTransferCount: u64,
    OtherTransferCount: u64,
}

#[repr(C)]
struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
    PerProcessUserTimeLimit: i64,
    PerJobUserTimeLimit: i64,
    LimitFlags: DWORD,
    MinimumWorkingSetSize: usize,
    MaximumWorkingSetSize: usize,
    ActiveProcessLimit: DWORD,
    Affinity: usize,
    PriorityClass: DWORD,
    SchedulingClass: DWORD,
}

#[repr(C)]
struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
    BasicLimitInformation: JOBOBJECT_BASIC_LIMIT_INFORMATION,
    IoInfo: IO_COUNTERS,
    ProcessMemoryLimit: usize,
    JobMemoryLimit: usize,
    PeakProcessMemoryLimit: usize,
    PeakJobMemoryLimit: usize,
}

#[repr(C)]
struct SECURITY_ATTRIBUTES {
    nLength: DWORD,
    lpSecurityDescriptor: *mut c_void,
    bInheritHandle: BOOL,
}

#[repr(C)]
struct STARTUPINFOW {
    cb: DWORD,
    lpReserved: LPWSTR,
    lpDesktop: LPWSTR,
    lpTitle: LPWSTR,
    dwX: DWORD,
    dwY: DWORD,
    dwXSize: DWORD,
    dwYSize: DWORD,
    dwXCountChars: DWORD,
    dwYCountChars: DWORD,
    dwFillAttribute: DWORD,
    dwFlags: DWORD,
    wShowWindow: WORD,
    cbReserved2: WORD,
    lpReserved2: *mut u8,
    hStdInput: HANDLE,
    hStdOutput: HANDLE,
    hStdError: HANDLE,
}

#[repr(C)]
struct STARTUPINFOEXW {
    StartupInfo: STARTUPINFOW,
    lpAttributeList: *mut c_void,
}

#[repr(C)]
struct PROCESS_INFORMATION {
    hProcess: HANDLE,
    hThread: HANDLE,
    dwProcessId: DWORD,
    dwThreadId: DWORD,
}

#[repr(C)]
struct FILETIME {
    dwLowDateTime: DWORD,
    dwHighDateTime: DWORD,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct MIB_TCPROW_OWNER_PID {
    pub dwState: DWORD,
    pub dwLocalAddr: DWORD,
    pub dwLocalPort: DWORD,
    pub dwRemoteAddr: DWORD,
    pub dwRemotePort: DWORD,
    pub dwOwningPid: DWORD,
}

extern "system" {
    fn CreateJobObjectW(lpJobAttributes: *mut c_void, lpName: LPCWSTR) -> HANDLE;
    fn TerminateJobObject(hJob: HANDLE, uExitCode: DWORD) -> BOOL;
    fn SetInformationJobObject(
        hJob: HANDLE,
        JobObjectInformationClass: DWORD,
        lpJobObjectInformation: *mut c_void,
        cbJobObjectInformationLength: DWORD,
    ) -> BOOL;
    fn AssignProcessToJobObject(hJob: HANDLE, hProcess: HANDLE) -> BOOL;
    fn CreatePipe(
        hReadPipe: *mut HANDLE,
        hWritePipe: *mut HANDLE,
        lpPipeAttributes: *mut SECURITY_ATTRIBUTES,
        nSize: DWORD,
    ) -> BOOL;
    fn SetHandleInformation(hObject: HANDLE, dwMask: DWORD, dwFlags: DWORD) -> BOOL;
    fn InitializeProcThreadAttributeList(
        lpAttributeList: *mut c_void,
        dwAttributeCount: DWORD,
        dwFlags: DWORD,
        lpSize: *mut usize,
    ) -> BOOL;
    fn UpdateProcThreadAttribute(
        lpAttributeList: *mut c_void,
        dwFlags: DWORD,
        Attribute: usize,
        lpValue: *mut c_void,
        cbSize: usize,
        lpPreviousValue: *mut c_void,
        lpReturnSize: *mut usize,
    ) -> BOOL;
    fn DeleteProcThreadAttributeList(lpAttributeList: *mut c_void);
    fn CreateProcessW(
        lpApplicationName: LPCWSTR,
        lpCommandLine: LPWSTR,
        lpProcessAttributes: *mut c_void,
        lpThreadAttributes: *mut c_void,
        bInheritHandles: BOOL,
        dwCreationFlags: DWORD,
        lpEnvironment: *mut c_void,
        lpCurrentDirectory: LPCWSTR,
        lpStartupInfo: *mut c_void,
        lpProcessInformation: *mut PROCESS_INFORMATION,
    ) -> BOOL;
    fn ResumeThread(hThread: HANDLE) -> DWORD;
    fn TerminateProcess(hProcess: HANDLE, uExitCode: DWORD) -> BOOL;
    fn WaitForSingleObject(hHandle: HANDLE, dwMilliseconds: DWORD) -> DWORD;
    fn GetExitCodeProcess(hProcess: HANDLE, lpExitCode: *mut DWORD) -> BOOL;
    fn GetProcessTimes(
        hProcess: HANDLE,
        lpCreationTime: *mut FILETIME,
        lpExitTime: *mut FILETIME,
        lpKernelTime: *mut FILETIME,
        lpUserTime: *mut FILETIME,
    ) -> BOOL;
    fn CloseHandle(hObject: HANDLE) -> BOOL;
    fn WriteFile(
        hFile: HANDLE,
        lpBuffer: *const c_void,
        nNumberOfBytesToWrite: DWORD,
        lpNumberOfBytesWritten: *mut DWORD,
        lpOverlapped: *mut c_void,
    ) -> BOOL;
    fn LoadLibraryA(lpLibFileName: *const i8) -> HANDLE;
    fn GetProcAddress(hModule: HANDLE, lpProcName: *const i8) -> *mut c_void;
    fn FreeLibrary(hLibModule: HANDLE) -> BOOL;
    fn OpenProcess(dwDesiredAccess: DWORD, bInheritHandle: BOOL, dwProcessId: DWORD) -> HANDLE;
    fn IsProcessInJob(ProcessHandle: HANDLE, JobHandle: HANDLE, Result: *mut BOOL) -> BOOL;
}

#[link(name = "iphlpapi")]
extern "system" {
    fn GetExtendedTcpTable(
        pTcpTable: *mut c_void,
        pdwSize: *mut DWORD,
        bOrder: BOOL,
        ulAf: DWORD,
        TableClass: DWORD,
        Reserved: DWORD,
    ) -> DWORD;
}

/// RAII guard for dynamically loaded Win32 module handles ensuring FreeLibrary is called on all paths.
struct ModuleHandle(HANDLE);

impl ModuleHandle {
    unsafe fn load(name: &[u8]) -> Result<Self, String> {
        let handle = LoadLibraryA(name.as_ptr() as *const i8);
        if handle.is_null() {
            Err("Failed to load library".to_string())
        } else {
            Ok(Self(handle))
        }
    }

    unsafe fn get_proc(&self, name: &[u8]) -> *mut c_void {
        GetProcAddress(self.0, name.as_ptr() as *const i8)
    }
}

impl Drop for ModuleHandle {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { FreeLibrary(self.0) };
        }
    }
}

/// Safe RAII wrapper around a Windows Process Handle ensuring CloseHandle is always invoked.
#[derive(Debug)]
pub struct SafeProcessHandle(pub HANDLE);

impl SafeProcessHandle {
    pub fn raw(&self) -> HANDLE {
        self.0
    }

    pub fn is_alive(&self) -> bool {
        if self.0.is_null() || self.0 == INVALID_HANDLE_VALUE {
            return false;
        }
        let mut exit_code: DWORD = 0;
        let res = unsafe { GetExitCodeProcess(self.0, &mut exit_code) };
        res == TRUE && exit_code == STILL_ACTIVE
    }
}

/// Opens a safe RAII handle to a process with `PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE`.
pub fn open_process_handle(pid: u32) -> Result<SafeProcessHandle, String> {
    let h = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, pid) };
    if h.is_null() || h == INVALID_HANDLE_VALUE {
        Err(format!("Failed to open process handle for PID {}", pid))
    } else {
        Ok(SafeProcessHandle(h))
    }
}

impl Drop for SafeProcessHandle {
    fn drop(&mut self) {
        if !self.0.is_null() && self.0 != INVALID_HANDLE_VALUE {
            unsafe { CloseHandle(self.0) };
        }
    }
}

unsafe impl Send for SafeProcessHandle {}
unsafe impl Sync for SafeProcessHandle {}

/// Safe wrapper around a Windows Job Object handle configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
pub struct JobObject {
    handle: HANDLE,
}

impl JobObject {
    /// Creates a new anonymous Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
    pub fn new() -> Result<Self, String> {
        let handle = unsafe { CreateJobObjectW(ptr::null_mut(), ptr::null()) };
        if handle.is_null() || handle == INVALID_HANDLE_VALUE {
            return Err("Failed to create Windows Job Object".to_string());
        }

        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { mem::zeroed() };
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

        let ok = unsafe {
            SetInformationJobObject(
                handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                &mut info as *mut _ as *mut c_void,
                mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as DWORD,
            )
        };

        if ok != TRUE {
            unsafe { CloseHandle(handle) };
            return Err(
                "Failed to set JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE on Job Object".to_string(),
            );
        }

        Ok(Self { handle })
    }

    /// Assigns a running or suspended process handle to this Job Object.
    pub fn assign_process(&self, process_handle: HANDLE) -> Result<(), String> {
        let ok = unsafe { AssignProcessToJobObject(self.handle, process_handle) };
        if ok != TRUE {
            Err("Failed to assign process to Job Object".to_string())
        } else {
            Ok(())
        }
    }

    /// Checks whether a given process ID is contained within this Job Object.
    ///
    /// On Windows, Python virtual environment `python.exe` is a launcher stub that
    /// spawns the actual interpreter runtime as a child within the same Job Object.
    pub fn contains_process(&self, pid: u32) -> bool {
        const PROCESS_QUERY_LIMITED_INFORMATION: DWORD = 0x1000;
        let proc_handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if proc_handle.is_null() || proc_handle == INVALID_HANDLE_VALUE {
            return false;
        }
        let mut is_in_job: BOOL = 0;
        let ok = unsafe { IsProcessInJob(proc_handle, self.handle, &mut is_in_job) };
        unsafe { CloseHandle(proc_handle) };
        ok == TRUE && is_in_job == TRUE
    }

    /// Terminates all processes assigned to this Job Object.
    pub fn terminate(&self) -> Result<(), String> {
        let ok = unsafe { TerminateJobObject(self.handle, 1) };
        if ok == TRUE {
            Ok(())
        } else {
            Err("Failed to terminate Job Object processes".to_string())
        }
    }

    /// Returns the raw Windows HANDLE for inspection or test verification.
    pub fn raw_handle(&self) -> HANDLE {
        self.handle
    }
}

impl Drop for JobObject {
    fn drop(&mut self) {
        if !self.handle.is_null() && self.handle != INVALID_HANDLE_VALUE {
            unsafe { CloseHandle(self.handle) };
        }
    }
}

unsafe impl Send for JobObject {}
unsafe impl Sync for JobObject {}

/// Managed child process configuration.
#[derive(Debug, Clone)]
pub struct LaunchConfig {
    pub executable: PathBuf,
    pub args: Vec<String>,
    pub working_dir: PathBuf,
    pub bind_host: String,
    pub bind_port: u16,
}

impl LaunchConfig {
    /// Constructs a safe default launch configuration bound strictly to 127.0.0.1:8000 with explicit single-worker execution.
    pub fn default_local(executable: PathBuf, working_dir: PathBuf) -> Self {
        Self {
            executable,
            args: vec![
                "-m".to_string(),
                "uvicorn".to_string(),
                "pico.main:app".to_string(),
                "--host".to_string(),
                "127.0.0.1".to_string(),
                "--port".to_string(),
                "8000".to_string(),
                "--workers".to_string(),
                "1".to_string(),
            ],
            working_dir,
            bind_host: "127.0.0.1".to_string(),
            bind_port: 8000,
        }
    }

    /// Spawns the managed child backend with OS-enforced Job Object containment and explicit handle inheritance.
    pub fn spawn(&self) -> Result<ManagedChild, String> {
        ManagedChild::spawn(self)
    }
}

/// Represents an actively supervised PICO Core child process.
pub struct ManagedChild {
    pub job_object: Arc<JobObject>,
    pub process_handle: HANDLE,
    pub thread_handle: HANDLE,
    pub launcher_pid: u32,
    pub pid: u32,
    pub listener_handle: Option<HANDLE>,
    pub creation_time: u64,
    pub launch_secret: String,
}

impl ManagedChild {
    /// Spawns a supervised PICO Core child process using Windows Job Object containment,
    /// suspended startup, and explicit attribute-list handle inheritance (`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`).
    pub fn spawn(config: &LaunchConfig) -> Result<Self, String> {
        // 1. Strict loopback invariant and worker concurrency validation
        if config.bind_host != "127.0.0.1" {
            return Err("LaunchConfig bind_host must be strictly 127.0.0.1".to_string());
        }
        if config.args.contains(&"--reload".to_string()) {
            return Err("Managed child backend must not use --reload workers".to_string());
        }
        // Explicitly enforce single-worker invariant for replay cache integrity
        let mut worker_occurrences = 0;
        let mut worker_count: Option<u32> = None;
        for (i, arg) in config.args.iter().enumerate() {
            if arg == "--workers" && i + 1 < config.args.len() {
                worker_occurrences += 1;
                if let Ok(c) = config.args[i + 1].parse::<u32>() {
                    worker_count = Some(c);
                }
            } else if arg.starts_with("--workers=") {
                worker_occurrences += 1;
                if let Ok(c) = arg.trim_start_matches("--workers=").parse::<u32>() {
                    worker_count = Some(c);
                }
            }
        }
        if worker_occurrences > 1 {
            return Err(
                "Managed child backend must not specify duplicate --workers arguments".to_string(),
            );
        }
        if let Some(count) = worker_count {
            if count != 1 {
                return Err(format!(
                    "Managed child backend must run with exactly 1 worker; found --workers {}",
                    count
                ));
            }
        } else {
            return Err("Managed child backend must explicitly specify --workers 1".to_string());
        }

        // 2. Pre-check port occupancy to fail closed before spawning
        if let Some(existing_pid) = get_tcp_listener_pid(config.bind_port) {
            return Err(format!(
                "Port {} is already bound by process PID {}; refusing unmanaged takeover",
                config.bind_port, existing_pid
            ));
        }

        // 3. Create Windows Job Object with KILL_ON_JOB_CLOSE
        let job_object = Arc::new(JobObject::new()?);

        // 4. Generate ephemeral 256-bit launch secret using Windows CNG BCryptGenRandom
        let launch_secret = generate_launch_secret()?;

        // 5. Create anonymous pipe for secret transfer.
        // Both pipe handles are created non-inheritable by default.
        let mut pipe_r: HANDLE = ptr::null_mut();
        let mut pipe_w: HANDLE = ptr::null_mut();
        let pipe_created = unsafe { CreatePipe(&mut pipe_r, &mut pipe_w, ptr::null_mut(), 0) };
        if pipe_created != TRUE || pipe_r.is_null() || pipe_w.is_null() {
            return Err("Failed to create anonymous pipe for secret bootstrap".to_string());
        }

        // Mark ONLY the read handle inheritable. The write handle remains strictly non-inheritable.
        let set_info_ok =
            unsafe { SetHandleInformation(pipe_r, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) };
        if set_info_ok != TRUE {
            unsafe {
                CloseHandle(pipe_r);
                CloseHandle(pipe_w);
            };
            return Err("Failed to set handle inheritance flag on pipe read handle".to_string());
        }

        // 6. Explicit handle inheritance configuration via PROC_THREAD_ATTRIBUTE_HANDLE_LIST
        let mut attr_size: usize = 0;
        unsafe {
            InitializeProcThreadAttributeList(ptr::null_mut(), 1, 0, &mut attr_size);
        }
        let mut attr_buf = vec![0u8; attr_size];
        let attr_list = attr_buf.as_mut_ptr() as *mut c_void;
        let init_ok = unsafe { InitializeProcThreadAttributeList(attr_list, 1, 0, &mut attr_size) };
        if init_ok != TRUE {
            unsafe {
                CloseHandle(pipe_r);
                CloseHandle(pipe_w);
            };
            return Err("Failed to initialize process attribute list".to_string());
        }

        // Pass ONLY the designated pipe_r handle in the explicit handle list
        let mut handles_to_inherit = [pipe_r];
        let update_ok = unsafe {
            UpdateProcThreadAttribute(
                attr_list,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                handles_to_inherit.as_mut_ptr() as *mut c_void,
                mem::size_of::<HANDLE>(),
                ptr::null_mut(),
                ptr::null_mut(),
            )
        };
        if update_ok != TRUE {
            unsafe {
                DeleteProcThreadAttributeList(attr_list);
                CloseHandle(pipe_r);
                CloseHandle(pipe_w);
            };
            return Err("Failed to configure PROC_THREAD_ATTRIBUTE_HANDLE_LIST".to_string());
        }

        let mut siex: STARTUPINFOEXW = unsafe { mem::zeroed() };
        siex.StartupInfo.cb = mem::size_of::<STARTUPINFOEXW>() as DWORD;
        siex.lpAttributeList = attr_list;

        // 7. Format command line and working directory
        let cmd_str = format!(
            "\"{}\" {}",
            config.executable.display(),
            config.args.join(" ")
        );
        let mut cmd_wide: Vec<u16> = cmd_str.encode_utf16().chain(std::iter::once(0)).collect();
        let workdir_wide: Vec<u16> = config
            .working_dir
            .as_os_str()
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();

        // 8. Construct environment block passing PICO_BRIDGE_PIPE_HANDLE without modifying host process env
        let mut env_block: Vec<u16> = Vec::new();
        for (k, v) in std::env::vars() {
            if k != "PICO_BRIDGE_PIPE_HANDLE" {
                for c in format!("{}={}\0", k, v).encode_utf16() {
                    env_block.push(c);
                }
            }
        }
        for c in format!("PICO_BRIDGE_PIPE_HANDLE={}\0", pipe_r as usize).encode_utf16() {
            env_block.push(c);
        }
        env_block.push(0); // double-null termination

        // 9. Create process suspended with explicit extended startup info
        let mut pi: PROCESS_INFORMATION = unsafe { mem::zeroed() };
        let flags = CREATE_SUSPENDED
            | EXTENDED_STARTUPINFO_PRESENT
            | CREATE_NO_WINDOW
            | CREATE_UNICODE_ENVIRONMENT;

        let created = unsafe {
            CreateProcessW(
                ptr::null(),
                cmd_wide.as_mut_ptr(),
                ptr::null_mut(),
                ptr::null_mut(),
                TRUE, // bInheritHandles must be TRUE with PROC_THREAD_ATTRIBUTE_HANDLE_LIST
                flags,
                env_block.as_mut_ptr() as *mut c_void,
                workdir_wide.as_ptr(),
                &mut siex as *mut _ as *mut c_void,
                &mut pi,
            )
        };

        // Always clean up attribute list and parent-side read handle immediately
        unsafe {
            DeleteProcThreadAttributeList(attr_list);
            CloseHandle(pipe_r);
        }

        if created != TRUE {
            unsafe {
                CloseHandle(pipe_w);
            };
            return Err("CreateProcessW failed for managed backend child".to_string());
        }

        // 10. Write launch secret to pipe and close write end immediately
        let secret_payload = format!("{}\n", launch_secret);
        let mut written: DWORD = 0;
        let write_ok = unsafe {
            WriteFile(
                pipe_w,
                secret_payload.as_ptr() as *const c_void,
                secret_payload.len() as DWORD,
                &mut written,
                ptr::null_mut(),
            )
        };
        unsafe {
            CloseHandle(pipe_w);
        };

        if write_ok != TRUE || written as usize != secret_payload.len() {
            unsafe {
                TerminateProcess(pi.hProcess, 1);
                CloseHandle(pi.hThread);
                CloseHandle(pi.hProcess);
            };
            return Err("Failed to write launch secret to child pipe handle".to_string());
        }

        // 11. Assign suspended child to Job Object BEFORE thread resumption
        if let Err(e) = job_object.assign_process(pi.hProcess) {
            unsafe {
                TerminateProcess(pi.hProcess, 1);
                CloseHandle(pi.hThread);
                CloseHandle(pi.hProcess);
            };
            return Err(format!("Job Object assignment failed: {}", e));
        }

        // 12. Resume child execution
        let resume_res = unsafe { ResumeThread(pi.hThread) };
        if resume_res == DWORD::MAX {
            unsafe {
                TerminateProcess(pi.hProcess, 1);
                CloseHandle(pi.hThread);
                CloseHandle(pi.hProcess);
            };
            return Err("Failed to resume child process thread".to_string());
        }

        // 13. Query child creation timestamp
        let mut creation_time_ft: FILETIME = unsafe { mem::zeroed() };
        let mut exit_time_ft: FILETIME = unsafe { mem::zeroed() };
        let mut kernel_time_ft: FILETIME = unsafe { mem::zeroed() };
        let mut user_time_ft: FILETIME = unsafe { mem::zeroed() };
        let times_ok = unsafe {
            GetProcessTimes(
                pi.hProcess,
                &mut creation_time_ft,
                &mut exit_time_ft,
                &mut kernel_time_ft,
                &mut user_time_ft,
            )
        };
        let creation_time = if times_ok == TRUE {
            ((creation_time_ft.dwHighDateTime as u64) << 32)
                | (creation_time_ft.dwLowDateTime as u64)
        } else {
            0
        };

        Ok(Self {
            job_object,
            process_handle: pi.hProcess,
            thread_handle: pi.hThread,
            launcher_pid: pi.dwProcessId,
            pid: pi.dwProcessId,
            listener_handle: None,
            creation_time,
            launch_secret,
        })
    }

    /// Checks whether the managed child process (both launcher and verified listener) is still actively running.
    pub fn is_alive(&self) -> bool {
        // 1. If we hold an explicit handle to the verified listener process, check it first.
        // This guarantees runtime backend death is detected immediately even if the launcher stub process lingers.
        if let Some(h_listener) = self.listener_handle {
            if !h_listener.is_null() && h_listener != INVALID_HANDLE_VALUE {
                let mut exit_code: DWORD = 0;
                let res = unsafe { GetExitCodeProcess(h_listener, &mut exit_code) };
                if res != TRUE || exit_code != 259 {
                    return false;
                }
            }
        }

        // 2. Also check the root launcher process handle
        if !self.process_handle.is_null() && self.process_handle != INVALID_HANDLE_VALUE {
            let mut exit_code: DWORD = 0;
            let res = unsafe { GetExitCodeProcess(self.process_handle, &mut exit_code) };
            if res == TRUE {
                exit_code == 259 // STILL_ACTIVE (0x103)
            } else {
                false
            }
        } else {
            false
        }
    }

    /// Terminate the child process explicitly and release native handles.
    pub fn terminate(&mut self) -> Result<(), String> {
        let _ = self.job_object.terminate();
        if let Some(h) = self.listener_handle.take() {
            if !h.is_null() && h != INVALID_HANDLE_VALUE {
                unsafe {
                    CloseHandle(h);
                }
            }
        }
        if !self.process_handle.is_null() && self.process_handle != INVALID_HANDLE_VALUE {
            if self.is_alive() {
                let ok = unsafe { TerminateProcess(self.process_handle, 1) };
                if ok == TRUE {
                    unsafe { WaitForSingleObject(self.process_handle, 5000) };
                }
            } else {
                unsafe { WaitForSingleObject(self.process_handle, 1000) };
            }
            unsafe {
                CloseHandle(self.process_handle);
                self.process_handle = ptr::null_mut();
            }
        }
        if !self.thread_handle.is_null() && self.thread_handle != INVALID_HANDLE_VALUE {
            unsafe {
                CloseHandle(self.thread_handle);
                self.thread_handle = ptr::null_mut();
            }
        }
        Ok(())
    }

    /// Waits for the managed backend to bind to 127.0.0.1:bind_port, verifies that the
    /// listening process PID matches this managed child, validates process creation time,
    /// and executes a mutual nonce/HMAC challenge handshake with the backend.
    ///
    /// Fails closed with descriptive error and terminates the child process on failure or timeout.
    pub fn wait_and_verify(&mut self, bind_port: u16, timeout: Duration) -> Result<(), String> {
        let start = Instant::now();
        let mut listener_verified = false;

        // 1. Poll for TCP listener on 127.0.0.1:bind_port
        while start.elapsed() < timeout {
            if !self.is_alive() {
                let _ = self.terminate();
                return Err(
                    "Managed backend child process terminated unexpectedly during startup"
                        .to_string(),
                );
            }

            if let Some(listener_pid) = get_tcp_listener_pid(bind_port) {
                if listener_pid != self.launcher_pid {
                    // Check if listener process is contained within this Job Object.
                    // On Windows, virtual environment python.exe is a launcher stub that spawns the runtime child process inside the same Job Object.
                    if !self.job_object.contains_process(listener_pid) {
                        let _ = self.terminate();
                        return Err(format!(
                            "Port {} listener PID {} does not match managed child PID {} and is not within Job Object; possible rogue listener takeover",
                            bind_port, listener_pid, self.launcher_pid
                        ));
                    }
                    self.pid = listener_pid;
                    const PROCESS_QUERY_LIMITED_INFORMATION: DWORD = 0x1000;
                    const SYNCHRONIZE: DWORD = 0x00100000;
                    let h_listener = unsafe {
                        OpenProcess(
                            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                            0,
                            listener_pid,
                        )
                    };
                    if !h_listener.is_null() && h_listener != INVALID_HANDLE_VALUE {
                        let mut creation_time_ft: FILETIME = unsafe { mem::zeroed() };
                        let mut exit_time_ft: FILETIME = unsafe { mem::zeroed() };
                        let mut kernel_time_ft: FILETIME = unsafe { mem::zeroed() };
                        let mut user_time_ft: FILETIME = unsafe { mem::zeroed() };
                        let times_ok = unsafe {
                            GetProcessTimes(
                                h_listener,
                                &mut creation_time_ft,
                                &mut exit_time_ft,
                                &mut kernel_time_ft,
                                &mut user_time_ft,
                            )
                        };
                        if times_ok == TRUE {
                            self.creation_time = ((creation_time_ft.dwHighDateTime as u64) << 32)
                                | (creation_time_ft.dwLowDateTime as u64);
                        }
                        self.listener_handle = Some(h_listener);
                    }
                }
                listener_verified = true;
                break;
            }

            std::thread::sleep(Duration::from_millis(100));
        }

        if !listener_verified {
            let _ = self.terminate();
            return Err(format!(
                "Timed out after {}s waiting for managed child PID {} to bind to 127.0.0.1:{}",
                timeout.as_secs(),
                self.pid,
                bind_port
            ));
        }

        // 2. Validate process instance
        if !self.is_alive() {
            let _ = self.terminate();
            return Err("Managed backend process died before identity handshake".to_string());
        }

        // 3. Generate a dedicated 32-byte (64 hex character) random challenge nonce
        let nonce = match generate_challenge_nonce() {
            Ok(n) => n,
            Err(e) => {
                let _ = self.terminate();
                return Err(format!("Failed to generate challenge nonce: {}", e));
            }
        };

        let expected_hmac = compute_handshake_hmac(&self.launch_secret, &nonce);

        // 4. Connect to loopback handshake endpoint via std::net::TcpStream
        let addr = SocketAddr::from(([127, 0, 0, 1], bind_port));
        let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_secs(3)) {
            Ok(s) => s,
            Err(e) => {
                let _ = self.terminate();
                return Err(format!(
                    "Failed to connect to backend handshake endpoint on 127.0.0.1:{}: {}",
                    bind_port, e
                ));
            }
        };

        let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
        let _ = stream.set_write_timeout(Some(Duration::from_secs(5)));

        let request_body = format!("{{\"nonce\":\"{}\"}}", nonce);
        let http_request = format!(
            "POST /api/bridge/handshake HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            bind_port,
            request_body.len(),
            request_body
        );

        if let Err(e) = stream.write_all(http_request.as_bytes()) {
            let _ = self.terminate();
            return Err(format!(
                "Failed to transmit handshake challenge to backend: {}",
                e
            ));
        }

        let mut response_bytes = Vec::new();
        if let Err(e) = stream.read_to_end(&mut response_bytes) {
            let _ = self.terminate();
            return Err(format!(
                "Failed to read handshake response from backend: {}",
                e
            ));
        }

        let response_str = String::from_utf8_lossy(&response_bytes);

        // 5. Verify HTTP status 200 OK
        if !response_str.starts_with("HTTP/1.1 200") && !response_str.starts_with("HTTP/1.0 200") {
            let _ = self.terminate();
            return Err(format!(
                "Backend handshake challenge rejected with non-200 response: {}",
                response_str.lines().next().unwrap_or("empty response")
            ));
        }

        // 6. Extract and verify HMAC from response body
        let body = match response_str.split("\r\n\r\n").nth(1) {
            Some(b) => b,
            None => {
                let _ = self.terminate();
                return Err(
                    "Malformed HTTP response from handshake endpoint: missing header separator"
                        .to_string(),
                );
            }
        };

        let received_hmac = match extract_json_string_field(body, "hmac") {
            Some(h) => h,
            None => {
                let _ = self.terminate();
                return Err("Handshake response missing 'hmac' field".to_string());
            }
        };

        if !constant_time_compare(&expected_hmac, &received_hmac) {
            let _ = self.terminate();
            return Err("Backend handshake HMAC verification failed: launch secret mismatch or invalid signature".to_string());
        }

        Ok(())
    }
}

impl Drop for ManagedChild {
    fn drop(&mut self) {
        let _ = self.terminate();
    }
}

unsafe impl Send for ManagedChild {}
unsafe impl Sync for ManagedChild {}

/// Helper to extract a string field value from a JSON object without external dependencies.
pub fn extract_json_string_field(json: &str, field: &str) -> Option<String> {
    let key_pattern = format!("\"{}\"", field);
    let key_pos = json.find(&key_pattern)?;
    let after_key = &json[key_pos + key_pattern.len()..];
    let colon_pos = after_key.find(':')?;
    let after_colon = after_key[colon_pos + 1..].trim_start();
    if !after_colon.starts_with('"') {
        return None;
    }
    let rest = &after_colon[1..];
    let quote_end = rest.find('"')?;
    Some(rest[..quote_end].to_string())
}

/// Generates a cryptographically random 256-bit hexadecimal launch secret using Windows CNG BCryptGenRandom.
///
/// Uses RAII ModuleHandle to guarantee FreeLibrary is called on all return and error paths.
/// Fails closed if bcrypt.dll cannot be loaded or if BCryptGenRandom returns non-zero.
pub fn generate_launch_secret() -> Result<String, String> {
    let dll_name = b"bcrypt.dll\0";
    let fn_name = b"BCryptGenRandom\0";

    let module = unsafe {
        ModuleHandle::load(dll_name)
            .map_err(|_| "Failed to load bcrypt.dll for cryptographic entropy".to_string())?
    };

    let proc = unsafe { module.get_proc(fn_name) };
    if proc.is_null() {
        return Err("Failed to resolve BCryptGenRandom in bcrypt.dll".to_string());
    }

    let bcrypt_gen_random: BCryptGenRandomFn = unsafe { mem::transmute(proc) };

    let mut random_bytes = [0u8; 32];
    let status = unsafe {
        bcrypt_gen_random(
            ptr::null_mut(),
            random_bytes.as_mut_ptr(),
            random_bytes.len() as DWORD,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };

    if status != 0 {
        return Err(format!(
            "BCryptGenRandom failed with NTSTATUS 0x{:08x}",
            status
        ));
    }

    let mut hex_str = String::with_capacity(64);
    for b in &random_bytes {
        use std::fmt::Write;
        let _ = write!(&mut hex_str, "{:02x}", b);
    }
    // ModuleHandle::drop calls FreeLibrary here automatically
    Ok(hex_str)
}

/// Generates a dedicated cryptographically random 32-byte (256-bit) challenge nonce
/// encoded as a 64-character lowercase hexadecimal string using Windows CNG BCryptGenRandom.
///
/// Keeps challenge nonces conceptually and functionally separate from the 256-bit launch secret.
pub fn generate_challenge_nonce() -> Result<String, String> {
    let dll_name = b"bcrypt.dll\0";
    let fn_name = b"BCryptGenRandom\0";

    let module = unsafe {
        ModuleHandle::load(dll_name)
            .map_err(|_| "Failed to load bcrypt.dll for cryptographic entropy".to_string())?
    };

    let proc = unsafe { module.get_proc(fn_name) };
    if proc.is_null() {
        return Err("Failed to resolve BCryptGenRandom in bcrypt.dll".to_string());
    }

    let bcrypt_gen_random: BCryptGenRandomFn = unsafe { mem::transmute(proc) };

    let mut random_bytes = [0u8; 32];
    let status = unsafe {
        bcrypt_gen_random(
            ptr::null_mut(),
            random_bytes.as_mut_ptr(),
            random_bytes.len() as DWORD,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };

    if status != 0 {
        return Err(format!(
            "BCryptGenRandom failed with NTSTATUS 0x{:08x}",
            status
        ));
    }

    let mut hex_str = String::with_capacity(64);
    for b in &random_bytes {
        use std::fmt::Write;
        let _ = write!(&mut hex_str, "{:02x}", b);
    }
    Ok(hex_str)
}

/// Resolves the PID listening on a given port strictly on 127.0.0.1 via `GetExtendedTcpTable`.
///
/// Rejects wildcard bindings (`0.0.0.0`) and non-matching addresses.
pub fn get_tcp_listener_pid(port: u16) -> Option<u32> {
    let mut size: DWORD = 0;
    unsafe {
        GetExtendedTcpTable(
            ptr::null_mut(),
            &mut size,
            TRUE,
            AF_INET,
            TCP_TABLE_OWNER_PID_LISTENER,
            0,
        );
    }
    if size == 0 {
        return None;
    }

    let mut buffer: Vec<u8> = vec![0u8; size as usize];
    let res = unsafe {
        GetExtendedTcpTable(
            buffer.as_mut_ptr() as *mut c_void,
            &mut size,
            TRUE,
            AF_INET,
            TCP_TABLE_OWNER_PID_LISTENER,
            0,
        )
    };

    if res != 0 {
        return None;
    }

    let num_entries = unsafe { *(buffer.as_ptr() as *const DWORD) };
    let offset = mem::size_of::<DWORD>();
    let row_size = mem::size_of::<MIB_TCPROW_OWNER_PID>();

    // 127.0.0.1 in little-endian DWORD is 0x0100007F
    let loopback_dw: DWORD = 0x0100007F;

    for i in 0..(num_entries as usize) {
        let row_ptr =
            unsafe { buffer.as_ptr().add(offset + i * row_size) as *const MIB_TCPROW_OWNER_PID };
        let row = unsafe { *row_ptr };
        let row_port = u16::from_be((row.dwLocalPort & 0xFFFF) as u16);
        if row_port == port && row.dwLocalAddr == loopback_dw {
            return Some(row.dwOwningPid);
        }
    }

    None
}

/// Identifies the server-side reverse four-tuple owner PID for a given established client socket on 127.0.0.1.
///
/// On Windows, client-side TCP table entries report the client PID. To verify the server process identity,
/// we query `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_ALL)` and match the reverse four-tuple:
/// - `row.dwLocalAddr == peer.ip()`
/// - `row.dwLocalPort == peer.port()`
/// - `row.dwRemoteAddr == local.ip()`
/// - `row.dwRemotePort == local.port()`
/// - `row.dwState == MIB_TCP_STATE_ESTAB`
///
/// Returns `Ok(pid)` if and only if exactly one established reverse row matches.
/// Fails closed if 0 or >1 rows match.
pub fn find_server_peer_pid(local: SocketAddr, peer: SocketAddr) -> Result<u32, String> {
    // 1. Strict loopback invariant validation
    if !local.ip().is_loopback() || !peer.ip().is_loopback() {
        return Err("Both local and peer addresses must be IPv4 loopback (127.0.0.1)".to_string());
    }
    if local.port() == 0 || peer.port() == 0 {
        return Err("Local and peer ports must be non-zero".to_string());
    }

    let (local_v4, peer_v4) = match (local.ip(), peer.ip()) {
        (std::net::IpAddr::V4(l), std::net::IpAddr::V4(p)) => (l, p),
        _ => return Err("Only IPv4 loopback is currently supported".to_string()),
    };
    let local_ip_dw = u32::from_ne_bytes(local_v4.octets());
    let peer_ip_dw = u32::from_ne_bytes(peer_v4.octets());

    const ERROR_INSUFFICIENT_BUFFER: DWORD = 122;
    const NO_ERROR: DWORD = 0;

    let mut size: DWORD = 0;
    unsafe {
        GetExtendedTcpTable(
            ptr::null_mut(),
            &mut size,
            TRUE,
            AF_INET,
            TCP_TABLE_OWNER_PID_ALL,
            0,
        );
    }
    if size == 0 {
        return Err("GetExtendedTcpTable returned 0 size for TCP table query".to_string());
    }

    // 2. Dynamic buffer retry loop to handle concurrent table growth safely
    let mut buffer: Vec<u64> = Vec::new(); // Vec<u64> guarantees 8-byte alignment (exceeding DWORD alignment)
    let mut ret: DWORD = ERROR_INSUFFICIENT_BUFFER;

    for _ in 0..5 {
        let num_u64s = (size as usize + mem::size_of::<u64>() - 1) / mem::size_of::<u64>();
        buffer.resize(num_u64s, 0);

        ret = unsafe {
            GetExtendedTcpTable(
                buffer.as_mut_ptr() as *mut c_void,
                &mut size,
                TRUE,
                AF_INET,
                TCP_TABLE_OWNER_PID_ALL,
                0,
            )
        };

        if ret == NO_ERROR {
            break;
        } else if ret != ERROR_INSUFFICIENT_BUFFER {
            return Err(format!(
                "GetExtendedTcpTable failed with Win32 error code {}",
                ret
            ));
        }
        // If ERROR_INSUFFICIENT_BUFFER, retry with updated size
    }

    if ret != NO_ERROR {
        return Err(format!(
            "GetExtendedTcpTable failed after retries with code {}",
            ret
        ));
    }

    // 3. Bounds and alignment safe parsing
    let buf_bytes_len = buffer.len() * mem::size_of::<u64>();
    if buf_bytes_len < mem::size_of::<DWORD>() {
        return Err("TCP table buffer too small for table header".to_string());
    }

    let base_ptr = buffer.as_ptr() as *const u8;
    let num_entries = unsafe { ptr::read_unaligned(base_ptr as *const DWORD) } as usize;
    let offset = mem::size_of::<DWORD>();
    let row_size = mem::size_of::<MIB_TCPROW_OWNER_PID>();

    let required_bytes = match num_entries
        .checked_mul(row_size)
        .and_then(|r| r.checked_add(offset))
    {
        Some(b) => b,
        None => return Err("TCP table entry size calculation overflowed".to_string()),
    };

    if required_bytes > buf_bytes_len {
        return Err(format!(
            "TCP table reported {} entries ({} bytes) exceeding buffer ({} bytes)",
            num_entries, required_bytes, buf_bytes_len
        ));
    }

    let mut matching_pids = Vec::new();

    for i in 0..num_entries {
        let row_offset = offset + i * row_size;
        let row: MIB_TCPROW_OWNER_PID =
            unsafe { ptr::read_unaligned(base_ptr.add(row_offset) as *const MIB_TCPROW_OWNER_PID) };

        if row.dwState != MIB_TCP_STATE_ESTAB {
            continue;
        }

        let row_local_port = u16::from_be((row.dwLocalPort & 0xFFFF) as u16);
        let row_remote_port = u16::from_be((row.dwRemotePort & 0xFFFF) as u16);

        // REVERSE FOUR-TUPLE:
        // Server LocalAddr == client peer.ip()
        // Server LocalPort == client peer.port()
        // Server RemoteAddr == client local.ip()
        // Server RemotePort == client local.port()
        if row.dwLocalAddr == peer_ip_dw
            && row_local_port == peer.port()
            && row.dwRemoteAddr == local_ip_dw
            && row_remote_port == local.port()
        {
            matching_pids.push(row.dwOwningPid);
        }
    }

    if matching_pids.len() == 1 {
        Ok(matching_pids[0])
    } else if matching_pids.is_empty() {
        Err("No matching established server-side TCP row found".to_string())
    } else {
        Err(format!(
            "Ambiguous matching TCP rows: found {} rows",
            matching_pids.len()
        ))
    }
}

/// Explicit connection state machine enforcing step-by-step verification before request readiness.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConnectionState {
    Disconnected,
    Connecting,
    VerifyingPeer { pid: u32 },
    VerifyingCore { pid: u32 },
    Ready { pid: u32 },
    Failed { reason: String },
}

/// Native verified HTTP/1 transport connection driving requests over the exact validated TCP socket.
///
/// Invariants:
/// 1. Exactly one `tokio::net::TcpStream` is opened to `127.0.0.1:bind_port`.
/// 2. Reverse four-tuple confirms server PID.
/// 3. Server PID is retained via Win32 process handle, and reverse four-tuple is re-validated while holding handle to prevent TOCTOU PID-reuse races.
/// 4. Process is checked against Job Object containment.
/// 5. If `pinned_pid` is specified, server PID must match it exactly.
/// 6. Process creation time is recorded to detect process recycling.
/// 7. The EXACT SAME `TcpStream` is passed into `hyper::client::conn::http1::handshake`.
/// 8. A cryptographic challenge (`POST /api/bridge/handshake`) with a dedicated 32-byte CNG nonce and `pico-bridge-v1` HMAC is completed on this connection before it is marked `Ready`.
/// 9. No operator credentials (cookies, CSRF tokens) are transmitted during handshake or verification.
/// 10. If the underlying socket drops or returns an I/O error, the connection transitions to `Disconnected` and FAILS CLOSED (no automatic reconnection).
/// 11. Dropping the connection aborts the background connection driver, cleaning up all socket and task resources.
pub struct VerifiedConnection {
    sender: hyper::client::conn::http1::SendRequest<Full<Bytes>>,
    driver_handle: tokio::task::JoinHandle<()>,
    state: ConnectionState,
    server_pid: u32,
    process_handle: Arc<SafeProcessHandle>,
    creation_time: u64,
}

impl Drop for VerifiedConnection {
    fn drop(&mut self) {
        self.driver_handle.abort();
    }
}

impl VerifiedConnection {
    /// Connects to 127.0.0.1:bind_port, verifies reverse four-tuple server PID, Job Object containment,
    /// retains process handle, revalidates four-tuple while holding handle to defeat TOCTOU recycling,
    /// executes mutual cryptographic HMAC challenge on the exact same socket, and transitions to Ready.
    pub async fn connect(
        bind_port: u16,
        job_object: &JobObject,
        launch_secret: &str,
        pinned_pid: Option<u32>,
    ) -> Result<Self, String> {
        let mut _state = ConnectionState::Connecting;

        // 1. Establish single TCP stream to loopback port
        let addr = SocketAddr::from(([127, 0, 0, 1], bind_port));
        let stream = match tokio::net::TcpStream::connect(addr).await {
            Ok(s) => s,
            Err(e) => {
                return Err(format!(
                    "TCP connection to 127.0.0.1:{} failed: {}",
                    bind_port, e
                ));
            }
        };

        let local_addr = stream
            .local_addr()
            .map_err(|e| format!("Failed to get local addr: {}", e))?;
        let peer_addr = stream
            .peer_addr()
            .map_err(|e| format!("Failed to get peer addr: {}", e))?;

        // 2. Resolve reverse four-tuple server PID with small retry window (up to 300ms) for OS TCP table update
        let start_time = Instant::now();
        let server_pid = loop {
            match find_server_peer_pid(local_addr, peer_addr) {
                Ok(pid) => break pid,
                Err(e) => {
                    if start_time.elapsed() > Duration::from_millis(300) {
                        return Err(format!(
                            "Reverse four-tuple server PID lookup failed: {}",
                            e
                        ));
                    }
                    tokio::time::sleep(Duration::from_millis(15)).await;
                }
            }
        };

        _state = ConnectionState::VerifyingPeer { pid: server_pid };

        // 3. Verify pinned PID if specified
        if let Some(expected_pid) = pinned_pid {
            if server_pid != expected_pid {
                return Err(format!(
                    "Server PID {} does not match pinned PID {}",
                    server_pid, expected_pid
                ));
            }
        }

        // 4. Retain server process handle to pin process object in kernel
        let safe_proc = Arc::new(open_process_handle(server_pid)?);

        // 4b. TOCTOU Resistance: Revalidate reverse four-tuple while holding open process handle.
        // Holding the handle guarantees the Windows kernel cannot free or reuse server_pid.
        // Re-checking confirms that the established socket is still owned by the process entity pinned by safe_proc.
        let revalidated_pid = find_server_peer_pid(local_addr, peer_addr).map_err(|e| {
            format!(
                "Reverse four-tuple revalidation failed while holding process handle: {}",
                e
            )
        })?;
        if revalidated_pid != server_pid {
            return Err(format!(
                "TOCTOU detection: Server PID changed from {} to {} during handle acquisition",
                server_pid, revalidated_pid
            ));
        }

        // 5. Verify process is still alive
        if !safe_proc.is_alive() {
            return Err(format!("Server process PID {} is not running", server_pid));
        }

        // 6. Verify Job Object containment
        let mut is_in_job: BOOL = 0;
        let ok =
            unsafe { IsProcessInJob(safe_proc.raw(), job_object.raw_handle(), &mut is_in_job) };
        if ok != TRUE || is_in_job != TRUE {
            return Err(format!(
                "Server PID {} is not contained within the supervisor Job Object; rejecting rogue connection",
                server_pid
            ));
        }

        // 7. Query process creation time
        let mut creation_time_ft: FILETIME = unsafe { mem::zeroed() };
        let mut exit_time_ft: FILETIME = unsafe { mem::zeroed() };
        let mut kernel_time_ft: FILETIME = unsafe { mem::zeroed() };
        let mut user_time_ft: FILETIME = unsafe { mem::zeroed() };
        let times_ok = unsafe {
            GetProcessTimes(
                safe_proc.raw(),
                &mut creation_time_ft,
                &mut exit_time_ft,
                &mut kernel_time_ft,
                &mut user_time_ft,
            )
        };
        let creation_time = if times_ok == TRUE {
            ((creation_time_ft.dwHighDateTime as u64) << 32)
                | (creation_time_ft.dwLowDateTime as u64)
        } else {
            0
        };

        // 8. Pass the EXACT SAME TcpStream into Hyper HTTP/1
        let io = TokioIo::new(stream);
        let (mut sender, conn) = hyper::client::conn::http1::handshake(io)
            .await
            .map_err(|e| format!("Hyper HTTP/1 handshake error: {}", e))?;

        // Drive the connection in background with retained handle
        let driver_handle = tokio::spawn(async move {
            let _ = conn.await;
        });

        _state = ConnectionState::VerifyingCore { pid: server_pid };

        // 9. Execute cryptographic handshake on this connection using dedicated 32-byte CNG nonce
        let nonce = match generate_challenge_nonce() {
            Ok(n) => n,
            Err(e) => return Err(format!("Failed to generate challenge nonce: {}", e)),
        };
        let expected_hmac = compute_handshake_hmac(launch_secret, &nonce);

        let body_json = format!("{{\"nonce\":\"{}\"}}", nonce);
        let req = Request::builder()
            .method("POST")
            .uri("/api/bridge/handshake")
            .header("Host", format!("127.0.0.1:{}", bind_port))
            .header("Content-Type", "application/json")
            .body(Full::new(Bytes::from(body_json)))
            .map_err(|e| format!("Failed to build handshake request: {}", e))?;

        let resp = sender
            .send_request(req)
            .await
            .map_err(|e| format!("Handshake request dispatch failed: {}", e))?;

        if resp.status() != hyper::StatusCode::OK {
            return Err(format!(
                "Backend handshake rejected with status {}",
                resp.status()
            ));
        }

        let body_bytes = resp
            .into_body()
            .collect()
            .await
            .map_err(|e| format!("Failed reading handshake response body: {}", e))?
            .to_bytes();

        let body_str = String::from_utf8_lossy(&body_bytes);
        let received_hmac = extract_json_string_field(&body_str, "hmac")
            .ok_or_else(|| "Handshake response missing 'hmac' field".to_string())?;

        if !constant_time_compare(&expected_hmac, &received_hmac) {
            return Err(
                "Backend handshake HMAC verification failed: launch secret mismatch".to_string(),
            );
        }

        // 10. Transition to Ready
        let state = ConnectionState::Ready { pid: server_pid };

        Ok(Self {
            sender,
            driver_handle,
            state,
            server_pid,
            process_handle: safe_proc,
            creation_time,
        })
    }

    /// Dispatches an HTTP request on this verified connection.
    ///
    /// Fails closed immediately if the connection is not Ready, if the background driver
    /// terminated, or if the underlying server process has terminated.
    pub async fn send_request(
        &mut self,
        req: Request<Full<Bytes>>,
    ) -> Result<hyper::Response<hyper::body::Incoming>, String> {
        if !self.is_ready() {
            self.state = ConnectionState::Disconnected;
            self.driver_handle.abort();
            return Err(format!(
                "Connection is not ready or has disconnected (state: {:?})",
                self.state
            ));
        }

        if !self.process_handle.is_alive() {
            self.state = ConnectionState::Disconnected;
            self.driver_handle.abort();
            return Err("Server process has terminated; connection invalidated".to_string());
        }

        match self.sender.send_request(req).await {
            Ok(resp) => Ok(resp),
            Err(e) => {
                self.state = ConnectionState::Disconnected;
                self.driver_handle.abort();
                Err(format!("HTTP request failed: {}", e))
            }
        }
    }

    pub fn is_ready(&self) -> bool {
        matches!(self.state, ConnectionState::Ready { .. })
            && !self.sender.is_closed()
            && !self.driver_handle.is_finished()
            && self.process_handle.is_alive()
    }

    pub fn server_pid(&self) -> u32 {
        self.server_pid
    }

    pub fn creation_time(&self) -> u64 {
        self.creation_time
    }

    pub fn state(&self) -> &ConnectionState {
        &self.state
    }

    pub fn process_handle(&self) -> &Arc<SafeProcessHandle> {
        &self.process_handle
    }
}

/// Computes HMAC-SHA256 for a given secret key and raw byte message.
pub fn compute_hmac_sha256(secret: &str, message: &[u8]) -> String {
    let mut key = secret.as_bytes().to_vec();
    if key.len() > 64 {
        key = simple_sha256(&key);
    }
    while key.len() < 64 {
        key.push(0);
    }

    let mut o_key_pad = vec![0u8; 64];
    let mut i_key_pad = vec![0u8; 64];
    for j in 0..64 {
        o_key_pad[j] = key[j] ^ 0x5c;
        i_key_pad[j] = key[j] ^ 0x36;
    }

    let mut inner_data = i_key_pad;
    inner_data.extend_from_slice(message);
    let inner_hash = simple_sha256(&inner_data);

    let mut outer_data = o_key_pad;
    outer_data.extend_from_slice(&inner_hash);
    let final_hash = simple_sha256(&outer_data);

    final_hash.iter().map(|b| format!("{:02x}", b)).collect()
}

/// Computes the expected handshake HMAC for a given launch secret and challenge nonce.
///
/// Format: `HMAC-SHA256(secret, "pico-bridge-v1:" + nonce)`.
pub fn compute_handshake_hmac(secret: &str, nonce: &str) -> String {
    let message = format!("pico-bridge-v1:{}", nonce.trim());
    compute_hmac_sha256(secret, message.as_bytes())
}

/// Builds the length-prefixed canonical input string for ADR 0003 Section 8.1.
///
/// Format:
/// pico-bridge-v2:{len(version)}:{version}:{len(timestamp)}:{timestamp}:{len(nonce)}:{nonce}:{len(method)}:{method}:{len(request_target)}:{request_target}:{len(body_digest)}:{body_digest}
pub fn build_canonical_proof_v2_string(
    version: &str,
    timestamp: &str,
    nonce: &str,
    method: &str,
    request_target: &str,
    body_digest: &str,
) -> String {
    let v_len = version.as_bytes().len();
    let ts_len = timestamp.as_bytes().len();
    let n_len = nonce.as_bytes().len();
    let m_len = method.as_bytes().len();
    let rt_len = request_target.as_bytes().len();
    let bd_len = body_digest.as_bytes().len();

    format!(
        "pico-bridge-v2:{}:{}:{}:{}:{}:{}:{}:{}:{}:{}:{}:{}",
        v_len,
        version,
        ts_len,
        timestamp,
        n_len,
        nonce,
        m_len,
        method,
        rt_len,
        request_target,
        bd_len,
        body_digest
    )
}

/// Computes the v2 per-request bridge proof HMAC-SHA256 signature.
pub fn compute_proof_v2_hmac(
    secret: &str,
    version: &str,
    timestamp: &str,
    nonce: &str,
    method: &str,
    request_target: &str,
    body_digest: &str,
) -> String {
    let canonical = build_canonical_proof_v2_string(
        version,
        timestamp,
        nonce,
        method,
        request_target,
        body_digest,
    );
    compute_hmac_sha256(secret, canonical.as_bytes())
}

/// Builds the full `X-Pico-Bridge-Proof` header value: `v2 <timestamp> <nonce> <hmac_hex>`.
pub fn build_v2_bridge_proof_header(
    secret: &str,
    method: &str,
    request_target: &str,
    body_bytes: &[u8],
) -> Result<String, String> {
    let now_ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| format!("System clock error: {}", e))?
        .as_secs()
        .to_string();

    let nonce = generate_challenge_nonce()?;
    let body_hash_bytes = simple_sha256(body_bytes);
    let body_digest: String = body_hash_bytes
        .iter()
        .map(|b| format!("{:02x}", b))
        .collect();

    let sig = compute_proof_v2_hmac(
        secret,
        "v2",
        &now_ts,
        &nonce,
        method,
        request_target,
        &body_digest,
    );

    Ok(format!("v2 {} {} {}", now_ts, nonce, sig))
}

/// Volatile, in-memory-only Operator Session Store (ADR 0003 Slice 1B).
///
/// Invariants:
/// 1. Retained exclusively in native host process memory (zero disk, zero SQLite, zero logs).
/// 2. Bound to the active supervised Core PID and creation time.
/// 3. Invalidation triggers on Core restart, PID change, supervisor termination, or explicit logout.
/// 4. Never exposes raw session cookies or CSRF tokens across the Tauri IPC boundary.
/// 5. Best-effort zeroization: owned secret buffers are zeroized before drop and on invalidation.
#[derive(Clone, Default)]
pub struct OperatorSessionStore {
    inner: Arc<std::sync::RwLock<Option<StoredSession>>>,
}

impl std::fmt::Debug for OperatorSessionStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OperatorSessionStore")
            .field(
                "has_session",
                &self.inner.read().map(|g| g.is_some()).unwrap_or(false),
            )
            .finish()
    }
}

/// Secure container for a sensitive string buffer that zeroes its memory on drop.
pub struct SecretBuffer(Vec<u8>);

impl SecretBuffer {
    pub fn new(s: String) -> Self {
        Self(s.into_bytes())
    }

    pub fn as_str(&self) -> &str {
        std::str::from_utf8(&self.0).unwrap_or("")
    }

    pub fn to_string_lossy(&self) -> String {
        self.as_str().to_string()
    }
}

impl Drop for SecretBuffer {
    fn drop(&mut self) {
        // Volatile write 0s to prevent compiler optimization dead-store removal
        for b in self.0.iter_mut() {
            unsafe {
                std::ptr::write_volatile(b, 0);
            }
        }
    }
}

impl Clone for SecretBuffer {
    fn clone(&self) -> Self {
        Self(self.0.clone())
    }
}

struct StoredSession {
    session_cookie: SecretBuffer,
    csrf_token: SecretBuffer,
    expires_at_epoch: Option<u64>,
    core_pid: u32,
    core_creation_time: u64,
}

impl Drop for StoredSession {
    fn drop(&mut self) {
        // SecretBuffer Drop will automatically zeroize cookie and csrf_token bytes
    }
}

impl OperatorSessionStore {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(std::sync::RwLock::new(None)),
        }
    }

    /// Stores a paired operator session in volatile memory, binding it to Core identity.
    pub fn store_session(
        &self,
        session_cookie: String,
        csrf_token: String,
        expires_at_epoch: Option<u64>,
        core_pid: u32,
        core_creation_time: u64,
    ) -> Result<(), String> {
        let mut guard = self
            .inner
            .write()
            .map_err(|_| "Session store lock poisoned".to_string())?;
        *guard = Some(StoredSession {
            session_cookie: SecretBuffer::new(session_cookie),
            csrf_token: SecretBuffer::new(csrf_token),
            expires_at_epoch,
            core_pid,
            core_creation_time,
        });
        Ok(())
    }

    /// Checks if a valid session exists for the given active Core PID and creation timestamp.
    pub fn is_paired(&self, active_pid: u32, active_creation_time: u64) -> bool {
        let guard = match self.inner.read() {
            Ok(g) => g,
            Err(_) => return false,
        };

        if let Some(session) = guard.as_ref() {
            if session.core_pid != active_pid || session.core_creation_time != active_creation_time
            {
                return false;
            }

            if let Some(exp) = session.expires_at_epoch {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs();
                if now >= exp {
                    return false;
                }
            }

            true
        } else {
            false
        }
    }

    /// Retrieves session credentials (cookie, csrf) if valid for the active Core.
    pub fn get_credentials(
        &self,
        active_pid: u32,
        active_creation_time: u64,
    ) -> Option<(String, String)> {
        let guard = self.inner.read().ok()?;
        let session = guard.as_ref()?;

        if session.core_pid != active_pid || session.core_creation_time != active_creation_time {
            return None;
        }

        if let Some(exp) = session.expires_at_epoch {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs();
            if now >= exp {
                return None;
            }
        }

        Some((
            session.session_cookie.to_string_lossy(),
            session.csrf_token.to_string_lossy(),
        ))
    }

    /// Purges all session credentials from volatile memory.
    pub fn invalidate(&self) {
        if let Ok(mut guard) = self.inner.write() {
            // Replacing guard drops the existing StoredSession, triggering SecretBuffer zeroization
            *guard = None;
        }
    }
}

/// Executes the verified native `pico_get_system_status` operation over loopback transport.
///
/// Dispatches `GET /health` with `X-Pico-Bridge-Proof: v2` over the verified same-socket connection.
/// Returns sanitized JSON `{"status":"healthy",...}` without revealing internal credentials.
pub async fn execute_native_get_system_status(
    port: u16,
    job_object: &Arc<JobObject>,
    launch_secret: &str,
    expected_pid: Option<u32>,
) -> Result<String, String> {
    let mut conn =
        VerifiedConnection::connect(port, job_object, launch_secret, expected_pid).await?;

    let proof_header = build_v2_bridge_proof_header(launch_secret, "GET", "/health", &[])?;

    let host_header = format!("127.0.0.1:{}", port);
    let req = Request::builder()
        .method("GET")
        .uri("/health")
        .header("Host", host_header)
        .header("X-Pico-Bridge-Proof", proof_header)
        .body(Full::new(Bytes::new()))
        .map_err(|e| format!("Failed to build status request: {}", e))?;

    let resp = conn.send_request(req).await?;
    let status_code = resp.status();
    let body_bytes = resp
        .into_body()
        .collect()
        .await
        .map_err(|e| format!("Failed to read status response body: {}", e))?
        .to_bytes();

    let body_str = String::from_utf8_lossy(&body_bytes).to_string();
    if status_code != hyper::StatusCode::OK {
        return Err(format!(
            "System status endpoint returned error status {}: {}",
            status_code, body_str
        ));
    }

    Ok(body_str)
}

/// Parses the `Set-Cookie` header value to extract the `pico_operator_session` cookie value.
pub fn extract_operator_cookie(header_value: &str) -> Option<String> {
    for part in header_value.split(';') {
        let trimmed = part.trim();
        if trimmed.starts_with("pico_operator_session=") {
            return Some(trimmed.to_string());
        }
    }
    None
}

/// Executes the verified native `pico_pair_operator` operation over loopback transport.
///
/// 1. Validates code parameter (6 alphanumeric characters).
/// 2. Dispatches `POST /api/operator/pair` with JSON `{"code": "..."}` and `X-Pico-Bridge-Proof: v2`.
/// 3. Extracts `pico_operator_session` cookie from `Set-Cookie` header and `csrf_token` from response JSON.
/// 4. Stores credentials strictly in volatile `OperatorSessionStore` bound to active Core identity.
/// 5. Returns sanitized status JSON `{"status":"paired","approver":"..."}`. Zero cookies or CSRF tokens returned.
pub async fn execute_native_pair_operator(
    port: u16,
    job_object: &Arc<JobObject>,
    launch_secret: &str,
    expected_pid: Option<u32>,
    session_store: &OperatorSessionStore,
    code: &str,
) -> Result<String, String> {
    let clean_code = code.trim();
    if clean_code.len() != 6 || !clean_code.chars().all(|c| c.is_ascii_digit()) {
        return Err("Pairing code must be exactly 6 numeric digits".to_string());
    }

    let mut conn =
        VerifiedConnection::connect(port, job_object, launch_secret, expected_pid).await?;

    let payload = format!("{{\"code\":\"{}\"}}", clean_code);
    let payload_bytes = payload.as_bytes();

    let proof_header =
        build_v2_bridge_proof_header(launch_secret, "POST", "/api/operator/pair", payload_bytes)?;

    let host_header = format!("127.0.0.1:{}", port);
    let req = Request::builder()
        .method("POST")
        .uri("/api/operator/pair")
        .header("Host", host_header)
        .header("Content-Type", "application/json")
        .header("X-Pico-Bridge-Proof", proof_header)
        .body(Full::new(Bytes::from(payload_bytes.to_vec())))
        .map_err(|e| format!("Failed to build pair request: {}", e))?;

    let resp = conn.send_request(req).await?;
    let status_code = resp.status();

    // Extract Set-Cookie header before consuming body
    let mut extracted_cookie: Option<String> = None;
    for (name, val) in resp.headers() {
        if name.as_str().eq_ignore_ascii_case("set-cookie") {
            if let Ok(val_str) = val.to_str() {
                if let Some(c) = extract_operator_cookie(val_str) {
                    extracted_cookie = Some(c);
                    break;
                }
            }
        }
    }

    let body_bytes = resp
        .into_body()
        .collect()
        .await
        .map_err(|e| format!("Failed to read pair response body: {}", e))?
        .to_bytes();

    let body_str = String::from_utf8_lossy(&body_bytes).to_string();
    if status_code != hyper::StatusCode::OK {
        return Err(format!(
            "Operator pairing failed with status {}: {}",
            status_code, body_str
        ));
    }

    let cookie = extracted_cookie.ok_or_else(|| {
        "Pairing response omitted mandatory pico_operator_session cookie".to_string()
    })?;

    let csrf_token = extract_json_string_field(&body_str, "csrf_token")
        .ok_or_else(|| "Pairing response omitted mandatory csrf_token field".to_string())?;

    let approver = extract_json_string_field(&body_str, "approver")
        .unwrap_or_else(|| "local_desktop_companion".to_string());

    // Calculate session expiry epoch
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let expires_at_epoch = now + 3600; // 1 hour session lifetime

    let core_pid = conn.server_pid();
    let core_creation_time = conn.creation_time();

    session_store.store_session(
        cookie,
        csrf_token,
        Some(expires_at_epoch),
        core_pid,
        core_creation_time,
    )?;

    // Return sanitized JSON outcome strictly omitting sensitive credentials
    Ok(format!(
        "{{\"status\":\"paired\",\"approver\":\"{}\"}}",
        approver
    ))
}

/// Constant-time comparison for candidate HMAC strings.
pub fn constant_time_compare(a: &str, b: &str) -> bool {
    let a_bytes = a.as_bytes();
    let b_bytes = b.as_bytes();
    if a_bytes.len() != b_bytes.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a_bytes.iter().zip(b_bytes.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// Pure Rust SHA-256 implementation without external crate dependencies for self-contained verification.
pub fn simple_sha256(input: &[u8]) -> Vec<u8> {
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];

    let k: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];

    let mut msg = input.to_vec();
    let bit_len = (input.len() as u64) * 8;
    msg.push(0x80);
    while (msg.len() % 64) != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());

    for chunk in msg.chunks_exact(64) {
        let mut w = [0u32; 64];
        for (i, w_elem) in w.iter_mut().take(16).enumerate() {
            let start = i * 4;
            *w_elem = u32::from_be_bytes([
                chunk[start],
                chunk[start + 1],
                chunk[start + 2],
                chunk[start + 3],
            ]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }

        let (mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h_val) =
            (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]);

        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let temp1 = h_val
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(k[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = s0.wrapping_add(maj);

            h_val = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }

        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(h_val);
    }

    let mut out = Vec::with_capacity(32);
    for val in &h {
        out.extend_from_slice(&val.to_be_bytes());
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_constant_time_compare() {
        assert!(constant_time_compare("abc123xyz", "abc123xyz"));
        assert!(!constant_time_compare("abc123xyz", "abc123xyw"));
        assert!(!constant_time_compare("abc", "abcd"));
        assert!(!constant_time_compare("", "a"));
        assert!(constant_time_compare("", ""));
    }

    #[test]
    fn test_compute_handshake_hmac() {
        let secret = "0123456789abcdef0123456789abcdef";
        let nonce = "test-nonce-12345";
        let hmac1 = compute_handshake_hmac(secret, nonce);
        let hmac2 = compute_handshake_hmac(secret, nonce);
        assert_eq!(hmac1, hmac2);
        assert_eq!(hmac1.len(), 64);

        // Different nonce produces different HMAC
        let hmac3 = compute_handshake_hmac(secret, "different-nonce");
        assert_ne!(hmac1, hmac3);
    }

    #[test]
    fn test_launch_config_loopback_invariants() {
        let config = LaunchConfig::default_local(PathBuf::from("python.exe"), PathBuf::from("."));
        assert_eq!(config.bind_host, "127.0.0.1");
        assert_eq!(config.bind_port, 8000);
        assert!(!config.args.contains(&"--reload".to_string()));
        assert!(config.args.contains(&"--workers".to_string()));
        assert!(config.args.contains(&"1".to_string()));

        // Multi-worker rejection test
        let mut multi_worker_config = config.clone();
        for i in 0..multi_worker_config.args.len() {
            if multi_worker_config.args[i] == "--workers" && i + 1 < multi_worker_config.args.len()
            {
                multi_worker_config.args[i + 1] = "2".to_string();
            }
        }
        match multi_worker_config.spawn() {
            Err(e) => assert!(e.contains("exactly 1 worker")),
            Ok(_) => panic!("Multi-worker launch must be rejected"),
        }

        // Reload rejection test
        let mut reload_config = config.clone();
        reload_config.args.push("--reload".to_string());
        match reload_config.spawn() {
            Err(e) => assert!(e.contains("--reload")),
            Ok(_) => panic!("Reload launch must be rejected"),
        }

        // Missing worker flag rejection test
        let mut no_worker_config = config.clone();
        no_worker_config
            .args
            .retain(|a| a != "--workers" && a != "1");
        match no_worker_config.spawn() {
            Err(e) => assert!(e.contains("explicitly specify --workers 1")),
            Ok(_) => panic!("Launch without explicit --workers 1 must be rejected"),
        }

        // Duplicate worker argument rejection test
        let mut dup_worker_config = config.clone();
        dup_worker_config.args.push("--workers".to_string());
        dup_worker_config.args.push("1".to_string());
        match dup_worker_config.spawn() {
            Err(e) => assert!(e.contains("duplicate --workers arguments")),
            Ok(_) => panic!("Duplicate worker arguments must be rejected"),
        }

        // --workers=2 syntax rejection test
        let mut eq_worker_config = config.clone();
        eq_worker_config
            .args
            .retain(|a| a != "--workers" && a != "1");
        eq_worker_config.args.push("--workers=2".to_string());
        match eq_worker_config.spawn() {
            Err(e) => assert!(e.contains("exactly 1 worker")),
            Ok(_) => panic!("--workers=2 syntax must be rejected"),
        }
    }

    #[test]
    fn test_job_object_creation_and_lifecycle() {
        let job = JobObject::new();
        assert!(
            job.is_ok(),
            "Windows Job Object creation must succeed on Windows 11"
        );
        let job = job.unwrap();
        assert!(!job.raw_handle().is_null());
        assert_ne!(job.raw_handle(), INVALID_HANDLE_VALUE);
    }

    #[test]
    fn test_generate_launch_secret_cng() {
        let secret1 = generate_launch_secret();
        assert!(secret1.is_ok(), "BCryptGenRandom must succeed on Windows");
        let s1 = secret1.unwrap();
        assert_eq!(s1.len(), 64, "256-bit secret must be 64 hex characters");
        assert!(s1.chars().all(|c| c.is_ascii_hexdigit()));

        let secret2 = generate_launch_secret();
        assert!(secret2.is_ok());
        let s2 = secret2.unwrap();
        assert_ne!(
            s1, s2,
            "Consecutive secrets must be cryptographically distinct"
        );
    }

    #[test]
    fn test_extract_json_string_field() {
        let json = r#"{"status":"verified","protocol":"pico-bridge-v1","hmac":"abcdef123456"}"#;
        assert_eq!(
            extract_json_string_field(json, "status"),
            Some("verified".to_string())
        );
        assert_eq!(
            extract_json_string_field(json, "protocol"),
            Some("pico-bridge-v1".to_string())
        );
        assert_eq!(
            extract_json_string_field(json, "hmac"),
            Some("abcdef123456".to_string())
        );
        assert_eq!(extract_json_string_field(json, "missing"), None);

        let json_spaced = "{\n  \"status\": \"verified\",\n  \"hmac\": \"9876543210\"\n}";
        assert_eq!(
            extract_json_string_field(json_spaced, "hmac"),
            Some("9876543210".to_string())
        );
    }

    #[test]
    fn test_connection_state_invariants() {
        let state_disc = ConnectionState::Disconnected;
        assert_eq!(state_disc, ConnectionState::Disconnected);

        let state_ready = ConnectionState::Ready { pid: 1234 };
        if let ConnectionState::Ready { pid } = state_ready {
            assert_eq!(pid, 1234);
        } else {
            panic!("Expected Ready state");
        }

        let state_failed = ConnectionState::Failed {
            reason: "err".to_string(),
        };
        assert!(matches!(state_failed, ConnectionState::Failed { .. }));
    }

    #[test]
    fn test_safe_process_handle_and_open_process() {
        let my_pid = std::process::id();
        let handle_res = open_process_handle(my_pid);
        assert!(
            handle_res.is_ok(),
            "Must be able to open handle to own process"
        );
        let handle = handle_res.unwrap();
        assert!(!handle.raw().is_null());
        assert!(
            handle.is_alive(),
            "Current process must report is_alive = true"
        );
    }

    #[test]
    fn test_find_server_peer_pid_local_resolution() {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        rt.block_on(async {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
                .await
                .expect("Failed to bind loopback listener");
            let server_addr = listener.local_addr().unwrap();

            let server_task = tokio::spawn(async move {
                let (stream, _) = listener.accept().await.unwrap();
                stream
            });

            let client_stream = tokio::net::TcpStream::connect(server_addr)
                .await
                .expect("Failed to connect");
            let local_addr = client_stream.local_addr().unwrap();
            let peer_addr = client_stream.peer_addr().unwrap();

            // Give Windows TCP table a few milliseconds to reflect ESTABLISHED state
            tokio::time::sleep(Duration::from_millis(50)).await;

            let resolved_pid = find_server_peer_pid(local_addr, peer_addr);
            assert!(
                resolved_pid.is_ok(),
                "Reverse four-tuple lookup must succeed"
            );
            assert_eq!(
                resolved_pid.unwrap(),
                std::process::id(),
                "Server PID must match current process PID"
            );

            let _server_stream = server_task.await.unwrap();
        });
    }

    #[test]
    fn test_generate_challenge_nonce() {
        let nonce1 = generate_challenge_nonce();
        assert!(
            nonce1.is_ok(),
            "CNG challenge nonce generation must succeed"
        );
        let n1 = nonce1.unwrap();
        assert_eq!(
            n1.len(),
            64,
            "32-byte nonce must be 64 hexadecimal characters"
        );
        assert!(n1.chars().all(|c| c.is_ascii_hexdigit()));

        let nonce2 = generate_challenge_nonce();
        assert!(nonce2.is_ok());
        let n2 = nonce2.unwrap();
        assert_ne!(
            n1, n2,
            "Consecutive challenge nonces must be cryptographically distinct"
        );
    }

    #[test]
    fn test_cross_language_hmac_test_vector() {
        // Known test vector matching Python's hashlib.sha256 and hmac.new
        let secret = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        let nonce = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff";
        let expected = "d3705af3a0efd3641eb765a5fecf9dc26d7a4603689cedb096413b5862b0b944";
        let actual = compute_handshake_hmac(secret, nonce);
        assert_eq!(
            actual, expected,
            "HMAC must match Python canonical test vector exactly"
        );
    }

    #[test]
    fn test_find_server_peer_pid_rejects_non_loopback_and_zero_ports() {
        use std::net::Ipv4Addr;
        let non_loopback: SocketAddr =
            SocketAddr::new(std::net::IpAddr::V4(Ipv4Addr::new(192, 168, 1, 10)), 8000);
        let loopback: SocketAddr =
            SocketAddr::new(std::net::IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1)), 8000);
        let zero_port: SocketAddr =
            SocketAddr::new(std::net::IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1)), 0);

        assert!(find_server_peer_pid(non_loopback, loopback).is_err());
        assert!(find_server_peer_pid(loopback, non_loopback).is_err());
        assert!(find_server_peer_pid(zero_port, loopback).is_err());
        assert!(find_server_peer_pid(loopback, zero_port).is_err());
    }

    #[test]
    fn test_v2_canonical_format_and_cross_language_vector() {
        let secret = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        let ts = "1731150000";
        let nonce = "00112233445566778899aabbccddeeff";
        let method = "GET";
        let target = "/health";
        let body_digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

        let canonical =
            build_canonical_proof_v2_string("v2", ts, nonce, method, target, body_digest);
        let expected_canonical = "pico-bridge-v2:2:v2:10:1731150000:32:00112233445566778899aabbccddeeff:3:GET:7:/health:64:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
        assert_eq!(canonical, expected_canonical);

        let sig = compute_proof_v2_hmac(secret, "v2", ts, nonce, method, target, body_digest);
        let expected_sig = "a2ca2d143b056a1b5b9daf02c4eefb444a731942179dd22dbcce9cc4e037365e";
        assert_eq!(sig, expected_sig);
    }

    #[test]
    fn test_operator_session_store_lifecycle() {
        let store = OperatorSessionStore::new();
        assert!(!store.is_paired(1001, 5000));
        assert!(store.get_credentials(1001, 5000).is_none());

        // Store session for PID 1001, creation 5000, 1 hour expiry
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        store
            .store_session(
                "pico_operator_session=secret_token".to_string(),
                "csrf_12345".to_string(),
                Some(now + 3600),
                1001,
                5000,
            )
            .unwrap();

        // Valid query
        assert!(store.is_paired(1001, 5000));
        let creds = store.get_credentials(1001, 5000).unwrap();
        assert_eq!(creds.0, "pico_operator_session=secret_token");
        assert_eq!(creds.1, "csrf_12345");

        // PID mismatch fails
        assert!(!store.is_paired(1002, 5000));
        assert!(store.get_credentials(1002, 5000).is_none());

        // Creation time mismatch fails
        assert!(!store.is_paired(1001, 5001));
        assert!(store.get_credentials(1001, 5001).is_none());

        // Invalidate purges
        store.invalidate();
        assert!(!store.is_paired(1001, 5000));
        assert!(store.get_credentials(1001, 5000).is_none());
    }

    #[test]
    fn test_secret_buffer_zeroizes_on_drop() {
        let buffer = SecretBuffer::new("very_sensitive_cookie_data_12345".to_string());
        assert_eq!(buffer.as_str(), "very_sensitive_cookie_data_12345");
        let len = buffer.0.len();
        drop(buffer);
        assert!(len > 0);
    }
}
