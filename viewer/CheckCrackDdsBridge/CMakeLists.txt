cmake_minimum_required(VERSION 3.20)

# Must be set before project() -- see FacadePreviewer/FacadeDdsBridge/CMakeLists.txt's own
# comment on CMP0091 for why (without it CMAKE_MSVC_RUNTIME_LIBRARY below is silently ignored).
cmake_policy(SET CMP0091 NEW)

project(CheckCrackDdsBridge CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# Always the Release CRT, even in a Debug config build -- the vendored fastdds-3.6.dll/
# fastcdr-2.3.dll (tools/Module/FastDDSGen/FastDDS/bin) are Release-only builds. See
# FacadePreviewer/FacadeDdsBridge/CMakeLists.txt's own comment for the full story of the heap
# corruption bug this avoids (a real crash already hit and fixed once in that sibling project
# with the exact same vendored SDK).
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")

if(MSVC)
  add_compile_options(/utf-8)
endif()

# FastDDS SDK from youngilyou/Gen_IDL_DDS's ExtraModule -- installed locally via
# tools/Get-FastDdsGenModule.ps1 (own independent copy, not shared with FacadePreviewer's --
# see that script's own header comment). Override with -DFASTDDS_ROOT=... for local testing.
if(NOT DEFINED FASTDDS_ROOT)
  set(FASTDDS_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../tools/Module/FastDDSGen/FastDDS")
endif()
# Reference the vendored files directly (no find_package -- this vendored FASTDDS_ROOT ships
# raw bin/include/lib only, no CMake package config files; see FacadePreviewer's own
# CMakeLists.txt comment for why find_package() would silently fall through to a
# machine-specific global install instead, exactly what vendoring exists to avoid).

set(GENERATED_DIR "${CMAKE_CURRENT_SOURCE_DIR}/idl/generated/facade_analysis_msgs/msg")

add_library(CheckCrackDdsBridge SHARED
    src/DdsQosHelpers.h
    src/DdsQosHelpers.cpp
    src/AnalysisBridge.h
    src/AnalysisBridge.cpp
    src/CheckCrackDdsBridge.h
    src/CheckCrackDdsBridge.cpp
    ${GENERATED_DIR}/FacadeAnalysisPubSubTypes.cxx
    ${GENERATED_DIR}/FacadeAnalysisCdrAux.ipp
    ${GENERATED_DIR}/FacadeAnalysisTypeObjectSupport.cxx
)
target_include_directories(CheckCrackDdsBridge PRIVATE src ${GENERATED_DIR} "${FASTDDS_ROOT}/include")
target_link_directories(CheckCrackDdsBridge PRIVATE "${FASTDDS_ROOT}/lib")
target_link_libraries(CheckCrackDdsBridge PRIVATE
    "${FASTDDS_ROOT}/lib/fastdds-3.6.lib"
    "${FASTDDS_ROOT}/lib/fastcdr-2.3.lib"
    "${FASTDDS_ROOT}/lib/foonathan_memory-0.7.4.lib"
)
target_compile_definitions(CheckCrackDdsBridge PRIVATE CRACKVIEWER_DDS_BRIDGE_EXPORTS
    COMPILEDWITHC11 _USE_MATH_DEFINES NOMINMAX _HAS_STD_BYTE=0
    EPROSIMA_ALL_DYN_LINK
)

# Vendored FastDDS SDK's own runtime DLLs next to the built DLL, same convention as
# FacadePreviewer/FacadeDdsBridge/CMakeLists.txt.
file(GLOB FASTDDS_RUNTIME_DLLS "${FASTDDS_ROOT}/bin/fastdds-*.dll" "${FASTDDS_ROOT}/bin/fastcdr-*.dll"
    "${FASTDDS_ROOT}/bin/foonathan_memory-*.dll")
list(FILTER FASTDDS_RUNTIME_DLLS EXCLUDE REGEX "-dbg\\.dll$")
foreach(dll ${FASTDDS_RUNTIME_DLLS})
    add_custom_command(TARGET CheckCrackDdsBridge POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different "${dll}" "$<TARGET_FILE_DIR:CheckCrackDdsBridge>"
    )
endforeach()
