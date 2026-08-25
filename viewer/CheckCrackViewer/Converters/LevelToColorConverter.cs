using System;
using System.Globalization;
using System.Windows.Data;
using System.Windows.Media;

namespace CheckCrackViewer.Converters;

public class LevelToColorConverter : IValueConverter
{
    private static readonly SolidColorBrush Info = new(Color.FromRgb(0x9A, 0xA3, 0x9C));
    private static readonly SolidColorBrush Warning = new(Color.FromRgb(0xE0, 0xB1, 0x3B));
    private static readonly SolidColorBrush Error = new(Color.FromRgb(0xE8, 0x6A, 0x5E));

    public object Convert(object? value, Type targetType, object parameter, CultureInfo culture)
    {
        return (value as string)?.ToUpperInvariant() switch
        {
            "WARNING" => Warning,
            "ERROR" => Error,
            _ => Info,
        };
    }

    public object ConvertBack(object? value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotSupportedException();
}
