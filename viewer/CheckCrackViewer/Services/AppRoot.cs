using System.IO;

namespace CheckCrackViewer.Services;

/// <summary>The CheckCrackV2 project folder this viewer works in (e.g. D:\ClaudePr\CheckCrackV2) -- found by
/// walking up from the exe folder to the CLAUDE.local.md marker. Shared by MainViewModel (RootPath) and
/// UserStore (users.db lives here since 2026-10-07), which runs before MainViewModel exists (login window).</summary>
public static class AppRoot
{
    private static string? _path;

    public static string Path => _path ??= Discover();

    private static string Discover()
    {
        var probe = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
        for (int i = 0; i < 8 && probe != null; i++, probe = probe.Parent)
        {
            if (File.Exists(System.IO.Path.Combine(probe.FullName, "CLAUDE.local.md")))
                return probe.FullName;
        }
        return @"D:\ClaudePr\CheckCrack";
    }
}
