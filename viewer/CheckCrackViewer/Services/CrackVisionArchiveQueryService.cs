using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;
using Npgsql;

namespace CheckCrackViewer.Services;

/// <summary>Deserialization shape for one entry of {facade_id}_cracks.json -- fixed by
/// CheckCrackV2's tools/detect_cracks_folder.py payload construction, NOT by this project.
/// length_mm/max_width_mm/area_mm2/severity are all null in the source JSON exactly when the
/// facade isn't scale-calibrated (never fabricated from px alone). position.u_m/v_m are always
/// null in the JSON today -- the Python side never computes them -- UpsertFacadeCracksAsync
/// fills those in itself from crackvision_facades.px_per_m at ingestion time.</summary>
internal sealed class CrackJsonEntry
{
    [JsonPropertyName("crack_id")] public string CrackId { get; set; } = "";
    [JsonPropertyName("length_px")] public double LengthPx { get; set; }
    [JsonPropertyName("max_width_px")] public double MaxWidthPx { get; set; }
    [JsonPropertyName("mean_width_px")] public double MeanWidthPx { get; set; }
    [JsonPropertyName("area_px")] public double AreaPx { get; set; }
    [JsonPropertyName("length_mm")] public double? LengthMm { get; set; }
    [JsonPropertyName("max_width_mm")] public double? MaxWidthMm { get; set; }
    [JsonPropertyName("area_mm2")] public double? AreaMm2 { get; set; }
    [JsonPropertyName("confidence")] public double Confidence { get; set; }
    [JsonPropertyName("observation_state")] public string? ObservationState { get; set; }
    [JsonPropertyName("severity")] public string? Severity { get; set; }
    [JsonPropertyName("position")] public CrackPositionJson? Position { get; set; }
    [JsonPropertyName("bbox_px")] public double[] BboxPx { get; set; } = Array.Empty<double>();
    [JsonPropertyName("polygon_px")] public double[][] PolygonPx { get; set; } = Array.Empty<double[]>();
    [JsonPropertyName("source_observations")] public List<CrackSourceObservationJson> SourceObservations { get; set; } = new();
}

internal sealed class CrackPositionJson
{
    [JsonPropertyName("pixel_x")] public double PixelX { get; set; }
    [JsonPropertyName("pixel_y")] public double PixelY { get; set; }
}

internal sealed class CrackSourceObservationJson
{
    [JsonPropertyName("image_id")] public string ImageId { get; set; } = "";
    [JsonPropertyName("bbox_px_in_source")] public double[] BboxPxInSource { get; set; } = Array.Empty<double>();
    [JsonPropertyName("owned_pixel_count")] public long OwnedPixelCount { get; set; }
}

/// <summary>Deserialization shape for {facade_id}_scale_colmap.json -- mirrors src/crack/
/// measurement.py's ScaleInfo dataclass exactly (field-for-field), see
/// pipeline/runner.py for where it's written. Absent entirely for a facade stitched only via
/// the plain homography-chain mosaic (no metric scale basis at all).</summary>
internal sealed class ScaleColmapJson
{
    [JsonPropertyName("px_per_m")] public double? PxPerM { get; set; }
    [JsonPropertyName("calibrated")] public bool Calibrated { get; set; }
    [JsonPropertyName("reference_object_type")] public string? ReferenceObjectType { get; set; }
    [JsonPropertyName("reference_length_mm")] public double? ReferenceLengthMm { get; set; }
}

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

    /// <summary>Reads {facadeId}_cracks.json (+ {facadeId}_scale_colmap.json, if present) off
    /// outputDir -- the same local folder GenerateReport just finished writing to, before it
    /// gets zipped/uploaded -- and upserts crackvision_facades/crackvision_cracks/
    /// crackvision_crack_sources (schemas/crackvision_cracks.sql, MngData backend_core). Called
    /// alongside UpdateAnalysisResultAsync from WriteBackAnalysisResultsAsync; same direct-Npgsql
    /// pattern for the same reason (see this class's own header comment).
    ///
    /// No-op if {facadeId}_cracks.json doesn't exist -- matches FacadeItemViewModel.HasCrackResults's
    /// own gate, not an error (a facade can be stitched without crack detection ever having run).
    ///
    /// Replaces this facade's crack rows wholesale (delete + re-insert in one transaction) on
    /// every call rather than diffing against what's already there -- run-to-run comparison is a
    /// separate, deferred feature (crackvision_crack_links), not this method's job. crack_id
    /// itself stays stable across re-runs of the SAME mosaic version regardless
    /// (src/crack/merge_tiles.py's own job, upstream of this method, in the CheckCrackV2 repo).</summary>
    public static async Task UpsertFacadeCracksAsync(CrackVisionDbSettings settings, long archiveId, string facadeId,
        string outputDir, string? mosaicPath, double? coverageRatio, bool needsRetake, bool usedColmap,
        CancellationToken cancellationToken = default)
    {
        var cracksPath = Path.Combine(outputDir, $"{facadeId}_cracks.json");
        if (!File.Exists(cracksPath))
            return;

        List<CrackJsonEntry>? entries;
        try
        {
            entries = JsonSerializer.Deserialize<List<CrackJsonEntry>>(
                await File.ReadAllTextAsync(cracksPath, cancellationToken));
        }
        catch (JsonException)
        {
            return; // corrupt/partial file -- same "never let a write-back hiccup fail the run" stance as the caller
        }
        if (entries == null)
            return;

        ScaleColmapJson? scale = null;
        var scalePath = Path.Combine(outputDir, $"{facadeId}_scale_colmap.json");
        if (File.Exists(scalePath))
        {
            try
            {
                scale = JsonSerializer.Deserialize<ScaleColmapJson>(await File.ReadAllTextAsync(scalePath, cancellationToken));
            }
            catch (JsonException)
            {
                scale = null;
            }
        }
        var calibrated = scale?.Calibrated ?? false;
        double? pxPerM = calibrated ? scale?.PxPerM : null;

        // facade_id's own leading-direction-token convention (e.g. "FRONT_0" -> direction
        // "FRONT", sub_index 0; "ROOF" alone -> "ROOF"/0) -- see crackvision_cracks.sql's own
        // comment on why this is derived here rather than declared as a separate input.
        var parts = facadeId.Split('_', 2);
        var direction = parts[0].ToUpperInvariant();
        var subIndex = 0;
        if (parts.Length > 1 && int.TryParse(parts[1], out var parsedSub))
            subIndex = parsedSub;

        var mosaicSize = TryReadImageSize(mosaicPath);

        await using var conn = new NpgsqlConnection(BuildConnString(settings));
        await conn.OpenAsync(cancellationToken);
        await using var tx = await conn.BeginTransactionAsync(cancellationToken);

        long facadeRowId;
        await using (var cmd = new NpgsqlCommand(
            "INSERT INTO crackvision_facades " +
            "(archive_id, facade_id, direction, sub_index, mosaic_path, mosaic_width_px, mosaic_height_px, " +
            " coverage_ratio, needs_retake, used_colmap, scale_calibrated, scale_reference_type, " +
            " scale_reference_length_mm, px_per_m, updated_at) " +
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14, now()) " +
            "ON CONFLICT (archive_id, facade_id) DO UPDATE SET " +
            "mosaic_path = EXCLUDED.mosaic_path, mosaic_width_px = EXCLUDED.mosaic_width_px, " +
            "mosaic_height_px = EXCLUDED.mosaic_height_px, coverage_ratio = EXCLUDED.coverage_ratio, " +
            "needs_retake = EXCLUDED.needs_retake, used_colmap = EXCLUDED.used_colmap, " +
            "scale_calibrated = EXCLUDED.scale_calibrated, scale_reference_type = EXCLUDED.scale_reference_type, " +
            "scale_reference_length_mm = EXCLUDED.scale_reference_length_mm, px_per_m = EXCLUDED.px_per_m, " +
            "updated_at = now() " +
            "RETURNING facade_row_id", conn, tx))
        {
            cmd.Parameters.AddWithValue(archiveId);
            cmd.Parameters.AddWithValue(facadeId);
            cmd.Parameters.AddWithValue(direction);
            cmd.Parameters.AddWithValue(subIndex);
            cmd.Parameters.AddWithValue((object?)mosaicPath ?? DBNull.Value);
            cmd.Parameters.AddWithValue((object?)mosaicSize?.Width ?? DBNull.Value);
            cmd.Parameters.AddWithValue((object?)mosaicSize?.Height ?? DBNull.Value);
            cmd.Parameters.AddWithValue((object?)coverageRatio ?? DBNull.Value);
            cmd.Parameters.AddWithValue(needsRetake);
            cmd.Parameters.AddWithValue(usedColmap);
            cmd.Parameters.AddWithValue(calibrated);
            cmd.Parameters.AddWithValue((object?)scale?.ReferenceObjectType ?? DBNull.Value);
            cmd.Parameters.AddWithValue((object?)scale?.ReferenceLengthMm ?? DBNull.Value);
            cmd.Parameters.AddWithValue((object?)pxPerM ?? DBNull.Value);
            facadeRowId = (long)(await cmd.ExecuteScalarAsync(cancellationToken))!;
        }

        // crackvision_crack_sources rows for this facade cascade-delete via their FK to
        // crackvision_cracks -- no separate DELETE needed for that table.
        await using (var cmd = new NpgsqlCommand(
            "DELETE FROM crackvision_cracks WHERE facade_row_id = $1", conn, tx))
        {
            cmd.Parameters.AddWithValue(facadeRowId);
            await cmd.ExecuteNonQueryAsync(cancellationToken);
        }

        foreach (var c in entries)
        {
            double? uM = null, vM = null;
            if (pxPerM is > 0 && c.Position != null)
            {
                uM = c.Position.PixelX / pxPerM.Value;
                vM = c.Position.PixelY / pxPerM.Value;
            }

            await using (var cmd = new NpgsqlCommand(
                "INSERT INTO crackvision_cracks " +
                "(facade_row_id, crack_id, bbox_px, polygon_px, length_px, width_px, mean_width_px, area_px, " +
                " length_mm, width_mm, area_mm2, confidence, observation_state, severity, " +
                " position_pixel_x, position_pixel_y, position_u_m, position_v_m) " +
                "VALUES ($1,$2,$3::jsonb,$4::jsonb,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)", conn, tx))
            {
                cmd.Parameters.AddWithValue(facadeRowId);
                cmd.Parameters.AddWithValue(c.CrackId);
                cmd.Parameters.AddWithValue(JsonSerializer.Serialize(c.BboxPx));
                cmd.Parameters.AddWithValue(JsonSerializer.Serialize(c.PolygonPx));
                cmd.Parameters.AddWithValue(c.LengthPx);
                cmd.Parameters.AddWithValue(c.MaxWidthPx);
                cmd.Parameters.AddWithValue(c.MeanWidthPx);
                cmd.Parameters.AddWithValue(c.AreaPx);
                cmd.Parameters.AddWithValue((object?)c.LengthMm ?? DBNull.Value);
                cmd.Parameters.AddWithValue((object?)c.MaxWidthMm ?? DBNull.Value);
                cmd.Parameters.AddWithValue((object?)c.AreaMm2 ?? DBNull.Value);
                cmd.Parameters.AddWithValue(c.Confidence);
                cmd.Parameters.AddWithValue((object?)c.ObservationState ?? DBNull.Value);
                cmd.Parameters.AddWithValue((object?)c.Severity ?? DBNull.Value);
                cmd.Parameters.AddWithValue((object?)c.Position?.PixelX ?? DBNull.Value);
                cmd.Parameters.AddWithValue((object?)c.Position?.PixelY ?? DBNull.Value);
                cmd.Parameters.AddWithValue((object?)uM ?? DBNull.Value);
                cmd.Parameters.AddWithValue((object?)vM ?? DBNull.Value);
                await cmd.ExecuteNonQueryAsync(cancellationToken);
            }

            foreach (var src in c.SourceObservations)
            {
                await using var cmd = new NpgsqlCommand(
                    "INSERT INTO crackvision_crack_sources " +
                    "(facade_row_id, crack_id, image_id, bbox_px_in_source, owned_pixel_count) " +
                    "VALUES ($1,$2,$3,$4::jsonb,$5)", conn, tx);
                cmd.Parameters.AddWithValue(facadeRowId);
                cmd.Parameters.AddWithValue(c.CrackId);
                cmd.Parameters.AddWithValue(src.ImageId);
                cmd.Parameters.AddWithValue(JsonSerializer.Serialize(src.BboxPxInSource));
                cmd.Parameters.AddWithValue(src.OwnedPixelCount);
                await cmd.ExecuteNonQueryAsync(cancellationToken);
            }
        }

        await tx.CommitAsync(cancellationToken);
    }

    /// <summary>Best-effort mosaic pixel size via a WPF BitmapDecoder header read
    /// (BitmapCreateOptions.DelayCreation -- reads just the header, never decodes the full
    /// raster, safe for a large stitched mosaic). Returns null on any failure (missing file,
    /// unsupported format, locked file) -- mosaic_width_px/height_px are nullable in the schema
    /// specifically for this.</summary>
    private static (int Width, int Height)? TryReadImageSize(string? path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
            return null;
        try
        {
            using var stream = File.OpenRead(path);
            var decoder = System.Windows.Media.Imaging.BitmapDecoder.Create(
                stream, System.Windows.Media.Imaging.BitmapCreateOptions.DelayCreation,
                System.Windows.Media.Imaging.BitmapCacheOption.None);
            var frame = decoder.Frames[0];
            return (frame.PixelWidth, frame.PixelHeight);
        }
        catch
        {
            return null;
        }
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
