cmake_minimum_required(VERSION 3.14)
project(pico_whisper_shim C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

# Direct all runtime DLL outputs to a common bin/ directory
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/bin")

# Expect WHISPER_DIR pointing to whisper.cpp source root
set(WHISPER_DIR "" CACHE PATH "Path to whisper.cpp source tree")
if (NOT WHISPER_DIR)
    message(FATAL_ERROR "Please specify -DWHISPER_DIR=/path/to/whisper.cpp")
endif()

# Configure upstream whisper.cpp dependency if target does not already exist
if (NOT TARGET whisper)
    # CPU-only configuration invariants (ADR 0005)
    set(WHISPER_BUILD_EXAMPLES OFF CACHE BOOL "Disable whisper examples" FORCE)
    set(WHISPER_BUILD_TESTS OFF CACHE BOOL "Disable whisper tests" FORCE)
    set(WHISPER_BUILD_SERVER OFF CACHE BOOL "Disable whisper server" FORCE)
    set(BUILD_SHARED_LIBS ON CACHE BOOL "Build shared libraries" FORCE)
    set(GGML_CUDA OFF CACHE BOOL "Disable CUDA GPU acceleration" FORCE)
    set(GGML_VULKAN OFF CACHE BOOL "Disable Vulkan acceleration" FORCE)
    set(GGML_OPENCL OFF CACHE BOOL "Disable OpenCL acceleration" FORCE)
    set(GGML_AVX2 ON CACHE BOOL "Enable AVX2 CPU acceleration" FORCE)
    set(GGML_FMA ON CACHE BOOL "Enable FMA CPU acceleration" FORCE)

    add_subdirectory("${WHISPER_DIR}" "${CMAKE_CURRENT_BINARY_DIR}/whisper_build" EXCLUDE_FROM_ALL)
endif()

add_library(pico_whisper_shim SHARED pico_whisper_shim.cpp)
target_include_directories(pico_whisper_shim PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${WHISPER_DIR}/include
    ${WHISPER_DIR}/ggml/include
)
target_compile_definitions(pico_whisper_shim PRIVATE PICO_WHISPER_SHIM_EXPORTS)
target_link_libraries(pico_whisper_shim PRIVATE whisper)
