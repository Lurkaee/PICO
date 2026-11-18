# PICO Whisper Native C-ABI Shim (Milestone P11.3.2)

## 1. Architectural Purpose

The PICO Whisper Shim (`pico_whisper_shim`) provides a minimal, robust, cross-language C-ABI boundary between PICO's Python runtime and `whisper.cpp`.

### Why a Native Shim is Required
Directly binding `whisper.cpp`'s internal C API via Python `ctypes` requires passing `struct whisper_full_params` by value. This struct contains dozens of nested fields, bitfields, function pointers, and compiler-dependent padding. Cross-language alignment mismatches between MSVC and Python `ctypes` can trigger memory corruption, crashes, or silent parameter corruption.

The PICO shim solves this by:
1. Compiling directly against `whisper.h` using MSVC C++.
2. Exposing an opaque context pointer (`PicoWhisperContext*`) and primitive scalar arguments.
3. Managing `whisper_full_params` internally with strictly enforced defaults (greedy sampling, 4 threads, English language, suppressed stdout logging).
4. Providing thread-safe access through a dedicated per-context mutex.

---

## 2. Upstream Pinned Provenance

- **Upstream Repository**: `https://github.com/ggml-org/whisper.cpp` (official fork/org of `ggerganov/whisper.cpp`)
- **Pinned Release Tag**: `v1.9.5`
- **Pinned Commit**: `d1be6fde11ac6e0407606b4e42fe72d34add8037`
- **License**: MIT License (Georgi Gerganov / ggml-org)

---

## 3. Exported C-ABI Interface

The dynamic library `pico_whisper_shim.dll` exports the following symbols (`extern "C" __declspec(dllexport)`):

```c
typedef enum {
    PICO_WHISPER_OK                   = 0,
    PICO_WHISPER_ERR_INVALID_ARG      = 1,
    PICO_WHISPER_ERR_INIT_FAILED      = 2,
    PICO_WHISPER_ERR_INFERENCE_FAILED = 3,
    PICO_WHISPER_ERR_BUFFER_TOO_SMALL = 4,
    PICO_WHISPER_ERR_NULL_CONTEXT     = 5,
} PicoWhisperStatus;

typedef struct PicoWhisperContext PicoWhisperContext;

const char *pico_whisper_version(void);
const char *pico_whisper_system_info(void);

PicoWhisperStatus pico_whisper_init(
    const char *model_path,
    PicoWhisperContext **out_ctx
);

PicoWhisperStatus pico_whisper_transcribe(
    PicoWhisperContext *ctx,
    const float *samples,
    int n_samples,
    int n_threads,
    char *out_text_buffer,
    size_t buffer_size,
    size_t *out_text_len
);

void pico_whisper_free(PicoWhisperContext *ctx);
```

### Ownership & Resource Rules
1. **Context Ownership**: The caller owns the pointer returned by `pico_whisper_init()` and MUST call `pico_whisper_free(ctx)` when finished.
2. **Buffer Ownership**: The caller owns `samples` and `out_text_buffer`. The shim does not retain pointers to caller memory after `pico_whisper_transcribe()` returns.
3. **Thread Safety**: `pico_whisper_transcribe()` protects concurrent invocations on the same context using a `std::mutex`.
4. **Hardware Invariant**: GPU acceleration is explicitly disabled (`use_gpu = false`), strictly executing on CPU using AVX2 SIMD instructions.

---

## 4. Build & Staging Instructions

To build the shim and stage the native runtime DLLs:

```powershell
# From repository root:
.\tools\whisper_shim\build_shim.ps1
```

The script builds:
- `ggml-base.dll`
- `ggml-cpu.dll` (AVX2 CPU backend)
- `ggml.dll`
- `whisper.dll`
- `pico_whisper_shim.dll`

All DLLs are automatically staged into `%LOCALAPPDATA%\PICO\runtime\bin\`.
Model weights are staged into `%LOCALAPPDATA%\PICO\models\whisper\`.
Zero native binaries or model weights are committed to the Git repository.
