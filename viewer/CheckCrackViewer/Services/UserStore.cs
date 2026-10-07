using System;
using System.IO;
using System.Threading.Tasks;
using CheckCrackViewer.Models;
using Microsoft.Data.Sqlite;

namespace CheckCrackViewer.Services;

/// <summary>CheckCrack Viewer's own login accounts, stored locally in SQLite
/// (%APPDATA%\SmartCrackViewer\users.db) -- completely separate from the
/// "설정" page's MySQL connection (that's for future customer/apartment/image
/// data, not for who's allowed to open this app). Using a local file instead
/// of MySQL means there's no server/connection-string bootstrap problem: the
/// very first run just creates the file and its schema on the spot.</summary>
public static class UserStore
{
    // 2026-10-07 (사용자 지시): users.db는 CheckCrackV2 폴더(예: D:\ClaudePr\CheckCrackV2\users.db)에서 관리한다.
    // 예전 위치(%APPDATA%\SmartCrackViewer\users.db)에 있던 계정은 처음 한 번 그대로 복사해 온다(MigrateLegacyDb).
    // git에는 올리지 않는다(.gitignore).
    private static readonly string DbDir = AppRoot.Path;

    private static readonly string DbPath = Path.Combine(DbDir, "users.db");

    private static readonly string LegacyDbPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "SmartCrackViewer", "users.db");

    private static bool _migrated;

    private static string ConnectionString => $"Data Source={DbPath}";

    private static void MigrateLegacyDb()
    {
        if (_migrated)
            return;
        _migrated = true;
        if (File.Exists(DbPath) || !File.Exists(LegacyDbPath))
            return;
        // WAL 모드라 최근 변경이 -wal 파일에 있을 수 있음 -- SQLite 백업 API로 하나의 일관된 파일로 복사.
        using var source = new SqliteConnection($"Data Source={LegacyDbPath};Mode=ReadOnly");
        source.Open();
        using var target = new SqliteConnection($"Data Source={DbPath}");
        target.Open();
        source.BackupDatabase(target);
    }

    private static SqliteConnection OpenConnection()
    {
        Directory.CreateDirectory(DbDir);
        MigrateLegacyDb();
        var connection = new SqliteConnection(ConnectionString);
        connection.Open();
        using var pragma = connection.CreateCommand();
        pragma.CommandText = "PRAGMA journal_mode=WAL;";
        pragma.ExecuteNonQuery();
        return connection;
    }

    /// <summary>Creates the table if missing, and seeds a default admin/admin123
    /// account the very first time (so LoginWindow never needs its own
    /// account-creation flow -- changing/rotating this password is the
    /// "설정" page's job, see MainViewModel.ChangePassword).</summary>
    public static void EnsureCreated()
    {
        using var connection = OpenConnection();
        using (var command = connection.CreateCommand())
        {
            command.CommandText = """
                CREATE TABLE IF NOT EXISTS users (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    username       TEXT NOT NULL UNIQUE,
                    password_hash  TEXT NOT NULL,
                    display_name   TEXT,
                    role           TEXT,
                    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
                    last_login_at  TEXT
                );
                """;
            command.ExecuteNonQuery();
        }

        using (var countCommand = connection.CreateCommand())
        {
            countCommand.CommandText = "SELECT COUNT(*) FROM users;";
            var count = (long)(countCommand.ExecuteScalar() ?? 0L);
            if (count > 0)
                return;
        }

        using var seedCommand = connection.CreateCommand();
        seedCommand.CommandText = """
            INSERT INTO users (username, password_hash, display_name, role)
            VALUES ('admin', @hash, '관리자', 'admin');
            """;
        seedCommand.Parameters.AddWithValue("@hash", BCrypt.Net.BCrypt.HashPassword("admin123"));
        seedCommand.ExecuteNonQuery();
    }

    public static bool HasAnyUsers()
    {
        using var connection = OpenConnection();
        using var command = connection.CreateCommand();
        command.CommandText = "SELECT COUNT(*) FROM users;";
        var count = (long)(command.ExecuteScalar() ?? 0L);
        return count > 0;
    }

    /// <summary>Used by the "설정" page's account management, not by
    /// LoginWindow (login only ever uses the seeded admin account or whatever
    /// this creates afterward). Throws if the username is already taken.</summary>
    public static Task<AppUser> CreateUserAsync(string username, string password, string displayName)
    {
        using var connection = OpenConnection();
        using var command = connection.CreateCommand();
        command.CommandText = """
            INSERT INTO users (username, password_hash, display_name, role)
            VALUES (@username, @hash, @displayName, 'admin');
            SELECT last_insert_rowid();
            """;
        command.Parameters.AddWithValue("@username", username);
        command.Parameters.AddWithValue("@hash", BCrypt.Net.BCrypt.HashPassword(password));
        command.Parameters.AddWithValue("@displayName", displayName);
        var id = (long)(command.ExecuteScalar() ?? 0L);

        return Task.FromResult(new AppUser
        {
            Id = (int)id,
            Username = username,
            DisplayName = displayName,
            Role = "admin",
        });
    }

    public static Task<AppUser?> 
        ValidateLoginAsync(string username, string password)
    {
        using var connection = OpenConnection();
        using var command = connection.CreateCommand();
        command.CommandText = "SELECT id, username, password_hash, display_name, role FROM users WHERE username = @username LIMIT 1;";
        command.Parameters.AddWithValue("@username", username.Trim());

        using var reader = command.ExecuteReader();
        if (!reader.Read())
            return Task.FromResult<AppUser?>(null);

        var id = reader.GetInt32(0);
        var passwordHash = reader.GetString(2);
        var displayName = reader.IsDBNull(3) ? "" : reader.GetString(3);
        var role = reader.IsDBNull(4) ? "" : reader.GetString(4);
        reader.Close();

        if (!BCrypt.Net.BCrypt.Verify(password, passwordHash))
            return Task.FromResult<AppUser?>(null);

        using var updateCommand = connection.CreateCommand();
        updateCommand.CommandText = "UPDATE users SET last_login_at = datetime('now') WHERE id = @id;";
        updateCommand.Parameters.AddWithValue("@id", id);
        updateCommand.ExecuteNonQuery();

        return Task.FromResult<AppUser?>(new AppUser { Id = id, Username = username.Trim(), DisplayName = displayName, Role = role });
    }

    /// <summary>2026-10-07 로그인 창 "아이디 찾기": 이름(display_name)으로 아이디를 찾는다. 계정은 이 PC의
    /// SQLite에만 있고 이메일/전화가 없어 외부 인증 수단이 없으므로, 아이디는 앞 두 글자만 보이게 가린다
    /// (MaskUsername). 회사 계정(SmartOneFlow) 연동은 차후.</summary>
    public static List<(string MaskedUsername, string DisplayName)> FindUsernamesByDisplayName(string displayName)
    {
        var name = (displayName ?? "").Trim();
        var result = new List<(string, string)>();
        if (name.Length == 0)
            return result;
        using var connection = OpenConnection();
        using var command = connection.CreateCommand();
        command.CommandText = "SELECT username, display_name FROM users WHERE display_name = @name ORDER BY id;";
        command.Parameters.AddWithValue("@name", name);
        using var reader = command.ExecuteReader();
        while (reader.Read())
            result.Add((MaskUsername(reader.GetString(0)), reader.IsDBNull(1) ? "" : reader.GetString(1)));
        return result;
    }

    public static string MaskUsername(string username) =>
        username.Length <= 2 ? username[..1] + new string('*', Math.Max(1, username.Length - 1))
                             : username[..2] + new string('*', username.Length - 2);

    public static int CountUsers()
    {
        using var connection = OpenConnection();
        using var command = connection.CreateCommand();
        command.CommandText = "SELECT COUNT(*) FROM users;";
        return (int)(long)(command.ExecuteScalar() ?? 0L);
    }

    /// <summary>2026-10-07: 로그인 창에서 Ctrl+R(숨김, 화면 어디에도 표시 안 함)로 여는 비밀번호 재설정 --
    /// 아이디와 새 비밀번호만으로 바꾼다(사용자 결정: 임시 비밀번호/다른 관리자 확인 없음, 이 키를 아는 사람만 사용).
    /// 아이디가 없으면 false.</summary>
    public static bool ResetPassword(string username, string newPassword)
    {
        using var connection = OpenConnection();
        using var update = connection.CreateCommand();
        update.CommandText = "UPDATE users SET password_hash = @hash WHERE username = @u;";
        update.Parameters.AddWithValue("@hash", BCrypt.Net.BCrypt.HashPassword(newPassword));
        update.Parameters.AddWithValue("@u", (username ?? "").Trim());
        return update.ExecuteNonQuery() > 0;
    }

    /// <summary>"설정" 페이지의 계정 수정: 현재 비밀번호를 확인한 뒤에만 바꾼다.
    /// Returns false (no change made) if currentPassword doesn't match.</summary>
    public static Task<bool> ChangePasswordAsync(string username, string currentPassword, string newPassword)
    {
        using var connection = OpenConnection();

        string passwordHash;
        int id;
        using (var command = connection.CreateCommand())
        {
            command.CommandText = "SELECT id, password_hash FROM users WHERE username = @username LIMIT 1;";
            command.Parameters.AddWithValue("@username", username.Trim());
            using var reader = command.ExecuteReader();
            if (!reader.Read())
                return Task.FromResult(false);
            id = reader.GetInt32(0);
            passwordHash = reader.GetString(1);
        }

        if (!BCrypt.Net.BCrypt.Verify(currentPassword, passwordHash))
            return Task.FromResult(false);

        using var updateCommand = connection.CreateCommand();
        updateCommand.CommandText = "UPDATE users SET password_hash = @hash WHERE id = @id;";
        updateCommand.Parameters.AddWithValue("@hash", BCrypt.Net.BCrypt.HashPassword(newPassword));
        updateCommand.Parameters.AddWithValue("@id", id);
        updateCommand.ExecuteNonQuery();
        return Task.FromResult(true);
    }

    /// <summary>"설정" 페이지의 계정명(아이디) 변경 -- 비밀번호 변경과 마찬가지로
    /// 현재 비밀번호 확인 후에만 바꾼다. 새 아이디가 이미 있으면 실패로 처리.</summary>
    public static Task<(bool Success, string? Error)> ChangeUsernameAsync(string currentUsername, string newUsername, string currentPassword)
    {
        using var connection = OpenConnection();

        string passwordHash;
        int id;
        using (var command = connection.CreateCommand())
        {
            command.CommandText = "SELECT id, password_hash FROM users WHERE username = @username LIMIT 1;";
            command.Parameters.AddWithValue("@username", currentUsername.Trim());
            using var reader = command.ExecuteReader();
            if (!reader.Read())
                return Task.FromResult<(bool, string?)>((false, "현재 계정을 찾을 수 없습니다."));
            id = reader.GetInt32(0);
            passwordHash = reader.GetString(1);
        }

        if (!BCrypt.Net.BCrypt.Verify(currentPassword, passwordHash))
            return Task.FromResult<(bool, string?)>((false, "비밀번호가 올바르지 않습니다."));

        using (var checkCommand = connection.CreateCommand())
        {
            checkCommand.CommandText = "SELECT COUNT(*) FROM users WHERE username = @newUsername;";
            checkCommand.Parameters.AddWithValue("@newUsername", newUsername.Trim());
            var exists = (long)(checkCommand.ExecuteScalar() ?? 0L) > 0;
            if (exists)
                return Task.FromResult<(bool, string?)>((false, "이미 사용 중인 아이디입니다."));
        }

        using var updateCommand = connection.CreateCommand();
        updateCommand.CommandText = "UPDATE users SET username = @newUsername WHERE id = @id;";
        updateCommand.Parameters.AddWithValue("@newUsername", newUsername.Trim());
        updateCommand.Parameters.AddWithValue("@id", id);
        updateCommand.ExecuteNonQuery();

        return Task.FromResult<(bool, string?)>((true, null));
    }
}
