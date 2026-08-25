using System;
using System.Globalization;
using System.IO;
using System.Windows.Data;
using System.Windows.Media.Imaging;

namespace CheckCrackViewer.Converters;

/// <summary>Loads a facade mosaic TIFF (or any WIC-supported image) as a
/// downscaled thumbnail. Facade mosaics are tens of megapixels — decoding
/// at DecodePixelWidth keeps this to thumbnail cost instead of loading the
/// full original into memory just to shrink it in the UI.</summary>
public class PathToBitmapConverter : IValueConverter
{
    public int DecodePixelWidth { get; set; } = 700;

    public object? Convert(object? value, Type targetType, object parameter, CultureInfo culture)
    {
        if (value is not string path || string.IsNullOrEmpty(path) || !File.Exists(path))
            return null;
        try
        {
            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.DecodePixelWidth = DecodePixelWidth;
            bitmap.UriSource = new Uri(path);
            bitmap.EndInit();
            bitmap.Freeze();
            return bitmap;
        }
        catch
        {
            return null; // file mid-write or an unsupported variant — thumbnail just stays blank
        }
    }

    public object ConvertBack(object? value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotSupportedException();
}
