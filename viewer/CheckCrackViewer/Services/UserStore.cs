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
    private static readonly string DbDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "SmartCrackViewer");

    private static readonly string DbPath = Path.Combine(DbDir, "users.db");

    private static string ConnectionString => $"Data Source={DbPath}";

    private static SqliteConnection OpenConnection()
    {
        Directory.CreateDirectory(DbDir);
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

    public static Task<AppUser?> ValidateLoginAsync(string username, string password)
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
