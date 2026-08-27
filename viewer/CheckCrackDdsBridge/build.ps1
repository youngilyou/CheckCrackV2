# One-command build for CheckCrackDdsBridge. Requires
# tools\Get-FastDdsGenModule.ps1 (repo root) to have been run first.
#
# The /p:VCToolsVersion override is REQUIRED on this machine family -- see
# FacadePreviewer/FacadeDdsBridge/build.ps1's own comment for the full story (same vendored
# FastDDS SDK, same prebuilt-lib/MSVC-toolset mismatch).
param(
    [string]$Config = "Debug"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

cmake -S $ScriptDir -B (Join-Path $ScriptDir "build") -G "Visual Studio 17 2022" -A x64
cmake --build (Join-Path $ScriptDir "build") --config $Config -- /p:VCToolsVersion=14.44.35207
