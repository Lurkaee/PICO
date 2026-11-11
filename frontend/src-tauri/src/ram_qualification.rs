//! Benchmark and qualification test measuring real memory footprints of supervised PICO Core
//! and native desktop components across lifecycle phases: Startup Peak, Stable Idle, and Paired Idle.

#[cfg(test)]
mod tests {
    use crate::resolve_backend_launch_config;
    use crate::supervisor::{self, OperatorSessionStore};
    use std::path::PathBuf;

    #[test]
    fn test_ram_qualification_process_tree_memory_sampling() {
        let mut config = resolve_backend_launch_config().expect("Config resolution must succeed");
        let test_port: u16 = 9005;
        config.bind_port = test_port;
        for i in 0..config.args.len() {
            if config.args[i] == "--port" && i + 1 < config.args.len() {
                config.args[i + 1] = test_port.to_string();
            }
        }

        let mut child = config
            .spawn()
            .expect("Spawning managed child into Job Object must succeed");

        // Perform mutual handshake and listener PID verification before sampling
        let verify_res = child.wait_and_verify(test_port, std::time::Duration::from_secs(30));
        assert!(
            verify_res.is_ok(),
            "wait_and_verify failed: {:?}",
            verify_res.err()
        );
        let pid = child.pid;

        // Native Win32 memory sampling without external PowerShell subprocess overhead
        #[repr(C)]
        struct ProcessMemoryCountersEx {
            cb: u32,
            page_fault_count: u32,
            peak_working_set_size: usize,
            working_set_size: usize,
            quota_peak_paged_pool_usage: usize,
            quota_paged_pool_usage: usize,
            quota_peak_non_paged_pool_usage: usize,
            quota_non_paged_pool_usage: usize,
            pagefile_usage: usize,
            peak_pagefile_usage: usize,
            private_usage: usize,
        }

        #[link(name = "kernel32")]
        extern "system" {
            fn K32GetProcessMemoryInfo(
                process: *mut std::ffi::c_void,
                ppsmem_counters: *mut ProcessMemoryCountersEx,
                cb: u32,
            ) -> i32;
        }

        let sample_memory = |target_pid: u32| -> (f64, f64) {
            let Ok(handle) = supervisor::open_process_handle(target_pid) else {
                return (0.0, 0.0);
            };
            let mut counters = std::mem::MaybeUninit::<ProcessMemoryCountersEx>::zeroed();
            let cb = std::mem::size_of::<ProcessMemoryCountersEx>() as u32;
            unsafe {
                (*counters.as_mut_ptr()).cb = cb;
                if K32GetProcessMemoryInfo(handle.raw(), counters.as_mut_ptr(), cb) == 0 {
                    return (0.0, 0.0);
                }
                let c = counters.assume_init();
                let mib = 1024.0 * 1024.0;
                (
                    (c.working_set_size as f64) / mib,
                    (c.private_usage as f64) / mib,
                )
            }
        };

        // Phase 1: Startup Peak Sample
        let (ws_startup, pm_startup) = sample_memory(pid);
        println!("[RAM Qualification] Python Core Startup Peak: WorkingSet={:.2} MB, PrivateBytes={:.2} MB", ws_startup, pm_startup);

        // Phase 2: Stable Idle (wait 2 seconds)
        std::thread::sleep(std::time::Duration::from_secs(2));
        let mut idle_ws_samples = Vec::new();
        let mut idle_pm_samples = Vec::new();
        for _ in 0..5 {
            let (ws, pm) = sample_memory(pid);
            idle_ws_samples.push(ws);
            idle_pm_samples.push(pm);
            std::thread::sleep(std::time::Duration::from_millis(200));
        }
        let avg_idle_ws: f64 = idle_ws_samples.iter().sum::<f64>() / (idle_ws_samples.len() as f64);
        let avg_idle_pm: f64 = idle_pm_samples.iter().sum::<f64>() / (idle_pm_samples.len() as f64);
        println!("[RAM Qualification] Python Core Stable Idle: WorkingSet={:.2} MB, PrivateBytes={:.2} MB", avg_idle_ws, avg_idle_pm);

        // Phase 3: Paired Idle (generate pairing code, pair operator, and measure)
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .to_path_buf();
        let python_bin = repo_root.join(".venv").join("Scripts").join("python.exe");

        let mint_output = std::process::Command::new(&python_bin)
            .current_dir(&repo_root)
            .args(&[
                "-c",
                "from pico.services.operator_session import OperatorSessionService; s = OperatorSessionService.get_instance(); c = s.generate_pairing_code(); print(c.code)",
            ])
            .output()
            .expect("Failed to mint pairing challenge via python");

        let valid_code = String::from_utf8_lossy(&mint_output.stdout)
            .trim()
            .to_string();
        assert!(
            mint_output.status.success() && valid_code.len() == 6,
            "Failed to mint pairing code (status={:?}, stdout='{}', stderr='{}')",
            mint_output.status,
            valid_code,
            String::from_utf8_lossy(&mint_output.stderr)
        );
        let session_store = OperatorSessionStore::new();

        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(async {
            let pair_res = supervisor::execute_native_pair_operator(
                test_port,
                &child.job_object,
                &child.launch_secret,
                Some(child.pid),
                &session_store,
                &valid_code,
            )
            .await;
            assert!(
                pair_res.is_ok(),
                "Pairing must succeed: {:?}",
                pair_res.err()
            );
        });

        std::thread::sleep(std::time::Duration::from_millis(500));
        let (ws_paired, pm_paired) = sample_memory(pid);
        println!("[RAM Qualification] Python Core Paired Idle: WorkingSet={:.2} MB, PrivateBytes={:.2} MB", ws_paired, pm_paired);

        // Verify Core memory characteristics
        assert!(ws_startup > 0.0, "Working set must be positive");
        assert!(pm_startup > 0.0, "Private bytes must be positive");
        assert!(avg_idle_ws > 0.0);

        // Terminate cleanly
        assert!(child.terminate().is_ok());
        std::thread::sleep(std::time::Duration::from_millis(500));
        assert!(!child.is_alive());
    }
}
