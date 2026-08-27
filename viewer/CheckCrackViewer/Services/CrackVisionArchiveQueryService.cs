using Npgsql;

namespace CheckCrackViewer.Services;

/// <summary>One row of MngData backend_core's crackvision_archives table (see
/// backend_core/schemas/facade_archives.sql -- column names confirmed directly from that file,
/// 2026-08-27). Read-only -- CheckCrackViewer never writes to this table, only queries it for
/// the "수동 다운로드" browse UI (관리자가 직접 선택, see AnalysisLoadBalancer README).</summary>
public sealed record CrackVisionArchiveRecord(
    long ArchiveId, string Company, string Building, string ZipPath,
    long SizeBytes, int ImageCount, IReadOnlyList<string> Directions, DateTime CreatedAt);

/// <summary>Direct PostgreSQL client for CrackVisionDB (MngData backend_core) -- used only by
/// the manual browse/download path. The automatic (DDS AnalysisAssignment) path never queries
/// this database at all; it already receives everything it needs over DDS.</summary>
public static class CrackVisionArchiveQueryService
{
    public static async Task<IReadOnlyList<CrackVisionArchiveRecord>> ListArchivesAsync(CrackVisionDbSettings settings,
        int limit = 200, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(settings.PostgresHost))
            throw new InvalidOperationException("PostgreSQL host가 설정되지 않았습니다 (설정 화면에서 CrackVisionDB 접속 정보를 입력하세요).");

        var connString = new NpgsqlConnectionStringBuilder
        {
            Host = settings.PostgresHost,
            Port = settings.PostgresPort,
            Database = settings.PostgresDatabase,
            Username = settings.PostgresUser,
            Password = settings.PostgresPassword,
        }.ToString();

        await using var conn = new NpgsqlConnection(connString);
        await conn.OpenAsync(cancellationToken);

        await using var cmd = new NpgsqlCommand(
            "SELECT archive_id, company, building, zip_path, size_bytes, image_count, directions, created_at " +
            "FROM crackvision_archives ORDER BY created_at DESC LIMIT $1", conn);
        cmd.Parameters.AddWithValue(limit);

        var results = new List<CrackVisionArchiveRecord>();
        await using var reader = await cmd.ExecuteReaderAsync(cancellationToken);
        while (await reader.ReadAsync(cancellationToken))
        {
            results.Add(new CrackVisionArchiveRecord(
                reader.GetInt64(0), reader.GetString(1), reader.GetString(2), reader.GetString(3),
                reader.GetInt64(4), reader.GetInt32(5), reader.GetFieldValue<string[]>(6), reader.GetDateTime(7)));
        }
        return results;
    }
}
