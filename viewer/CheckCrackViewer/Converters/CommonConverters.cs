using System;
using System.Globalization;
using System.Windows;
using System.Windows.Data;
using System.Windows.Media;

namespace CheckCrackViewer.Converters;

/// <summary>0.6628 -> "66.3%". Null (report field genuinely absent, e.g. no
/// calibration) renders as "—", never as 0% or a fabricated value.</summary>
public class RatioToPercentConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object parameter, CultureInfo culture)
    {
        if (value is double d)
            return $"{d * 100:0.#}%";
        return "—";
    }

    public object ConvertBack(object? value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotSupportedException();
}

/// <summary>Formats a nullable px measurement, e.g. 460.67 -> "460.7px".</summary>
public class PxValueConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object parameter, CultureInfo culture)
    {
        if (value is double d)
            return $"{d:0.#}px";
        return "—";
    }

    public object ConvertBack(object? value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotSupportedException();
}

/// <summary>value.ToString() == parameter.ToString() -> Visible, otherwise Collapsed
/// (or the reverse when Invert=True). Used for hamburger-menu page switching
/// (SelectedMenu == "analysis"/"training"/"settings") and AI 학습 탭's per-source panels
/// (SelectedSource == "labeled"/"raw_crops"/"stitched").</summary>
public class StringEqualsToVisibilityConverter : IValueConverter
{
    public bool Invert { get; set; }

    public object Convert(object? value, Type targetType, object parameter, CultureInfo culture)
    {
        var equal = string.Equals(value?.ToString(), parameter?.ToString(), StringComparison.Ordinal);
        if (Invert) equal = !equal;
        return equal ? Visibility.Visible : Visibility.Collapsed;
    }

    public object ConvertBack(object? value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotSupportedException();
}

/// <summary>values[0].ToString() == values[1].ToString() -> the sidebar's active-item
/// accent color, otherwise transparent. Used for hamburger-menu nav button highlighting
/// (ConverterParameter can't itself be data-bound in WPF, hence MultiBinding here instead
/// of the single-value StringEqualsToVisibilityConverter's ConverterParameter approach).</summary>
public class TwoStringsEqualToBrushConverter : IMultiValueConverter
{
    private static readonly Brush ActiveBrush = new SolidColorBrush(Color.FromRgb(0xFF, 0x6A, 0x3D));
    private static readonly Brush InactiveBrush = Brushes.Transparent;

    public object Convert(object?[] values, Type targetType, object parameter, CultureInfo culture)
        => values.Length == 2 && string.Equals(values[0]?.ToString(), values[1]?.ToString(), StringComparison.Ordinal)
            ? ActiveBrush
            : InactiveBrush;

    public object[] ConvertBack(object? value, Type[] targetTypes, object parameter, CultureInfo culture)
        => throw new NotSupportedException();
}

/// <summary>value.ToString() == parameter.ToString() -> true/false (plain bool, not
/// Visibility) -- for bindings like IsEnabled/IsHitTestVisible that
/// StringEqualsToVisibilityConverter can't target. Invert=True flips the sense, same
/// as the Visibility converter.</summary>
public class StringEqualsToBoolConverter : IValueConverter
{
    public bool Invert { get; set; }

    public object Convert(object? value, Type targetType, object parameter, CultureInfo culture)
    {
        var equal = string.Equals(value?.ToString(), parameter?.ToString(), StringComparison.Ordinal);
        return Invert ? !equal : equal;
    }

    public object ConvertBack(object? value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotSupportedException();
}

/// <summary>values[0] and values[1] are the SAME object instance -> the selection
/// accent brush, otherwise transparent. Used for the FACADES tree's facade-card
/// selection highlight (values[0]=this card's item, values[1]=SelectedFacade) --
/// unlike TwoStringsEqualToBrushConverter this compares by reference, not by
/// ToString(), since two different facades could share a display string.</summary>
public class ReferenceEqualsToBrushConverter : IMultiValueConverter
{
    private static readonly Brush ActiveBrush = new SolidColorBrush(Color.FromRgb(0xFF, 0x6A, 0x3D));
    private static readonly Brush InactiveBrush = Brushes.Transparent;

    public object Convert(object?[] values, Type targetType, object parameter, CultureInfo culture)
        => values.Length == 2 && values[0] != null && ReferenceEquals(values[0], values[1])
            ? ActiveBrush
            : InactiveBrush;

    public object[] ConvertBack(object? value, Type[] targetTypes, object parameter, CultureInfo culture)
        => throw new NotSupportedException();
}

/// <summary>true/non-null/non-empty -> Visible, otherwise Collapsed.</summary>
public class TruthyToVisibilityConverter : IValueConverter
{
    public bool Invert { get; set; }

    public object Convert(object? value, Type targetType, object parameter, CultureInfo culture)
    {
        bool truthy = value switch
        {
            null => false,
            bool b => b,
            string s => !string.IsNullOrEmpty(s),
            int i => i != 0,
            _ => true,
        };
        if (Invert) truthy = !truthy;
        return truthy ? Visibility.Visible : Visibility.Collapsed;
    }

    public object ConvertBack(object? value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotSupportedException();
}
