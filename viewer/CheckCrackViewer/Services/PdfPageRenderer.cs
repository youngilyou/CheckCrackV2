using System.IO;
using System.Windows.Media.Imaging;
using PDFtoImage;
using SkiaSharp;

namespace CheckCrackViewer.Services;

/// <summary>Renders PDF pages (e.g. tools/generate_report.py's output) to
/// WPF-displayable bitmaps for the 결과보기 screen's "보고서" panel. This is
/// independent of whatever generated the PDF (previously PDFsharp/MigraDoc in
/// C#, now WeasyPrint from src/report/pdf_report.py) -- PDFtoImage (wraps
/// PDFium, MIT license, no DevExpress-style .NET Framework lock-in) just
/// rasterizes an existing PDF file, it was never coupled to the generator.</summary>
public static class PdfPageRenderer
{
    public static int GetPageCount(string pdfPath)
    {
        using var stream = File.OpenRead(pdfPath);
        return Conversion.GetPageCount(stream);
    }

    public static BitmapSource RenderPage(string pdfPath, int pageIndex)
    {
        using var stream = File.OpenRead(pdfPath);
        using var bitmap = Conversion.ToImage(stream, pageIndex);
        using var data = bitmap.Encode(SKEncodedImageFormat.Png, 100);
        using var ms = new MemoryStream(data.ToArray());

        var image = new BitmapImage();
        image.BeginInit();
        image.CacheOption = BitmapCacheOption.OnLoad;
        image.StreamSource = ms;
        image.EndInit();
        image.Freeze();
        return image;
    }
}
