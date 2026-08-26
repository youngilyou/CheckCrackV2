using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using CheckCrackViewer.Models;
using CheckCrackViewer.Services;
using Microsoft.Win32;

namespace CheckCrackViewer.ViewModels;

/// <summary>"원본 AI" 화면: 스티칭 전 원본 사진을 한 장씩 넘겨보며 YOLO 크랙 후보를
/// 체크박스로 켜고 끄는 빠른 확인용 도구. 정밀 측정(길이/폭/스켈레톤)은 스티칭된
/// 모자이크 경로(MainViewModel.DetectCracksCommand)가 맡고, 여기는 "여기 크랙 후보가
/// 있다"만 빠르게 훑어보는 용도 -- AiTrainingViewModel과 마찬가지로 독립된 도구.</summary>
public partial class OriginalAiViewModel : ObservableObject
{
    private const int MaxDisplayDim = 1600;

    public string RootPath { get; set; } = "";

    [ObservableProperty] private string _selectedFolderPath = "";
    [ObservableProperty] private List<string> _imageList = new();
    [ObservableProperty] private int _currentImageIndex = -1;
    [ObservableProperty] private string _imagePath = "";
    [ObservableProperty] private BitmapImage? _displayBitmap;
    [ObservableProperty] private int _origWidth;
    [ObservableProperty] private int _origHeight;
    [ObservableProperty] private double _displayWidth;
    [ObservableProperty] private double _displayHeight;
    [ObservableProperty] private double _scale = 1.0;
    [ObservableProperty] private bool _showCrackOverlay;
    [ObservableProperty] private bool _isDetecting;
    [ObservableProperty] private string _statusText = "\"이미지 폴더 열기\"로 원본 사진 폴더를 선택하세요.";

    /// <summary>식별 보조 보기 (원본/그림자 보정/이진화/스켈레톤/윤곽선) -- 균열 색이 벽과
    /// 비슷해 식별이 어려울 때만 참고하는 화면 표시 전용 기능. tools/identify_view.py가
    /// 만든 이미지로 DisplayBitmap만 바꿔치기할 뿐, OverlayBoxes(크랙 판단 영역 표시)는
    /// 항상 원본 이미지 좌표계 그대로 -- 어떤 보기를 보고 있든 오버레이 좌표는 바뀌지
    /// 않는다.</summary>
    [ObservableProperty] private string _viewMode = "original";
    [ObservableProperty] private bool _isBuildingView;
    [ObservableProperty] private string _viewModeError = "";

    public IReadOnlyList<ViewModeOption> ViewModeOptions => IdentifyViewClient.Options;

    partial void OnViewModeChanged(string value) => _ = ApplyViewModeAsync();

    public ObservableCollection<CrackBoxItem> OverlayBoxes { get; } = new();

    public bool HasImage => DisplayBitmap != null;
    public bool HasImageList => ImageList.Count > 0;
    public string ImageListLabel => HasImageList ? $"{CurrentImageIndex + 1} / {ImageList.Count}" : "";

    // tools/detect_cracks_images.py가 쓰는 output/originals_cracks.json을 image_id별로
    // 캐싱 -- 이전/다음 넘길 때마다 파일을 다시 읽지 않음.
    private readonly Dictionary<string, List<List<CrackPolygonPoint>>> _detectionsByImageId = new();

    partial void OnCurrentImageIndexChanged(int value)
    {
        OnPropertyChanged(nameof(ImageListLabel));
        PreviousImageCommand.NotifyCanExecuteChanged();
        NextImageCommand.NotifyCanExecuteChanged();
    }

    partial void OnImageListChanged(List<string> value)
    {
        OnPropertyChanged(nameof(HasImageList));
        OnPropertyChanged(nameof(ImageListLabel));
        PreviousImageCommand.NotifyCanExecuteChanged();
        NextImageCommand.NotifyCanExecuteChanged();
        DetectCracksCommand.NotifyCanExecuteChanged();
    }

    partial void OnSelectedFolderPathChanged(string value) => DetectCracksCommand.NotifyCanExecuteChanged();

    partial void OnShowCrackOverlayChanged(bool value) => RebuildOverlay();

    private bool CanGoPrevious() => CurrentImageIndex > 0;
    private bool CanGoNext() => CurrentImageIndex >= 0 && CurrentImageIndex < ImageList.Count - 1;

    [RelayCommand(CanExecute = nameof(CanGoPrevious))]
    private void PreviousImage()
    {
        CurrentImageIndex--;
        LoadImage(ImageList[CurrentImageIndex]);
    }

    [RelayCommand(CanExecute = nameof(CanGoNext))]
    private void NextImage()
    {
        CurrentImageIndex++;
        LoadImage(ImageList[CurrentImageIndex]);
    }

    [RelayCommand]
    private void SelectImageFolder()
    {
        var dialog = new OpenFolderDialog
        {
            Title = "이미지 폴더 선택",
            InitialDirectory = Directory.Exists(RootPath) ? RootPath : @"D:\",
        };
        if (dialog.ShowDialog() != true)
            return;

        SelectedFolderPath = dialog.FolderName;
        var files = new[] { "*.jpg", "*.jpeg" }
            .SelectMany(pattern => Directory.GetFiles(dialog.FolderName, pattern))
            .OrderBy(p => p, StringComparer.OrdinalIgnoreCase)
            .ToList();
        ImageList = files;

        _detectionsByImageId.Clear();
        LoadDetectionsIfPresent(dialog.FolderName);

        if (files.Count > 0)
        {
            CurrentImageIndex = 0;
            LoadImage(files[0]);
        }
        else
        {
            CurrentImageIndex = -1;
            DisplayBitmap = null;
            OverlayBoxes.Clear();
            OnPropertyChanged(nameof(HasImage));
        }
        StatusText = $"선택됨: {SelectedFolderPath}  ({files.Count}장)";
    }

    private bool CanDetectCracks() => !IsDetecting && !string.IsNullOrEmpty(SelectedFolderPath);

    /// <summary>폴더 전체를 한 번에 처리 -- 사진 넘길 때마다 즉석 실행하지 않는다
    /// (사용자 확인 사항). RunFacade와 동일한 Process 실행 패턴.</summary>
    [RelayCommand(CanExecute = nameof(CanDetectCracks))]
    private async Task DetectCracks()
    {
        if (string.IsNullOrEmpty(SelectedFolderPath))
            return;

        IsDetecting = true;
        StatusText = "크랙 탐지 실행 중…";
        try
        {
            var scriptPath = Path.Combine(RootPath, "tools", "detect_cracks_images.py");
            var psi = new ProcessStartInfo
            {
                FileName = PythonEnvironment.DiscoverPythonExe(),
                WorkingDirectory = RootPath,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            psi.ArgumentList.Add(scriptPath);
            psi.ArgumentList.Add(SelectedFolderPath);

            using var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
            process.Start();
            ChildProcessRegistry.Register(process);
            try
            {
                var stderrTask = process.StandardError.ReadToEndAsync();
                var stdoutTask = process.StandardOutput.ReadToEndAsync();
                await process.WaitForExitAsync();

                if (process.ExitCode != 0)
                {
                    var stderr = await stderrTask;
                    var firstLine = stderr.Split('\n').FirstOrDefault(l => !string.IsNullOrWhiteSpace(l)) ?? "(no stderr output)";
                    StatusText = $"크랙 탐지 실패 (exit {process.ExitCode}): {firstLine.Trim()}";
                    return;
                }
            }
            finally
            {
                ChildProcessRegistry.Unregister(process);
            }

            _detectionsByImageId.Clear();
            LoadDetectionsIfPresent(SelectedFolderPath);
            ShowCrackOverlay = true;
            RebuildOverlay();
            StatusText = $"크랙 탐지 완료 ({ImageList.Count}장) — 체크박스로 오버레이를 켜고 끌 수 있습니다.";
        }
        catch (Exception ex)
        {
            StatusText = $"크랙 탐지를 시작할 수 없습니다: {ex.Message}";
        }
        finally
        {
            IsDetecting = false;
        }
    }

    private void LoadDetectionsIfPresent(string folderPath)
    {
        var jsonPath = Path.Combine(folderPath, "output", "originals_cracks.json");
        if (!File.Exists(jsonPath))
            return;
        try
        {
            var json = File.ReadAllText(jsonPath);
            var entries = JsonSerializer.Deserialize<List<OriginalCracksEntry>>(json);
            if (entries == null)
                return;
            foreach (var entry in entries)
            {
                var polygons = entry.Detections
                    .Select(det => det.PolygonPx
                        .Where(pt => pt.Count >= 2)
                        .Select(pt => new CrackPolygonPoint((int)Math.Round(pt[0]), (int)Math.Round(pt[1])))
                        .ToList())
                    .Where(pts => pts.Count >= 3)
                    .ToList();
                _detectionsByImageId[entry.ImageId] = polygons;
            }
        }
        catch (JsonException)
        {
            // 탐지 중간에 저장된 파일이거나 아직 없음 -- 다음 재실행 때 다시 시도
        }
    }

    private void LoadImage(string path)
    {
        try
        {
            using (var stream = File.OpenRead(path))
            {
                var decoder = BitmapDecoder.Create(stream, BitmapCreateOptions.DelayCreation, BitmapCacheOption.None);
                var frame = decoder.Frames[0];
                OrigWidth = frame.PixelWidth;
                OrigHeight = frame.PixelHeight;
            }

            var scale = Math.Min(1.0, (double)MaxDisplayDim / Math.Max(OrigWidth, OrigHeight));
            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.DecodePixelWidth = (int)Math.Round(OrigWidth * scale);
            bitmap.UriSource = new Uri(path);
            bitmap.EndInit();
            bitmap.Freeze();

            ImagePath = path;
            DisplayBitmap = bitmap;
            Scale = scale;
            DisplayWidth = bitmap.PixelWidth;
            DisplayHeight = bitmap.PixelHeight;
            StatusText = $"{Path.GetFileName(path)} · {OrigWidth}×{OrigHeight}px · {Math.Round(scale * 100)}%로 표시 중";
            RebuildOverlay();
            OnPropertyChanged(nameof(HasImage));
            if (ViewMode != "original")
                _ = ApplyViewModeAsync();
        }
        catch (Exception ex)
        {
            StatusText = $"이미지를 불러올 수 없습니다: {ex.Message}";
        }
    }

    /// <summary>ViewMode가 바뀌거나 새 이미지를 불러올 때 호출 -- 이미 캐시된 처리 결과가
    /// 있으면 즉시, 없으면 tools/identify_view.py를 실행한 뒤 DisplayBitmap만 바꾼다.
    /// OrigWidth/OrigHeight/Scale/DisplayWidth/DisplayHeight는 항상 원본 사진 기준으로
    /// LoadImage에서 이미 고정되어 있으므로 여기서 다시 계산하지 않는다.</summary>
    private async Task ApplyViewModeAsync()
    {
        if (string.IsNullOrEmpty(ImagePath))
            return;
        var mode = ViewMode;
        var path = ImagePath;
        ViewModeError = "";

        if (mode == "original")
        {
            SetDisplayBitmapFrom(path);
            return;
        }

        var alreadyCached = File.Exists(IdentifyViewClient.CachePath(path, mode));
        if (!alreadyCached)
            IsBuildingView = true;
        try
        {
            var (resultPath, error) = await IdentifyViewClient.GetOrBuildAsync(RootPath, path, mode);
            // 실행 중에 사용자가 다른 사진/모드로 이미 넘어갔으면 이 결과는 버린다.
            if (ImagePath != path || ViewMode != mode)
                return;
            if (error != null)
            {
                ViewModeError = $"보기 생성 실패: {error}";
                return;
            }
            SetDisplayBitmapFrom(resultPath!);
        }
        finally
        {
            IsBuildingView = false;
        }
    }

    private void SetDisplayBitmapFrom(string path)
    {
        var bitmap = new BitmapImage();
        bitmap.BeginInit();
        bitmap.CacheOption = BitmapCacheOption.OnLoad;
        bitmap.DecodePixelWidth = (int)Math.Round(DisplayWidth);
        bitmap.UriSource = new Uri(path);
        bitmap.EndInit();
        bitmap.Freeze();
        DisplayBitmap = bitmap;
    }

    private void RebuildOverlay()
    {
        OverlayBoxes.Clear();
        if (!ShowCrackOverlay || string.IsNullOrEmpty(ImagePath))
            return;

        var imageId = Path.GetFileNameWithoutExtension(ImagePath);
        if (!_detectionsByImageId.TryGetValue(imageId, out var polygons))
            return;

        foreach (var polygon in polygons)
        {
            var canvasPoints = new PointCollection();
            foreach (var p in polygon)
                canvasPoints.Add(new Point(p.X * Scale, p.Y * Scale));
            canvasPoints.Freeze();
            OverlayBoxes.Add(new CrackBoxItem { CanvasPoints = canvasPoints, OrigPoints = polygon });
        }
    }
}

file sealed class OriginalCracksEntry
{
    [JsonPropertyName("image_id")] public string ImageId { get; set; } = "";
    [JsonPropertyName("file_name")] public string FileName { get; set; } = "";
    [JsonPropertyName("width")] public int Width { get; set; }
    [JsonPropertyName("height")] public int Height { get; set; }
    [JsonPropertyName("detections")] public List<OriginalCrackDetection> Detections { get; set; } = new();
}

file sealed class OriginalCrackDetection
{
    [JsonPropertyName("polygon_px")] public List<List<double>> PolygonPx { get; set; } = new();
    [JsonPropertyName("confidence")] public double Confidence { get; set; }
}
