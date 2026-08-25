namespace CheckCrackViewer.Models;

/// <summary>The logged-in user, returned by UserStore on successful login.
/// Deliberately carries no password/hash -- nothing downstream needs it once
/// authentication has already happened.</summary>
public sealed class AppUser
{
    public int Id { get; set; }
    public string Username { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string Role { get; set; } = "";
}
