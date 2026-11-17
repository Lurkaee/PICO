/**
 * PICO Whisper C-ABI Shim (Milestone P11.3.2)
 *
 * Provides a minimal, robust, cross-language C-ABI boundary for whisper.cpp.
 * Decouples Python ctypes from the fragile internal struct layout of whisper_full_params.
 *
 * Invariants:
 * 1. CPU-only: GPU acceleration is explicitly disabled (use_gpu = false).
 * 2. In-memory: accepts float32 PCM samples [-1.0, 1.0] at 16,000 Hz mono from RAM.
 * 3. Bounded threads: inference is pinned to specified thread count (default: 4 threads).
 * 4. Deterministic cleanup: frees all native context memory on pico_whisper_free().
 */

#ifndef PICO_WHISPER_SHIM_H
#define PICO_WHISPER_SHIM_H

#include <stdint.h>
#include <stddef.h>

#ifdef _WIN32
  #ifdef PICO_WHISPER_SHIM_EXPORTS
    #define PICO_WHISPER_API __declspec(dllexport)
  #else
    #define PICO_WHISPER_API __declspec(dllimport)
  #endif
#else
  #define PICO_WHISPER_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

/** Status codes returned by the PICO Whisper shim */
typedef enum {
    PICO_WHISPER_OK                   = 0,
    PICO_WHISPER_ERR_INVALID_ARG      = 1,
    PICO_WHISPER_ERR_INIT_FAILED      = 2,
    PICO_WHISPER_ERR_INFERENCE_FAILED = 3,
    PICO_WHISPER_ERR_BUFFER_TOO_SMALL = 4,
    PICO_WHISPER_ERR_NULL_CONTEXT     = 5,
} PicoWhisperStatus;

/** Opaque handle to whisper context */
typedef struct PicoWhisperContext PicoWhisperContext;

/**
 * Return the underlying whisper.cpp version string.
 */
PICO_WHISPER_API const char *pico_whisper_version(void);

/**
 * Return system information string (AVX, AVX2, OpenMP, etc.).
 */
PICO_WHISPER_API const char *pico_whisper_system_info(void);

/**
 * Initialize whisper context from a verified local model path.
 *
 * @param model_path Path to the GGML model weight file (UTF-8).
 * @param out_ctx Pointer to receive the allocated opaque context handle.
 * @return PICO_WHISPER_OK on success, or an error status code.
 */
PICO_WHISPER_API PicoWhisperStatus pico_whisper_init(
    const char *model_path,
    PicoWhisperContext **out_ctx
);

/**
 * Transcribe in-memory 16 kHz 32-bit floating-point mono PCM audio samples [-1.0, 1.0].
 *
 * @param ctx Valid opaque context pointer.
 * @param samples Array of normalized 32-bit float audio samples.
 * @param n_samples Number of audio samples in the array.
 * @param n_threads Number of CPU threads to dedicate to inference (bounded to >= 1).
 * @param out_text_buffer Caller-allocated buffer to receive the null-terminated UTF-8 transcript.
 * @param buffer_size Size of out_text_buffer in bytes.
 * @param out_text_len Optional pointer to receive the transcript length in bytes (excluding null terminator).
 * @return PICO_WHISPER_OK on success, or an error status code.
 */
PICO_WHISPER_API PicoWhisperStatus pico_whisper_transcribe(
    PicoWhisperContext *ctx,
    const float *samples,
    int n_samples,
    int n_threads,
    char *out_text_buffer,
    size_t buffer_size,
    size_t *out_text_len
);

/**
 * Deterministically release all native memory and resources owned by the context.
 *
 * @param ctx Valid opaque context pointer (safe to call with NULL).
 */
PICO_WHISPER_API void pico_whisper_free(PicoWhisperContext *ctx);

#ifdef __cplusplus
}
#endif

#endif /* PICO_WHISPER_SHIM_H */
