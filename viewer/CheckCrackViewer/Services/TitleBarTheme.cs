using System;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;

namespace CheckCrackViewer.Services;

/// <summary>Recolors the native OS title bar (Windows 11 22H2+'s
/// DWMWA_CAPTION_COLOR) to match this app's dark theme instead of the
/// default system accent blue -- user flagged the login window's title bar
/// standing out as a bright blue strip against the rest of the dark UI.
/// DwmSetWindowAttribute silently no-ops on older Windows builds that don't
/// support this attribute, so this never throws on an unsupported OS.</summary>
public static class TitleBarTheme
{
    private const int DwmwaCaptionColor = 35;
    private const int DwmwaTextColor = 36;

    [DllImport("dwmapi.dll", PreserveSig = true)]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int dwAttribute, ref int pvAttribute, int cbAttribute);

    public static void Apply(Window window, Color caption, Color text)
    {
        window.SourceInitialized += (_, _) =>
        {
            var hwnd = new WindowInteropHelper(window).Handle;
            var captionRef = ToColorRef(caption);
            var textRef = ToColorRef(text);
            DwmSetWindowAttribute(hwnd, DwmwaCaptionColor, ref captionRef, sizeof(int));
            DwmSetWindowAttribute(hwnd, DwmwaTextColor, ref textRef, sizeof(int));
        };
    }

    // COLORREF is 0x00BBGGRR, not the 0x00RRGGBB a System.Windows.Media.Color reads as.
    private static int ToColorRef(Color c) => c.R | (c.G << 8) | (c.B << 16);
}
