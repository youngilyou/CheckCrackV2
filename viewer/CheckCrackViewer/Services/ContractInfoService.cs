using MySqlConnector;

namespace CheckCrackViewer.Services;

/// <summary>Contract facts for the report cover, from SmartCrackWeb's MySQL (smartcrack; same DB the
/// GenerateJsonOfScanArea tool reads): building name, address, client company, request number, and the 신청서 face
/// this facade belongs to.</summary>
public sealed record ContractInfo(string? BuildingName, string? Address, string? Client, string? RequestNo, string? FaceLabel);

/// <summary>2026-10-09 (사용자 요청: 보고서 표지의 의뢰자/주소/건물명을 서버에서). Read-only. Uses the viewer's
/// "DB 설정" (MySQL) connection; not configured or unreachable -> null, and the report keeps "미등록".</summary>
/// <summary>The company issuing the report (발행처) -- SmartOneFlow/SmartCrackWeb CompanySettings (one row).</summary>
public sealed record IssuerInfo(string? Company, string? Representative, string? BusinessNo, string? Address, string? Phone);

public static class ContractInfoService
{
    /// <summary>2026-10-09 (사용자: 발행처는 DB에 있음): CompanySettings, the single company row ERP admins maintain.</summary>
    public static async Task<IssuerInfo?> GetIssuerAsync(DbConnectionSettings? settings, CancellationToken cancellationToken = default)
    {
        if (settings == null || string.IsNullOrWhiteSpace(settings.Host) || string.IsNullOrWhiteSpace(settings.Database))
            return null;
        await using var conn = new MySqlConnection(BuildConnectionString(settings));
        await conn.OpenAsync(cancellationToken);
        await using var cmd = conn.CreateCommand();
        cmd.CommandText = "SELECT Name, Representative, BusinessRegNo, Address, Phone FROM CompanySettings ORDER BY Id LIMIT 1";
        await using var reader = await cmd.ExecuteReaderAsync(cancellationToken);
        if (!await reader.ReadAsync(cancellationToken))
            return null;
        string? Str(int i) => reader.IsDBNull(i) || string.IsNullOrWhiteSpace(reader.GetString(i)) ? null : reader.GetString(i).Trim();
        return new IssuerInfo(Str(0), Str(1), Str(2), Str(3), Str(4));
    }

    private static string BuildConnectionString(DbConnectionSettings settings) => new MySqlConnectionStringBuilder
    {
        Server = settings.Host.Trim(),
        Port = (uint)(settings.Port > 0 ? settings.Port : 3306),
        Database = settings.Database.Trim(),
        UserID = settings.User.Trim(),
        Password = settings.Password,
        SslMode = settings.UseSsl ? MySqlSslMode.Required : MySqlSslMode.None,
        ConnectionTimeout = (uint)Math.Max(1, settings.TimeoutSeconds),
        AllowPublicKeyRetrieval = true,
    }.ConnectionString;

    // SmartCrackWeb WallFace enum East=0, West=1, South=2, North=3 -- since 2026-10-08 building-relative faces, not compass
    // directions (SmartCrackWeb Labels.cs): 정면/후면/좌측면/우측면.
    private static readonly string[] FaceLabels = { "정면", "후면", "좌측면", "우측면" };

    /// <summary>Same order as SmartCrackWeb CrackVisionSync: 1) FacadeFaceMappings exception, 2) the fixed standard
    /// FRONT=정면, BACK=후면, LEFT=좌측면, RIGHT=우측면 (Labels.DirectionToFace), else null.</summary>
    public static string? StandardFaceLabel(string? direction) => direction?.Trim().ToUpperInvariant() switch
    {
        "FRONT" => "정면", "BACK" => "후면", "LEFT" => "좌측면", "RIGHT" => "우측면", _ => null,
    };

    public static async Task<ContractInfo?> GetAsync(DbConnectionSettings? settings, string contractNo, string? dongNo,
        string? direction, CancellationToken cancellationToken = default)
    {
        if (settings == null || string.IsNullOrWhiteSpace(settings.Host) || string.IsNullOrWhiteSpace(settings.Database)
            || string.IsNullOrWhiteSpace(contractNo))
            return null;
        await using var conn = new MySqlConnection(BuildConnectionString(settings));
        await conn.OpenAsync(cancellationToken);

        long requestId;
        string? buildingName, address, client, requestNo;
        await using (var cmd = conn.CreateCommand())
        {
            cmd.CommandText = """
                SELECT r.Id, b.Name, b.Address, u.CompanyName, u.Name, r.RequestNo
                FROM Contracts c
                JOIN InspectionRequests r ON r.Id = c.RequestId
                JOIN Buildings b ON b.Id = r.BuildingId
                LEFT JOIN Users u ON u.Id = r.UserId
                WHERE c.ContractNo = @no
                ORDER BY c.Id DESC
                LIMIT 1
                """;
            cmd.Parameters.AddWithValue("@no", contractNo.Trim());
            await using var reader = await cmd.ExecuteReaderAsync(cancellationToken);
            if (!await reader.ReadAsync(cancellationToken))
                return null;
            string? Str(int i) => reader.IsDBNull(i) || string.IsNullOrWhiteSpace(reader.GetString(i)) ? null : reader.GetString(i).Trim();
            requestId = reader.GetInt64(0);
            buildingName = Str(1);
            address = Str(2);
            client = Str(3) ?? Str(4);
            requestNo = Str(5);
        }

        string? faceLabel = null;
        if (!string.IsNullOrWhiteSpace(dongNo) && !string.IsNullOrWhiteSpace(direction))
        {
            try
            {
                await using var cmd = conn.CreateCommand();
                cmd.CommandText = "SELECT Face FROM FacadeFaceMappings WHERE RequestId = @rid AND DongNo = @dong AND Direction = @dir LIMIT 1";
                cmd.Parameters.AddWithValue("@rid", requestId);
                cmd.Parameters.AddWithValue("@dong", dongNo.Trim());
                cmd.Parameters.AddWithValue("@dir", direction.Trim());
                if (await cmd.ExecuteScalarAsync(cancellationToken) is { } face && face is not DBNull)
                {
                    var i = Convert.ToInt32(face);
                    faceLabel = i >= 0 && i < FaceLabels.Length ? FaceLabels[i] : null;
                }
            }
            catch (MySqlException)
            {
                // table not deployed yet -- the face is optional
            }
        }
        return new ContractInfo(buildingName, address, client, requestNo, faceLabel ?? StandardFaceLabel(direction));
    }
}
