using System;
using System.IO;
using System.Text.Json;
using System.Windows.Threading;
using CheckCrackViewer.Models;

namespace CheckCrackViewer.Services;

/// <summary>Polls logs/pipeline.log for new lines and parses each as a
/// PipelineLogEntry. Uses a DispatcherTimer (not System.Timers.Timer) so
/// every callback already runs on the UI thread — the caller can touch
/// ObservableCollections directly, no manual Dispatcher.Invoke needed.</summary>
public class PipelineLogTailer : IDisposable
{
    private readonly string _logPath;
    private readonly DispatcherTimer _timer;
    private long _lastPosition;
    private int _lineNumber;

    public event Action<PipelineLogEntry>? EntryParsed;
    public event Action<string>? RawLineFailed;

    public PipelineLogTailer(string logPath, TimeSpan? interval = null)
    {
        _logPath = logPath;
        _timer = new DispatcherTimer { Interval = interval ?? TimeSpan.FromMilliseconds(750) };
        _timer.Tick += (_, _) => Poll();
    }

    /// <summary>Starts at the current end of the file so the "LIVE LOG" panel only
    /// shows events that happen from now on — replaying the full (often
    /// thousands-of-lines) history on every launch made old runs look like
    /// they were actively in progress right now, which is exactly what this
    /// panel's label promises it isn't.</summary>
    public void Start()
    {
        _lastPosition = File.Exists(_logPath) ? new FileInfo(_logPath).Length : 0;
        _lineNumber = 0;
        _timer.Start();
    }

    public void Stop() => _timer.Stop();

    private void Poll()
    {
        if (!File.Exists(_logPath))
            return;

        try
        {
            using var fs = new FileStream(_logPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            if (fs.Length < _lastPosition)
            {
                // log file rotated/truncated since last read — re-read from the start
                _lastPosition = 0;
                _lineNumber = 0;
            }
            fs.Seek(_lastPosition, SeekOrigin.Begin);
            using var reader = new StreamReader(fs);
            string? line;
            while ((line = reader.ReadLine()) != null)
            {
                _lineNumber++;
                if (string.IsNullOrWhiteSpace(line))
                    continue;
                try
                {
                    var entry = JsonSerializer.Deserialize<PipelineLogEntry>(line);
                    if (entry != null)
                    {
                        entry.LineNumber = _lineNumber;
                        entry.ObservedAt = DateTime.Now;
                        EntryParsed?.Invoke(entry);
                    }
                }
                catch (JsonException)
                {
                    RawLineFailed?.Invoke(line);
                }
            }
            _lastPosition = fs.Position;
        }
        catch (IOException)
        {
            // Python holds the file open while writing; a transient lock just
            // means "try again next tick", not a real error.
        }
    }

    public void Dispose() => _timer.Stop();
}
