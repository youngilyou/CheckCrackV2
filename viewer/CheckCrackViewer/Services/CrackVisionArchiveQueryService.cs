using Npgsql;

namespace CheckCrackViewer.Services;

/// <summary>One row of MngData backend_core's crackvision_archives table (see
/// backend_core/schemas/facade_archives.sql -- column names confirmed directly from that file,
/// 2026-08-27). ContractId/CustomerName/StitchingZipPath/ReportPath/AnalysisStatus (2026-08-28)
/// are all nullable -- they stay empty for rows created before this schema addition, and for
/// archives whose analysis hasn't finished (or was never run) yet.</summary>
public sealed record CrackVisionArchiveRecord(
    long ArchiveId, string Company, string Building, string ZipPath,
    long SizeBytes, int ImageCount, IReadOnlyList<string> Directions, DateTime CreatedAt,
    string? ContractId, string? CustomerName, string? StitchingZipPath, string? ReportPath, string? AnalysisStatus);

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
            "contract_id, customer_name, stitching_zip_path, report_path, analysis_status " +
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
                NullableString(reader, 11), NullableString(reader, 12)));
        }
        return results;
    }

    /// <summary>Write-back called from MainViewModel.GenerateReport once stitching->크랙검사->
    /// 보고서 all succeeded for a facade whose ArchiveId is known. Any of the three value
    /// parameters left null leaves that column untouched (COALESCE against the existing value) --
    /// so a caller that only just finished the report doesn't have to already know/resend the
    /// stitching path from an earlier step.</summary>
    public static async Task UpdateAnalysisResultAsync(CrackVisionDbSettings settings, long archiveId,
        string? stitchingZipPath, string? reportPath, string? analysisStatus, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(settings.PostgresHost))
            throw new InvalidOperationException("PostgreSQL host가 설정되지 않았습니다 (설정 화면에서 CrackVisionDB 접속 정보를 입력하세요).");

        await using var conn = new NpgsqlConnection(BuildConnString(settings));
        await conn.OpenAsync(cancellationToken);

        await using var cmd = new NpgsqlCommand(
            "UPDATE crackvision_archives SET " +
            "stitching_zip_path = COALESCE($1, stitching_zip_path), " +
            "report_path = COALESCE($2, report_path), " +
            "analysis_status = COALESCE($3, analysis_status) " +
            "WHERE archive_id = $4", conn);
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
