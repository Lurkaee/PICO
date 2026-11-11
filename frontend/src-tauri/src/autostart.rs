//! Windows Native Auto-Start Management via HKCU Run Key (P11.2.4).
//!
//! Provides user-level (non-elevated) registry persistence for optional launch on Windows startup:
//! - Registry root: `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`
//! - Value name: `"PICO"`
//! - Value data: `"<executable_path>"` (escaped and quoted)
//! - Never requests administrator privileges or writes to HKLM.
//! - Safe idempotent enable, disable, and status query operations.

#![allow(non_snake_case)]

use std::ffi::c_void;
use std::path::Path;
use std::ptr;

type HKEY = *mut c_void;
type LSTATUS = i32;
type DWORD = u32;
type LPCWSTR = *const u16;
type LPBYTE = *mut u8;

const HKEY_CURRENT_USER: HKEY = 0x80000001u32 as usize as HKEY;
const ERROR_SUCCESS: LSTATUS = 0;
const ERROR_FILE_NOT_FOUND: LSTATUS = 2;

const KEY_READ: DWORD = 0x20019;
const KEY_SET_VALUE: DWORD = 0x0002;
const REG_SZ: DWORD = 1;

const RUN_KEY_PATH: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\Run";
const RUN_VALUE_NAME: &str = "PICO";

#[link(name = "advapi32")]
extern "system" {
    fn RegOpenKeyExW(
        hKey: HKEY,
        lpSubKey: LPCWSTR,
        ulOptions: DWORD,
        samDesired: DWORD,
        phkResult: *mut HKEY,
    ) -> LSTATUS;

    fn RegCreateKeyExW(
        hKey: HKEY,
        lpSubKey: LPCWSTR,
        Reserved: DWORD,
        lpClass: LPCWSTR,
        dwOptions: DWORD,
        samDesired: DWORD,
        lpSecurityAttributes: *mut c_void,
        phkResult: *mut HKEY,
        lpdwDisposition: *mut DWORD,
    ) -> LSTATUS;

    fn RegQueryValueExW(
        hKey: HKEY,
        lpValueName: LPCWSTR,
        lpReserved: *mut DWORD,
        lpType: *mut DWORD,
        lpData: LPBYTE,
        lpcbData: *mut DWORD,
    ) -> LSTATUS;

    fn RegSetValueExW(
        hKey: HKEY,
        lpValueName: LPCWSTR,
        Reserved: DWORD,
        dwType: DWORD,
        lpData: *const u8,
        cbData: DWORD,
    ) -> LSTATUS;

    fn RegDeleteValueW(hKey: HKEY, lpValueName: LPCWSTR) -> LSTATUS;

    #[cfg(test)]
    fn RegDeleteKeyW(hKey: HKEY, lpSubKey: LPCWSTR) -> LSTATUS;

    fn RegCloseKey(hKey: HKEY) -> LSTATUS;
}

fn to_wide_null(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Helper struct wrapping an open HKEY to guarantee RegCloseKey on drop.
struct SafeHKey(HKEY);

impl Drop for SafeHKey {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { RegCloseKey(self.0) };
        }
    }
}

/// Auto-start registration status payload.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct AutoStartStatus {
    pub enabled: bool,
    pub executable_path: Option<String>,
    pub matches_current_exe: bool,
}

/// Queries whether PICO is configured to launch at Windows login in HKCU Run.
pub fn is_autostart_enabled() -> Result<AutoStartStatus, String> {
    is_autostart_enabled_for_key(RUN_KEY_PATH, RUN_VALUE_NAME)
}

/// Enables auto-start by setting the HKCU Run value to the current executable path.
pub fn enable_autostart() -> Result<AutoStartStatus, String> {
    let current_exe = std::env::current_exe()
        .map_err(|e| format!("Failed to determine current executable path: {}", e))?;
    enable_autostart_for_key(RUN_KEY_PATH, RUN_VALUE_NAME, &current_exe)
}

/// Disables auto-start by deleting the HKCU Run value.
pub fn disable_autostart() -> Result<AutoStartStatus, String> {
    disable_autostart_for_key(RUN_KEY_PATH, RUN_VALUE_NAME)
}

/// Parameterized query allowing test isolation with custom registry subkeys.
pub fn is_autostart_enabled_for_key(
    subkey: &str,
    value_name: &str,
) -> Result<AutoStartStatus, String> {
    let subkey_w = to_wide_null(subkey);
    let value_name_w = to_wide_null(value_name);
    let mut hkey: HKEY = ptr::null_mut();

    let open_status =
        unsafe { RegOpenKeyExW(HKEY_CURRENT_USER, subkey_w.as_ptr(), 0, KEY_READ, &mut hkey) };

    if open_status == ERROR_FILE_NOT_FOUND {
        return Ok(AutoStartStatus {
            enabled: false,
            executable_path: None,
            matches_current_exe: false,
        });
    }

    if open_status != ERROR_SUCCESS {
        return Err(format!("RegOpenKeyExW failed with code {}", open_status));
    }

    let _safe_key = SafeHKey(hkey);

    let mut val_type: DWORD = 0;
    let mut data_len: DWORD = 0;

    let query_size_status = unsafe {
        RegQueryValueExW(
            hkey,
            value_name_w.as_ptr(),
            ptr::null_mut(),
            &mut val_type,
            ptr::null_mut(),
            &mut data_len,
        )
    };

    if query_size_status == ERROR_FILE_NOT_FOUND {
        return Ok(AutoStartStatus {
            enabled: false,
            executable_path: None,
            matches_current_exe: false,
        });
    }

    if query_size_status != ERROR_SUCCESS {
        return Err(format!(
            "RegQueryValueExW (size) failed with code {}",
            query_size_status
        ));
    }

    if val_type != REG_SZ || data_len == 0 {
        return Ok(AutoStartStatus {
            enabled: true,
            executable_path: None,
            matches_current_exe: false,
        });
    }

    let mut buf: Vec<u8> = vec![0u8; data_len as usize];
    let query_status = unsafe {
        RegQueryValueExW(
            hkey,
            value_name_w.as_ptr(),
            ptr::null_mut(),
            &mut val_type,
            buf.as_mut_ptr(),
            &mut data_len,
        )
    };

    if query_status != ERROR_SUCCESS {
        return Err(format!(
            "RegQueryValueExW (data) failed with code {}",
            query_status
        ));
    }

    let u16_slice = unsafe {
        std::slice::from_raw_parts(
            buf.as_ptr() as *const u16,
            (data_len as usize) / std::mem::size_of::<u16>(),
        )
    };

    let mut val_str = String::from_utf16_lossy(u16_slice);
    if let Some(null_pos) = val_str.find('\0') {
        val_str.truncate(null_pos);
    }

    let clean_path = val_str.trim().trim_matches('"').to_string();
    let current_exe = std::env::current_exe().ok();
    let matches = match current_exe {
        Some(exe) => {
            let exe_str = exe.to_string_lossy().to_string();
            clean_path.eq_ignore_ascii_case(&exe_str)
        }
        None => false,
    };

    Ok(AutoStartStatus {
        enabled: true,
        executable_path: Some(val_str),
        matches_current_exe: matches,
    })
}

/// Parameterized setter allowing test isolation.
pub fn enable_autostart_for_key(
    subkey: &str,
    value_name: &str,
    target_exe: &Path,
) -> Result<AutoStartStatus, String> {
    let subkey_w = to_wide_null(subkey);
    let value_name_w = to_wide_null(value_name);
    let mut hkey: HKEY = ptr::null_mut();

    let open_status = unsafe {
        RegCreateKeyExW(
            HKEY_CURRENT_USER,
            subkey_w.as_ptr(),
            0,
            ptr::null(),
            0,
            KEY_SET_VALUE | KEY_READ,
            ptr::null_mut(),
            &mut hkey,
            ptr::null_mut(),
        )
    };

    if open_status != ERROR_SUCCESS {
        return Err(format!(
            "RegCreateKeyExW for write failed with code {}",
            open_status
        ));
    }

    let _safe_key = SafeHKey(hkey);

    // Quote the path to handle spaces in Program Files cleanly
    let command_line = format!("\"{}\"", target_exe.display());
    let command_line_w = to_wide_null(&command_line);
    let bytes_len = (command_line_w.len() * std::mem::size_of::<u16>()) as DWORD;

    let set_status = unsafe {
        RegSetValueExW(
            hkey,
            value_name_w.as_ptr(),
            0,
            REG_SZ,
            command_line_w.as_ptr() as *const u8,
            bytes_len,
        )
    };

    if set_status != ERROR_SUCCESS {
        return Err(format!("RegSetValueExW failed with code {}", set_status));
    }

    is_autostart_enabled_for_key(subkey, value_name)
}

/// Parameterized deleter allowing test isolation.
pub fn disable_autostart_for_key(
    subkey: &str,
    value_name: &str,
) -> Result<AutoStartStatus, String> {
    let subkey_w = to_wide_null(subkey);
    let value_name_w = to_wide_null(value_name);
    let mut hkey: HKEY = ptr::null_mut();

    let open_status = unsafe {
        RegOpenKeyExW(
            HKEY_CURRENT_USER,
            subkey_w.as_ptr(),
            0,
            KEY_SET_VALUE | KEY_READ,
            &mut hkey,
        )
    };

    if open_status == ERROR_FILE_NOT_FOUND {
        return Ok(AutoStartStatus {
            enabled: false,
            executable_path: None,
            matches_current_exe: false,
        });
    }

    if open_status != ERROR_SUCCESS {
        return Err(format!(
            "RegOpenKeyExW for delete failed with code {}",
            open_status
        ));
    }

    let _safe_key = SafeHKey(hkey);

    let del_status = unsafe { RegDeleteValueW(hkey, value_name_w.as_ptr()) };

    if del_status != ERROR_SUCCESS && del_status != ERROR_FILE_NOT_FOUND {
        return Err(format!("RegDeleteValueW failed with code {}", del_status));
    }

    Ok(AutoStartStatus {
        enabled: false,
        executable_path: None,
        matches_current_exe: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn test_autostart_status_default_disabled_or_valid() {
        let status = is_autostart_enabled();
        assert!(
            status.is_ok(),
            "Querying HKCU Run key should not error: {:?}",
            status.err()
        );
    }

    #[test]
    fn test_autostart_lifecycle_in_test_key() {
        let test_subkey = format!("Software\\PICO_Test_Autostart_{}", std::process::id());
        const TEST_VAL: &str = "PICO_Test_Ephemeral_Val";
        let fake_exe = PathBuf::from(r"C:\Program Files\PICO\PICO.exe");

        // 1. Initial cleanup & missing-key query
        let _ = disable_autostart_for_key(&test_subkey, TEST_VAL);
        let s0 = is_autostart_enabled_for_key(&test_subkey, TEST_VAL).unwrap();
        assert!(!s0.enabled);
        assert!(s0.executable_path.is_none());

        // 2. Enable (must create subkey when absent)
        let s1 = enable_autostart_for_key(&test_subkey, TEST_VAL, &fake_exe).unwrap();
        assert!(s1.enabled);
        assert_eq!(
            s1.executable_path.unwrap(),
            r#""C:\Program Files\PICO\PICO.exe""#
        );

        // 3. Query
        let s2 = is_autostart_enabled_for_key(&test_subkey, TEST_VAL).unwrap();
        assert!(s2.enabled);
        assert_eq!(
            s2.executable_path.unwrap(),
            r#""C:\Program Files\PICO\PICO.exe""#
        );

        // 4. Disable
        let s3 = disable_autostart_for_key(&test_subkey, TEST_VAL).unwrap();
        assert!(!s3.enabled);
        assert!(s3.executable_path.is_none());

        // 5. Query after disable
        let s4 = is_autostart_enabled_for_key(&test_subkey, TEST_VAL).unwrap();
        assert!(!s4.enabled);
        assert!(s4.executable_path.is_none());

        // Clean up temporary registry key
        let subkey_w = to_wide_null(&test_subkey);
        unsafe { RegDeleteKeyW(HKEY_CURRENT_USER, subkey_w.as_ptr()) };
    }
}
