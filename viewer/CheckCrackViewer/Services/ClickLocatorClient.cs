using System;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;

namespace CheckCrackViewer.Services;

/// <summary>tools/click_locator.py 의 응답 (필드는 그 파일 docstring 참고). error 값이 null이면 "측정 불가"이지
/// 0이 아니다.</summary>
public sealed class ClickLocateResult
{
    [JsonPropertyName("id")] public int? Id { get; set; }
    [JsonPropertyName("error")] public string? Error { get; set; }
    [JsonPropertyName("image_id")] public string ImageId { get; set; } = "";
    [JsonPropertyName("raw_width")] public int RawWidth { get; set; }
    [JsonPropertyName("raw_height")] public int RawHeight { get; set; }
    [JsonPropertyName("flat")] public ClickPoint? Flat { get; set; }
    [JsonPropertyName("depth")] public ClickPoint? Depth { get; set; }
    [JsonPropertyName("flat_error")] public ClickErrorValue? FlatError { get; set; }
    [JsonPropertyName("depth_error")] public ClickErrorValue? DepthError { get; set; }
    [JsonPropertyName("px_per_m")] public double? PxPerM { get; set; }
    [JsonPropertyName("calibrated")] public bool Calibrated { get; set; }
}

public sealed class ClickPoint
{
    [JsonPropertyName("x")] public double X { get; set; }
    [JsonPropertyName("y")] public double Y { get; set; }
    [JsonPropertyName("residual_px")] public double? ResidualPx { get; set; }
}

public sealed class ClickErrorValue
{
    [JsonPropertyName("offset_px")] public double OffsetPx { get; set; }
    [JsonPropertyName("dx")] public double Dx { get; set; }
    [JsonPropertyName("dy")] public double Dy { get; set; }
    [JsonPropertyName("ncc")] public double Ncc { get; set; }
}

/// <summary>2026-09-26 (사용자 요구, "스티칭에서 선택한 곳과 원본이미지 위치 표시 오차"): 스티칭 클릭 지점이
/// 원본 사진의 어디인지를 깊이로 정확히 구하고, 그 마커 위치 오차를 실측하는 tools/click_locator.py를
/// 상주 프로세스로 띄워 둔다(매 클릭마다 새로 띄우면 깊이 맵/모자이크 로딩 때문에 느리다: 시작 약 1초,
/// 이후 클릭당 약 0.3초). 결과 보기에서 facade가 바뀌면 다른 프로세스로 교체한다. 실패하면 null을 돌려주고,
/// 호출부는 기존 평면 호모그래피 마커를 그대로 유지한다(마커를 못 옮긴다고 클릭 동작이 깨지면 안 됨).</summary>
public sealed class ClickLocatorClient : IDisposable
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private Process? _process;
    private string? _key;
    private int _nextId;

    public async Task<ClickLocateResult?> LocateAsync(
        string rootPath, string outputDir, string facadeId, int x, int y, CancellationToken ct)
    {
        await _gate.WaitAsync(ct);
        try
        {
            if (!await EnsureStartedAsync(rootPath, outputDir, facadeId, ct))
                return null;

            var id = ++_nextId;
            var request = JsonSerializer.Serialize(new { id, x, y });
            await _process!.StandardInput.WriteLineAsync(request.AsMemory(), ct);
            await _process.StandardInput.FlushAsync(ct);

            while (true)
            {
                var line = await ReadLineWithTimeoutAsync(_process.StandardOutput, TimeSpan.FromSeconds(60), ct);
                if (line == null)
                {
                    StopProcess(); // died or hung -- next call starts a fresh one
                    return null;
                }
                ClickLocateResult? result;
                try { result = JsonSerializer.Deserialize<ClickLocateResult>(line); }
                catch (JsonException) { continue; } // stray non-JSON line
                if (result?.Id == id)
                    return result;
            }
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception ex)
        {
            Debug.WriteLine($"ClickLocator 실패: {ex}");
            StopProcess();
            return null;
        }
        finally
        {
            _gate.Release();
        }
    }

    private async Task<bool> EnsureStartedAsync(string rootPath, string outputDir, string facadeId, CancellationToken ct)
    {
        var key = $"{outputDir}|{facadeId}";
        if (_process is { HasExited: false } && _key == key)
            return true;
        StopProcess();

        var script = Path.Combine(rootPath, "tools", "click_locator.py");
        if (!File.Exists(script))
            return false;

        var psi = new ProcessStartInfo
        {
            FileName = PythonEnvironment.DiscoverPythonExe(),
            WorkingDirectory = rootPath,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardInputEncoding = new System.Text.UTF8Encoding(false),
        };
        psi.ArgumentList.Add(script);
        psi.ArgumentList.Add(outputDir);
        psi.ArgumentList.Add(facadeId);

        var process = new Process { StartInfo = psi };
        process.Start();
        ChildProcessRegistry.Register(process);
        _ = process.StandardError.ReadToEndAsync(); // drain so a chatty stderr can never block the child

        var ready = await ReadLineWithTimeoutAsync(process.StandardOutput, TimeSpan.FromSeconds(90), ct);
        if (ready == null || !ready.Contains("\"ready\": true", StringComparison.Ordinal))
        {
            try { if (!process.HasExited) process.Kill(entireProcessTree: true); } catch { }
            ChildProcessRegistry.Unregister(process);
            return false;
        }
        _process = process;
        _key = key;
        return true;
    }

    private static async Task<string?> ReadLineWithTimeoutAsync(StreamReader reader, TimeSpan timeout, CancellationToken ct)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(timeout);
        try
        {
            return await reader.ReadLineAsync(cts.Token);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            return null; // timeout
        }
    }

    private void StopProcess()
    {
        var process = _process;
        _process = null;
        _key = null;
        if (process == null)
            return;
        try
        {
            if (!process.HasExited)
                process.Kill(entireProcessTree: true);
        }
        catch { /* already gone */ }
        ChildProcessRegistry.Unregister(process);
        process.Dispose();
    }

    public void Dispose() => StopProcess();
}
