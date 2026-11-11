//! Win32 Named Mutex Single-Instance Guard (P11.2.4).
//!
//! Ensures that only one instance of the PICO Desktop Companion process runs per user session:
//! - Mutex Name: `Local\PicoDesktopSingleInstanceMutex`
//! - If an instance is already running (`ERROR_ALREADY_EXISTS`), provides detection to allow
//!   restoring and bringing the existing instance to focus before gracefully exiting.

#![allow(non_snake_case)]

use std::ffi::c_void;
use std::ptr;

type HANDLE = *mut c_void;
type BOOL = i32;
type DWORD = u32;
type LPCWSTR = *const u16;

const ERROR_ALREADY_EXISTS: DWORD = 183;
const MUTEX_NAME: &str = "Local\\PicoDesktopSingleInstanceMutex";

#[link(name = "kernel32")]
extern "system" {
    fn CreateMutexW(lpMutexAttributes: *mut c_void, bInitialOwner: BOOL, lpName: LPCWSTR)
        -> HANDLE;

    fn GetLastError() -> DWORD;

    fn CloseHandle(hObject: HANDLE) -> BOOL;
}

fn to_wide_null(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Represents the held single-instance named mutex.
pub struct SingleInstanceGuard {
    handle: HANDLE,
    is_primary: bool,
}

impl SingleInstanceGuard {
    /// Attempts to acquire the single-instance mutex.
    /// Returns `Ok(guard)` where `guard.is_primary()` is true if this is the first instance,
    /// or false if an instance is already running.
    pub fn acquire() -> Result<Self, String> {
        Self::acquire_named(MUTEX_NAME)
    }

    /// Parameterized acquisition for test isolation.
    pub fn acquire_named(name: &str) -> Result<Self, String> {
        let name_w = to_wide_null(name);
        let handle = unsafe { CreateMutexW(ptr::null_mut(), 1, name_w.as_ptr()) };

        if handle.is_null() {
            let err = unsafe { GetLastError() };
            return Err(format!("CreateMutexW failed with code {}", err));
        }

        let last_err = unsafe { GetLastError() };
        let is_primary = last_err != ERROR_ALREADY_EXISTS;

        Ok(Self { handle, is_primary })
    }

    /// Returns true if this process successfully became the single primary instance.
    pub fn is_primary(&self) -> bool {
        self.is_primary
    }
}

impl Drop for SingleInstanceGuard {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            unsafe { CloseHandle(self.handle) };
        }
    }
}

unsafe impl Send for SingleInstanceGuard {}
unsafe impl Sync for SingleInstanceGuard {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_single_instance_named_mutex_lifecycle() {
        let unique_name = format!("Local\\PicoTestMutex_{}", std::process::id());

        // 1. Primary acquire
        let g1 = SingleInstanceGuard::acquire_named(&unique_name).unwrap();
        assert!(g1.is_primary(), "First acquisition must be primary");

        // 2. Secondary acquire while g1 is held
        let g2 = SingleInstanceGuard::acquire_named(&unique_name).unwrap();
        assert!(
            !g2.is_primary(),
            "Second acquisition while held must be secondary (already exists)"
        );

        // 3. Drop primary
        drop(g1);
        drop(g2);

        // 4. Re-acquire after drop
        let g3 = SingleInstanceGuard::acquire_named(&unique_name).unwrap();
        assert!(
            g3.is_primary(),
            "Acquisition after drop must be primary again"
        );
    }
}
