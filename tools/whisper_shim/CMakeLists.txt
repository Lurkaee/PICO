cmake_minimum_required(VERSION 3.14)
project(pico_whisper_shim C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

# Expect WHISPER_DIR pointing to whisper.cpp source root
set(WHISPER_DIR "" CACHE PATH "Path to whisper.cpp source tree")
if (NOT WHISPER_DIR)
    message(FATAL_ERROR "Please specify -DWHISPER_DIR=/path/to/whisper.cpp")
endif()

add_library(pico_whisper_shim SHARED pico_whisper_shim.cpp)
target_include_directories(pico_whisper_shim PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${WHISPER_DIR}/include
    ${WHISPER_DIR}/ggml/include
)
target_compile_definitions(pico_whisper_shim PRIVATE PICO_WHISPER_SHIM_EXPORTS)
target_link_libraries(pico_whisper_shim PRIVATE whisper)
