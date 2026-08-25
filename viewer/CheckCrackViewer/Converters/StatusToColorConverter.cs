using System;
using System.Globalization;
using System.Windows.Data;
using System.Windows.Media;
using CheckCrackViewer.Models;

namespace CheckCrackViewer.Converters;

public class StatusToColorConverter : IValueConverter
{
    public static readonly SolidColorBrush Unknown = new(Color.FromRgb(0x6B, 0x74, 0x6E));
    public static readonly SolidColorBrush InProgress = new(Color.FromRgb(0x4A, 0x8F, 0xE0));
    public static readonly SolidColorBrush Done = new(Color.FromRgb(0x4C, 0xAF, 0x6E));
    public static readonly SolidColorBrush NeedsReview = new(Color.FromRgb(0xE0, 0xB1, 0x3B));
    public static readonly SolidColorBrush Failed = new(Color.FromRgb(0xD3, 0x4A, 0x3D));

    public object Convert(object? value, Type targetType, object parameter, CultureInfo culture)
    {
        return (value as FacadeOverallStatus?) switch
        {
            FacadeOverallStatus.InProgress => InProgress,
            FacadeOverallStatus.Done => Done,
            FacadeOverallStatus.NeedsManualReview => NeedsReview,
            FacadeOverallStatus.Failed => Failed,
            _ => Unknown,
        };
    }

    public object ConvertBack(object? value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotSupportedException();
}

public class StatusToLabelConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object parameter, CultureInfo culture)
    {
        return (value as FacadeOverallStatus?) switch
        {
            FacadeOverallStatus.InProgress => "진행 중",
            FacadeOverallStatus.Done => "완료",
            FacadeOverallStatus.NeedsManualReview => "검토 필요",
            FacadeOverallStatus.Failed => "실패",
            _ => "대기",
        };
    }

    public object ConvertBack(object? value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotSupportedException();
}
