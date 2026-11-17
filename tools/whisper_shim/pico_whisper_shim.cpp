/**
 * PICO Whisper C-ABI Shim Implementation (Milestone P11.3.2)
 */

#include "pico_whisper_shim.h"
#include "whisper.h"

#include <string>
#include <vector>
#include <cstring>
#include <mutex>
#include <algorithm>

struct PicoWhisperContext {
    whisper_context *ctx = nullptr;
    std::mutex mtx;
};

extern "C" {

PICO_WHISPER_API const char *pico_whisper_version(void) {
    return whisper_version();
}

PICO_WHISPER_API const char *pico_whisper_system_info(void) {
    return whisper_print_system_info();
}

PICO_WHISPER_API PicoWhisperStatus pico_whisper_init(
    const char *model_path,
    PicoWhisperContext **out_ctx
) {
    if (!model_path || !out_ctx) {
        return PICO_WHISPER_ERR_INVALID_ARG;
    }
    *out_ctx = nullptr;

    whisper_context_params cparams = whisper_context_default_params();
    cparams.use_gpu = false;    // Non-negotiable invariant: CPU-only execution
    cparams.flash_attn = false; // CPU fallback

    whisper_context *wctx = whisper_init_from_file_with_params(model_path, cparams);
    if (!wctx) {
        return PICO_WHISPER_ERR_INIT_FAILED;
    }

    auto *pctx = new (std::nothrow) PicoWhisperContext();
    if (!pctx) {
        whisper_free(wctx);
        return PICO_WHISPER_ERR_INIT_FAILED;
    }

    pctx->ctx = wctx;
    *out_ctx = pctx;
    return PICO_WHISPER_OK;
}

PICO_WHISPER_API PicoWhisperStatus pico_whisper_transcribe(
    PicoWhisperContext *ctx,
    const float *samples,
    int n_samples,
    int n_threads,
    char *out_text_buffer,
    size_t buffer_size,
    size_t *out_text_len
) {
    if (!ctx || !ctx->ctx) {
        return PICO_WHISPER_ERR_NULL_CONTEXT;
    }
    if (!samples || n_samples <= 0 || !out_text_buffer || buffer_size == 0) {
        return PICO_WHISPER_ERR_INVALID_ARG;
    }

    std::lock_guard<std::mutex> lock(ctx->mtx);

    whisper_full_params wparams = whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
    wparams.n_threads = n_threads > 0 ? n_threads : 4;
    wparams.print_progress = false;
    wparams.print_special = false;
    wparams.print_realtime = false;
    wparams.print_timestamps = false;
    wparams.translate = false;
    wparams.language = "en";
    wparams.no_context = true;
    wparams.single_segment = false;

    int ret = whisper_full(ctx->ctx, wparams, samples, n_samples);
    if (ret != 0) {
        return PICO_WHISPER_ERR_INFERENCE_FAILED;
    }

    std::string full_text;
    const int n_segments = whisper_full_n_segments(ctx->ctx);
    for (int i = 0; i < n_segments; ++i) {
        const char *seg_text = whisper_full_get_segment_text(ctx->ctx, i);
        if (seg_text) {
            full_text += seg_text;
        }
    }

    // Trim leading whitespace
    size_t start_pos = full_text.find_first_not_of(" \t\r\n");
    if (start_pos != std::string::npos && start_pos > 0) {
        full_text = full_text.substr(start_pos);
    }
    // Trim trailing whitespace
    size_t end_pos = full_text.find_last_not_of(" \t\r\n");
    if (end_pos != std::string::npos && end_pos + 1 < full_text.size()) {
        full_text = full_text.substr(0, end_pos + 1);
    }

    if (full_text.size() + 1 > buffer_size) {
        if (out_text_len) {
            *out_text_len = full_text.size();
        }
        return PICO_WHISPER_ERR_BUFFER_TOO_SMALL;
    }

    std::memcpy(out_text_buffer, full_text.c_str(), full_text.size() + 1);
    if (out_text_len) {
        *out_text_len = full_text.size();
    }

    return PICO_WHISPER_OK;
}

PICO_WHISPER_API void pico_whisper_free(PicoWhisperContext *ctx) {
    if (ctx) {
        std::lock_guard<std::mutex> lock(ctx->mtx);
        if (ctx->ctx) {
            whisper_free(ctx->ctx);
            ctx->ctx = nullptr;
        }
        delete ctx;
    }
}

} // extern "C"
