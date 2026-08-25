using System;
using System.IO;

namespace CheckCrackViewer.Services;

/// <summary>Reads just the single crack.model scalar out of config/pipeline.yaml via
/// plain text/indentation scanning -- no general YAML parser dependency for one
/// read-only value (CLAUDE.local.md #33 documents the file's shape). Returns null
/// (never a guess) if the file, the "crack:" section, or "model:" key is missing --
/// caller must show an explicit "not found" message, not blank/fabricated text
/// (CLAUDE.local.md #5: "없는 값은 null. 임의 생성 금지").</summary>
public static class PipelineConfigReader
{
    public static string? ReadCrackModelPath(string rootPath)
    {
        var path = Path.Combine(rootPath, "config", "pipeline.yaml");
        if (!File.Exists(path))
            return null;

        try
        {
            var lines = File.ReadAllLines(path);
            var inCrackSection = false;
            foreach (var raw in lines)
            {
                if (string.IsNullOrWhiteSpace(raw))
                    continue;

                var indent = raw.Length - raw.TrimStart().Length;
                var trimmed = raw.Trim();

                if (indent == 0)
                {
                    inCrackSection = trimmed == "crack:";
                    continue;
                }

                if (!inCrackSection)
                    continue;

                if (trimmed.StartsWith("model:", StringComparison.Ordinal))
                {
                    var value = trimmed["model:".Length..].Trim();
                    var hashIdx = value.IndexOf(" #", StringComparison.Ordinal);
                    if (hashIdx >= 0)
                        value = value[..hashIdx].Trim();
                    value = value.Trim('"', '\'');
                    return string.IsNullOrEmpty(value) ? null : value;
                }
            }
            return null;
        }
        catch (IOException)
        {
            return null;
        }
    }
}
