using System.Text.Json;
using Npgsql;

namespace CheckCrackViewer.Services;

/// <summary>One entry of crackvision_archives.facade_analysis_results (2026-08-29) -- one archive
/// can have multiple facades (directions) sharing the same archive_id, and each facade's
/// write-back must NOT clobber its siblings' results (see that column's own schema comment for
/// the bug this replaced: flat stitching_zip_path/report_path/analysis_status alone meant only
/// the last-finished facade's result survived). FacadeId matches FacadeItemViewModel.FacadeId /
/// the facade_id already embedded in the uploaded file names.</summary>
public sealed record FacadeAnalysisResultEntry(string FacadeId, string? StitchingZipPath, string? ReportPath, string? Status);

/// <summary>One row of MngData backend_core's crackvision_archives table (see
/// backend_core/schemas/facade_archives.sql -- column names confirmed directly from that file,
/// 2026-08-27). ContractId/CustomerName/StitchingZipPath/ReportPath/AnalysisStatus (2026-08-28)
/// are all nullable -- they stay empty for rows created before this schema addition, and for
/// archives whose analysis hasn't finished (or was never run) yet. StitchingZipPath/ReportPath
/// still reflect whichever facade wrote back most recently (kept for backward compat / an
/// at-a-glance summary for the common single-direction archive) -- FacadeResults (2026-08-29) is
/// the authoritative per-facade breakdown for archives with more than one direction.</summary>
public sealed record CrackVisionArchiveRecord(
    long ArchiveId, string Company, string Building, string ZipPath,
    long SizeBytes, int ImageCount, IReadOnlyList<string> Directions, DateTime CreatedAt,
    string? ContractId, string? CustomerName, string? StitchingZipPath, string? ReportPath, string? AnalysisStatus,
    IReadOnlyList<FacadeAnalysisResultEntry> FacadeResults);

/// <summary>Direct PostgreSQL client for CrackVisionDB (MngData backend_core) -- used by the
/// manual browse/download path (read) and by MainViewModel.GenerateReport's analysis-result
/// write-back (update). The automatic (DDS AnalysisAssignment) path never queries this database
/// for its own dispatch; it already receives everything it needs over DDS -- but GenerateReport's
/// write-back applies to facades from EITHER path (see FacadeItemViewModel.ArchiveId).
///
/// 2026-08-28: this was previously read-only by design (see git history) -- the write side was
/// added specifically for stitching/report write-back, deliberately going straight to Postgres
/// rather than through a new backend_core REST endpoint, since backend_core's existing
/// crackvision REST endpoints all require an authenticated MngData web session
/// (X-MngData-Session), which doesn't exist for a machine-to-machine callback like this one, and
/// this workstation already holds live Postgres credentials for the read path above.</summary>
public static class CrackVisionArchiveQueryService
{
    public static async Task<IReadOnlyList<CrackVisionArchiveRecord>> ListArchivesAsync(CrackVisionDbSettings settings,
        int limit = 200, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(settings.PostgresHost))
            throw new InvalidOperationException("PostgreSQL host가 설정되지 않았습니다 (설정 화면에서 CrackVisionDB 접속 정보를 입력하세요).");

        await using var conn = new NpgsqlConnection(BuildConnString(settings));
        await conn.OpenAsync(cancellationToken);

        await using var cmd = new NpgsqlCommand(
            "SELECT archive_id, company, building, zip_path, size_bytes, image_count, directions, created_at, " +
            "contract_id, customer_name, stitching_zip_path, report_path, analysis_status, facade_analysis_results::text " +
            "FROM crackvision_archives ORDER BY created_at DESC LIMIT $1", conn);
        cmd.Parameters.AddWithValue(limit);

        var results = new List<CrackVisionArchiveRecord>();
        await using var reader = await cmd.ExecuteReaderAsync(cancellationToken);
        while (await reader.ReadAsync(cancellationToken))
        {
            results.Add(new CrackVisionArchiveRecord(
                reader.GetInt64(0), reader.GetString(1), reader.GetString(2), reader.GetString(3),
                reader.GetInt64(4), reader.GetInt32(5), reader.GetFieldValue<string[]>(6), reader.GetDateTime(7),
                NullableString(reader, 8), NullableString(reader, 9), NullableString(reader, 10),
                NullableString(reader, 11), NullableString(reader, 12), ParseFacadeResults(reader.GetString(13))));
        }
        return results;
    }

    /// <summary>Parses facade_analysis_results (a JSON object keyed by facade_id, see that
    /// column's schema comment) into a flat list. Malformed/empty JSON (should only happen for
    /// the '{}' default) yields an empty list rather than throwing -- this is a display concern,
    /// not something that should ever break the archive list.</summary>
    private static IReadOnlyList<FacadeAnalysisResultEntry> ParseFacadeResults(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var list = new List<FacadeAnalysisResultEntry>();
            foreach (var prop in doc.RootElement.EnumerateObject())
            {
                var value = prop.Value;
                list.Add(new FacadeAnalysisResultEntry(
                    prop.Name,
                    value.TryGetProperty("stitching_zip_path", out var s) && s.ValueKind == JsonValueKind.String ? s.GetString() : null,
                    value.TryGetProperty("report_path", out var r) && r.ValueKind == JsonValueKind.String ? r.GetString() : null,
                    value.TryGetProperty("status", out var st) && st.ValueKind == JsonValueKind.String ? st.GetString() : null));
            }
            return list;
        }
        catch (JsonException)
        {
            return Array.Empty<FacadeAnalysisResultEntry>();
        }
    }

    /// <summary>Write-back called from MainViewModel.GenerateReport once stitching->크랙검사->
    /// 보고서 all succeeded for a facade whose ArchiveId is known. Any of the three value
    /// parameters left null leaves that column/JSONB-field untouched (COALESCE against the
    /// existing value) -- so a caller that only just finished the report doesn't have to already
    /// know/resend the stitching path from an earlier step.
    ///
    /// facadeId (2026-08-29, required) identifies which facade (direction) this write-back is
    /// for -- facade_analysis_results is merged (`||`) keyed by facadeId, never replaced wholesale,
    /// so archives with more than one direction preserve every facade's own result instead of the
    /// last-finished one clobbering the rest (see that column's schema comment). The flat
    /// stitching_zip_path/report_path/analysis_status columns are still updated the same as
    /// before -- kept as a "most recently finished facade" summary for backward compat and the
    /// common single-direction case.</summary>
    public static async Task UpdateAnalysisResultAsync(CrackVisionDbSettings settings, long archiveId, string facadeId,
        string? stitchingZipPath, string? reportPath, string? analysisStatus, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(settings.PostgresHost))
            throw new InvalidOperationException("PostgreSQL host가 설정되지 않았습니다 (설정 화면에서 CrackVisionDB 접속 정보를 입력하세요).");
        if (string.IsNullOrWhiteSpace(facadeId))
            throw new ArgumentException("facadeId가 비어 있습니다 -- facade_analysis_results를 어느 facade 것으로 기록할지 알 수 없습니다.", nameof(facadeId));

        await using var conn = new NpgsqlConnection(BuildConnString(settings));
        await conn.OpenAsync(cancellationToken);

        // Merge fragment는 이 facade의 키 하나만 담는다 -- `||`는 최상위 키 단위로 병합되므로
        // (같은 키가 있으면 통째로 교체, 다른 키는 그대로 유지) 다른 facade의 기존 결과에는
        // 전혀 영향을 주지 않는다.
        var mergeFragment = JsonSerializer.Serialize(new Dictionary<string, object?>
        {
            [facadeId] = new
            {
                stitching_zip_path = stitchingZipPath,
                report_path = reportPath,
                status = analysisStatus,
                updated_at = DateTime.UtcNow.ToString("O"),
            },
        });

        await using var cmd = new NpgsqlCommand(
            "UPDATE crackvision_archives SET " +
            "facade_analysis_results = facade_analysis_results || $1::jsonb, " +
            "stitching_zip_path = COALESCE($2, stitching_zip_path), " +
            "report_path = COALESCE($3, report_path), " +
            "analysis_status = COALESCE($4, analysis_status) " +
            "WHERE archive_id = $5", conn);
        cmd.Parameters.AddWithValue(mergeFragment);
        cmd.Parameters.AddWithValue((object?)stitchingZipPath ?? DBNull.Value);
        cmd.Parameters.AddWithValue((object?)reportPath ?? DBNull.Value);
        cmd.Parameters.AddWithValue((object?)analysisStatus ?? DBNull.Value);
        cmd.Parameters.AddWithValue(archiveId);
        await cmd.ExecuteNonQueryAsync(cancellationToken);
    }

    private static string BuildConnString(CrackVisionDbSettings settings) =>
        new NpgsqlConnectionStringBuilder
        {
            Host = settings.PostgresHost,
            Port = settings.PostgresPort,
            Database = settings.PostgresDatabase,
            Username = settings.PostgresUser,
            Password = settings.PostgresPassword,
        }.ToString();

    private static string? NullableString(NpgsqlDataReader reader, int ordinal) =>
        reader.IsDBNull(ordinal) ? null : reader.GetString(ordinal);
}
