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
using System.Windows.Threading;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using CheckCrackViewer.Models;
using CheckCrackViewer.Services;
using Microsoft.Win32;

namespace CheckCrackViewer.ViewModels;

/// <summary>Native port of tools/crack_annotator/template.html -- draws crack
/// regions on a facade mosaic and exports them as JSON for YOLO training data
/// prep. The current export stores original-image polygon points directly.</summary>
public partial class AiTrainingViewModel : ObservableObject
{
    // Facade mosaics can be tens of thousands of px wide (see CLAUDE.local.md's
    // "30000x50000" example) -- cap on-screen display size the same way build.py's
    // MAX_DISPLAY_DIM does, decoding straight to that size so a huge TIFF is never
    // fully decoded at native resolution just to shrink it in the UI.
    private const int MaxDisplayDim = 1600;

    /// <summary>Set by MainViewModel (constructor + OnRootPathChanged) -- this
    /// ViewModel doesn't own RootPath itself, it just needs it to find
    /// training/crack_seg/*.py and tools/detect_cracks_images.py.</summary>
    public string RootPath { get; set; } = "";

    [ObservableProperty] private bool _isProcessing;
    [ObservableProperty] private string _progressText = "";
    [ObservableProperty] private bool _hasFailed;
    [ObservableProperty] private string _failureLog = "";

    [ObservableProperty] private bool _isTraining;
    [ObservableProperty] private string _trainingStatusText = "";
    /// <summary>운용자가 학습 시작 전에 직접 입력하는 산출물 이름 (2026-08-28).
    /// 이전엔 $"{source}_{mode}_{timestamp}"로 완전 자동 생성했는데, 나중에
    /// logs/training_runs/나 runs/ 목록에서 어떤 학습이 무엇을 위한 것이었는지
    /// 알아볼 수 없다는 문제가 있었음. 타임스탬프는 계속 붙여서 같은 이름을
    /// 재사용해도 기존 run과 충돌/덮어쓰기가 나지 않게 함 (RunTraining 참고).
    /// CanTrain이 이 값을 비어있지 않아야 하는 조건으로 요구.</summary>
    [ObservableProperty] private string _trainingOutputName = "";
    [ObservableProperty] private int _trainingEpoch;
    [ObservableProperty] private int _trainingEpochs;
    [ObservableProperty] private string _metricsText = "";
    [ObservableProperty] private string _trainingErrorText = "";
    // True once a training run has reached a terminal state (done or error) --
    // distinct from MetricsText being non-empty so a FAILED run also leaves a
    // persistent, visible result instead of just flashing inside the
    // "학습 진행 중" overlay and then disappearing once IsTraining goes false.
    [ObservableProperty] private bool _hasTrainingResult;

    /// <summary>Two independent training sources (see the AI 학습 탭 plan): each
    /// gets its own dataset dir/model runs so results are never silently mixed,
    /// which is exactly the "디버깅을 수월하게" requirement this was built for.
    /// "labeled" = already-masked source folder (no polygon drawing needed),
    /// "raw_crops" = plain unlabeled crack photos (polygon drawing, many images via
    /// Next/Back).</summary>
    [ObservableProperty] private string _selectedSource = "labeled";
    [ObservableProperty] private bool _isLabeledSelected = true;
    [ObservableProperty] private bool _isRawCropsSelected;

    private bool _syncingSource;

    partial void OnIsLabeledSelectedChanged(bool value) => HandleSourceCheckboxChanged(value, "labeled");
    partial void OnIsRawCropsSelectedChanged(bool value) => HandleSourceCheckboxChanged(value, "raw_crops");

    /// <summary>Two CheckBoxes behaving like a radio group (user's explicit request:
    /// "체크박스로 선택" but only one active source at a time) -- checking one
    /// unchecks the other; unchecking the active one just re-checks itself,
    /// since leaving zero sources selected has no meaning here.</summary>
    private void HandleSourceCheckboxChanged(bool value, string source)
    {
        if (_syncingSource)
            return;
        _syncingSource = true;
        try
        {
            if (value)
                SelectedSource = source;
            else if (SelectedSource != source)
                return; // some other source is active; nothing to reconcile
            // else: value==false on the currently-active source -- falls through
            // and re-asserts it below, since it can't be left unchecked.

            IsLabeledSelected = SelectedSource == "labeled";
            IsRawCropsSelected = SelectedSource == "raw_crops";
        }
        finally
        {
            _syncingSource = false;
        }
        if (value)
            OnSourceSelected(source);
    }

    /// <summary>Resets per-image/region state when switching sources -- each source's
    /// data is independent, so nothing from the previous source's screen should
    /// linger.</summary>
    private void OnSourceSelected(string source)
    {
        HasFailed = false;
        FailureLog = "";
        Boxes.Clear();
        DisplayBitmap = null;
        MaskOverlayBitmap = null;
        ImageList = new List<string>();
        CurrentImageIndex = -1;
        SelectedFolderPath = "";
        MetricsText = "";
        ZoomFactor = 1.0;
        ShowAiCrackOverlay = false;
        AiDetectedBoxes.Clear();
        _aiDetectionsByImageId.Clear();
        NotifyImageViewStateChanged();

        StatusText = source switch
        {
            "labeled" => "\"이미지 폴더 열기\"로 기존 마스크 데이터셋 폴더를 선택하세요.",
            _ => "\"이미지 폴더 열기\"로 일반 크랙 사진 폴더를 선택하세요.",
        };

        PrepareLabeledDatasetCommand.NotifyCanExecuteChanged();
        SaveTrainingDataCommand.NotifyCanExecuteChanged();
        DetectCracksCommand.NotifyCanExecuteChanged();
    }

    private void NotifyImageViewStateChanged()
    {
        OnPropertyChanged(nameof(HasImage));
        OnPropertyChanged(nameof(CanUseZoom));
        OnPropertyChanged(nameof(IsZoomed));
        OnPropertyChanged(nameof(ZoomedDisplayWidth));
        OnPropertyChanged(nameof(ZoomedDisplayHeight));
        OnPropertyChanged(nameof(ZoomPercentText));
        ZoomInCommand.NotifyCanExecuteChanged();
        ZoomOutCommand.NotifyCanExecuteChanged();
        ResetZoomCommand.NotifyCanExecuteChanged();
    }

    [ObservableProperty] private BitmapSource? _maskOverlayBitmap;
    [ObservableProperty] private List<string> _imageList = new();
    [ObservableProperty] private int _currentImageIndex = -1;

    public bool HasImageList => ImageList.Count > 0;
    public string ImageListLabel => HasImageList ? $"{CurrentImageIndex + 1} / {ImageList.Count}" : "";

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
    }

    private bool CanGoPrevious() => CurrentImageIndex > 0;
    private bool CanGoNext() => CurrentImageIndex >= 0 && CurrentImageIndex < ImageList.Count - 1;

    [RelayCommand(CanExecute = nameof(CanGoPrevious))]
    private void PreviousImage()
    {
        if (!ConfirmDiscardUnsavedBoxes())
            return;
        CurrentImageIndex--;
        LoadImageForCurrentSource();
    }

    [RelayCommand(CanExecute = nameof(CanGoNext))]
    private void NextImage()
    {
        if (!ConfirmDiscardUnsavedBoxes())
            return;
        CurrentImageIndex++;
        LoadImageForCurrentSource();
    }

    /// <summary>Tracks whether Boxes has changed since the last successful
    /// "학습 데이터 저장" (or since the current image was loaded) -- set in
    /// RenumberAndUpdate (every AddPolygon/UndoLast/ClearAll/RemoveBox funnels through
    /// it), cleared by SaveTrainingData and by loading a new image.</summary>
    private bool _boxesDirty;

    /// <summary>raw_crops annotates one image at a time via Next/Back -- warn before
    /// silently losing regions that were drawn but never saved, reusing ClearAll's
    /// existing confirm-dialog pattern rather than adding auto-save. Only fires when
    /// there's something un-saved (not just whenever Boxes is non-empty), so
    /// navigating right after a successful save doesn't false-positive.</summary>
    private bool ConfirmDiscardUnsavedBoxes()
    {
        if (SelectedSource == "labeled" || !_boxesDirty)
            return true;
        return MessageBox.Show($"저장하지 않은 영역 {Boxes.Count}개가 있습니다. 이동하면 사라집니다. 계속할까요?",
            "저장 안 됨", MessageBoxButton.YesNo, MessageBoxImage.Warning) == MessageBoxResult.Yes;
    }

    private void LoadImageForCurrentSource()
    {
        if (CurrentImageIndex < 0 || CurrentImageIndex >= ImageList.Count)
            return;
        var path = ImageList[CurrentImageIndex];
        if (SelectedSource == "labeled")
            LoadLabeledPreview(path);
        else
            LoadImage(path);
    }

    partial void OnIsProcessingChanged(bool value) => NotifyBusyCommands();
    partial void OnIsTrainingChanged(bool value) => NotifyBusyCommands();
    partial void OnIsDetectingCracksChanged(bool value) => NotifyBusyCommands();

    private void NotifyBusyCommands()
    {
        SelectImageFolderCommand.NotifyCanExecuteChanged();
        PrepareLabeledDatasetCommand.NotifyCanExecuteChanged();
        StartTrainingCommand.NotifyCanExecuteChanged();
        StartFineTuneCommand.NotifyCanExecuteChanged();
        DetectCracksCommand.NotifyCanExecuteChanged();
    }

    partial void OnSelectedFolderPathChanged(string value)
    {
        PrepareLabeledDatasetCommand.NotifyCanExecuteChanged();
        DetectCracksCommand.NotifyCanExecuteChanged();
    }

    // Dataset prep (IsProcessing), training (IsTraining) and crack detection
    // (IsDetectingCracks) each shell out a python subprocess -- never let more
    // than one run at a time.
    private bool CanRunPipeline() => !IsProcessing && !IsTraining && !IsDetectingCracks;
    private bool CanPrepareLabeledDataset() => !IsProcessing && !IsTraining && !IsDetectingCracks && SelectedSource == "labeled";
    private bool CanTrain() => !IsProcessing && !IsTraining && !IsDetectingCracks && !string.IsNullOrWhiteSpace(TrainingOutputName);

    partial void OnTrainingOutputNameChanged(string value)
    {
        StartTrainingCommand.NotifyCanExecuteChanged();
        StartFineTuneCommand.NotifyCanExecuteChanged();
    }
    private bool CanDetectCracks() => !IsProcessing && !IsTraining && !IsDetectingCracks && !string.IsNullOrEmpty(SelectedFolderPath);

    /// <summary>Set by SelectImageFolder, consumed by DetectCracks/PrepareLabeledDataset/
    /// SaveTrainingData.</summary>
    [ObservableProperty] private string _selectedFolderPath = "";

    [ObservableProperty] private string _imagePath = "";
    [ObservableProperty] private BitmapImage? _displayBitmap;
    [ObservableProperty] private int _origWidth;
    [ObservableProperty] private int _origHeight;
    [ObservableProperty] private double _displayWidth;
    [ObservableProperty] private double _displayHeight;
    [ObservableProperty] private double _scale = 1.0;
    [ObservableProperty] private double _zoomFactor = 1.0;
    [ObservableProperty] private string _statusText = "이미지를 선택하세요.";
    [ObservableProperty] private string _copyStatus = "";
    [ObservableProperty] private string _saveStatus = "";
    [ObservableProperty] private string _jsonPreview = "";

    public ObservableCollection<CrackBoxItem> Boxes { get; } = new();

    /// <summary>"크랙 탐지" (원본 AI 화면과 동일한 실행 기능): 현재 선택된 폴더 전체에
    /// tools/detect_cracks_images.py를 한 번 돌려서 YOLO 크랙 후보를 오버레이로 보여준다.
    /// Boxes(수동으로 그린 학습용 영역)와는 완전히 별개의 읽기 전용 오버레이 -- AI 탐지
    /// 결과를 참고만 하고, 실제 학습 라벨은 여전히 사람이 직접 그린 Boxes로 저장된다.</summary>
    [ObservableProperty] private bool _isDetectingCracks;
    [ObservableProperty] private bool _showAiCrackOverlay;

    public ObservableCollection<CrackBoxItem> AiDetectedBoxes { get; } = new();

    // tools/detect_cracks_images.py가 쓰는 output/originals_cracks.json을 image_id별로
    // 캐싱 -- 이전/다음 넘길 때마다 파일을 다시 읽지 않음 (OriginalAiViewModel과 동일한 패턴).
    private readonly Dictionary<string, List<List<CrackPolygonPoint>>> _aiDetectionsByImageId = new();

    partial void OnShowAiCrackOverlayChanged(bool value) => RebuildAiOverlay();

    /// <summary>식별 보조 보기 (원본/그림자 보정/이진화/스켈레톤/윤곽선) -- 균열 색이 벽과
    /// 비슷해 식별이 어려울 때만 참고하는 화면 표시 전용 기능 (OriginalAiViewModel과 동일한
    /// IdentifyViewClient 사용). Boxes/AiDetectedBoxes(크랙 판단 영역 표시)는 항상 원본
    /// 이미지 좌표계 그대로 -- 어떤 보기를 보고 있든 오버레이 좌표는 바뀌지 않는다.</summary>
    [ObservableProperty] private string _viewMode = "original";
    [ObservableProperty] private bool _isBuildingView;
    [ObservableProperty] private string _viewModeError = "";

    public IReadOnlyList<ViewModeOption> ViewModeOptions => IdentifyViewClient.Options;

    partial void OnViewModeChanged(string value) => _ = ApplyViewModeAsync();

    public bool HasImage => DisplayBitmap != null;
    public bool CanUseZoom => HasImage && SelectedSource != "labeled";
    public bool IsZoomed => CanUseZoom && Math.Abs(ZoomFactor - 1.0) > 0.001;
    public double ZoomedDisplayWidth => DisplayWidth * ZoomFactor;
    public double ZoomedDisplayHeight => DisplayHeight * ZoomFactor;
    public string ZoomPercentText => $"{ZoomFactor * 100:0}%";
    public string FacadeId => string.IsNullOrEmpty(ImagePath) ? "" : Path.GetFileNameWithoutExtension(ImagePath);
    public string SourceImageName => string.IsNullOrEmpty(ImagePath) ? "" : Path.GetFileName(ImagePath);

    partial void OnZoomFactorChanged(double value)
    {
        OnPropertyChanged(nameof(IsZoomed));
        OnPropertyChanged(nameof(ZoomedDisplayWidth));
        OnPropertyChanged(nameof(ZoomedDisplayHeight));
        OnPropertyChanged(nameof(ZoomPercentText));
        ZoomInCommand.NotifyCanExecuteChanged();
        ZoomOutCommand.NotifyCanExecuteChanged();
        ResetZoomCommand.NotifyCanExecuteChanged();
    }

    [RelayCommand(CanExecute = nameof(CanZoomIn))]
    private void ZoomIn() => ZoomFactor = Math.Min(6.0, Math.Round((ZoomFactor + 0.25) * 100) / 100);

    [RelayCommand(CanExecute = nameof(CanZoomOut))]
    private void ZoomOut() => ZoomFactor = Math.Max(1.0, Math.Round((ZoomFactor - 0.25) * 100) / 100);

    [RelayCommand(CanExecute = nameof(CanResetZoom))]
    private void ResetZoom() => ZoomFactor = 1.0;

    public void ApplyZoomDelta(int wheelDelta)
    {
        if (!CanUseZoom)
            return;
        if (wheelDelta > 0 && CanZoomIn())
            ZoomIn();
        else if (wheelDelta < 0 && CanZoomOut())
            ZoomOut();
    }

    private bool CanZoomIn() => CanUseZoom && ZoomFactor < 6.0;
    private bool CanZoomOut() => CanUseZoom && ZoomFactor > 1.0;
    private bool CanResetZoom() => IsZoomed;

    /// <summary>Picks a folder and immediately lists its images. "labeled" validates
    /// mask pairing; "raw_crops" just lists images for Next/Back polygon drawing.
    /// Either way, also (re)loads any existing tools/detect_cracks_images.py output
    /// for this folder so a previously-run 크랙 탐지 overlay survives re-opening the
    /// same folder.</summary>
    [RelayCommand(CanExecute = nameof(CanRunPipeline))]
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
        _aiDetectionsByImageId.Clear();
        LoadAiDetectionsIfPresent(dialog.FolderName);

        if (SelectedSource == "labeled")
        {
            var files = GetLabeledImageCandidates(dialog.FolderName).ToList();
            ImageList = files;
            if (files.Count > 0)
            {
                CurrentImageIndex = 0;
                LoadImageForCurrentSource();
            }
            else
            {
                CurrentImageIndex = -1;
                DisplayBitmap = null;
                MaskOverlayBitmap = null;
                NotifyImageViewStateChanged();
            }
            var validPairs = files.Count(p => File.Exists(Path.ChangeExtension(p, ".png")));
            StatusText = $"선택됨: {SelectedFolderPath}  ({files.Count}장, 마스크 쌍 {validPairs}장 -- \"데이터셋 준비\"로 검증/변환)";
            return;
        }

        var rawFiles = new[] { "*.jpg", "*.jpeg", "*.png" }
            .SelectMany(pattern => Directory.GetFiles(dialog.FolderName, pattern))
            .OrderBy(p => p, StringComparer.OrdinalIgnoreCase)
            .ToList();
        ImageList = rawFiles;
        if (rawFiles.Count > 0)
        {
            CurrentImageIndex = 0;
            LoadImageForCurrentSource();
        }
        else
        {
            CurrentImageIndex = -1;
        }
        StatusText = $"선택됨: {dialog.FolderName}  ({rawFiles.Count}장 -- 영역을 그리고 저장하며 Next로 넘어가세요)";
    }

    /// <summary>"labeled" source's own prepare step: runs prepare_labeled_dataset.py to
    /// (re)generate dataset_labeled/ from datasets/CUBIT-Seg/crack_org. Deterministic
    /// and safe to re-run any time (crack_org's source images never change).</summary>
    [RelayCommand(CanExecute = nameof(CanPrepareLabeledDataset))]
    private async Task PrepareLabeledDataset()
    {
        if (!ValidateLabeledDatasetFolder(SelectedFolderPath, out var validationError))
        {
            HasFailed = true;
            FailureLog = validationError;
            StatusText = "데이터셋 준비 실패: 기존 마스크 데이터셋 조건이 맞지 않습니다.";
            return;
        }

        IsProcessing = true;
        HasFailed = false;
        FailureLog = "";
        ProgressText = "데이터셋 준비 중...";
        StatusText = "기존 마스크 데이터셋 준비 중 (마스크 → YOLO 폴리곤 변환)...";

        try
        {
            var scriptPath = Path.Combine(RootPath, "training", "crack_seg", "prepare_labeled_dataset.py");
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
            psi.ArgumentList.Add("--input");
            psi.ArgumentList.Add(SelectedFolderPath);

            using var process = new Process { StartInfo = psi };
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
                    HasFailed = true;
                    FailureLog = firstLine.Trim();
                    StatusText = $"데이터셋 준비 실패 (exit {process.ExitCode})";
                }
                else
                {
                    var stdout = await stdoutTask;
                    StatusText = "기존 마스크 데이터셋 준비 완료. " + stdout.Split('\n').LastOrDefault(l => !string.IsNullOrWhiteSpace(l))?.Trim();
                }
            }
            finally
            {
                ChildProcessRegistry.Unregister(process);
            }
        }
        catch (Exception ex)
        {
            HasFailed = true;
            FailureLog = ex.ToString();
            StatusText = $"데이터셋 준비를 시작할 수 없습니다: {ex.Message}";
        }
        finally
        {
            IsProcessing = false;
        }
    }

    /// <summary>"원본 AI" 화면(OriginalAiViewModel.DetectCracks)과 동일한 실행 기능:
    /// 현재 선택된 폴더 전체에 tools/detect_cracks_images.py를 한 번 돌려서
    /// output/originals_cracks.json을 만들고 그 결과를 오버레이로 보여준다. 학습용
    /// 수동 라벨(Boxes)과는 독립적인 읽기 전용 참고 오버레이일 뿐, 저장되는 학습
    /// 데이터에는 영향을 주지 않는다.</summary>
    [RelayCommand(CanExecute = nameof(CanDetectCracks))]
    private async Task DetectCracks()
    {
        if (string.IsNullOrEmpty(SelectedFolderPath))
            return;

        IsDetectingCracks = true;
        StatusText = "크랙 탐지 실행 중...";
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

            using var process = new Process { StartInfo = psi };
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

            _aiDetectionsByImageId.Clear();
            LoadAiDetectionsIfPresent(SelectedFolderPath);
            ShowAiCrackOverlay = true;
            RebuildAiOverlay();
            StatusText = $"크랙 탐지 완료 ({ImageList.Count}장) -- 체크박스로 오버레이를 켜고 끌 수 있습니다.";
        }
        catch (Exception ex)
        {
            StatusText = $"크랙 탐지를 시작할 수 없습니다: {ex.Message}";
        }
        finally
        {
            IsDetectingCracks = false;
        }
    }

    private void LoadAiDetectionsIfPresent(string folderPath)
    {
        var jsonPath = Path.Combine(folderPath, "output", "originals_cracks.json");
        if (!File.Exists(jsonPath))
            return;
        try
        {
            var json = File.ReadAllText(jsonPath);
            var entries = JsonSerializer.Deserialize<List<AiCracksEntry>>(json);
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
                _aiDetectionsByImageId[entry.ImageId] = polygons;
            }
        }
        catch (JsonException)
        {
            // 탐지 중간에 저장된 파일이거나 아직 없음 -- 다음 재실행 때 다시 시도
        }
    }

    private void RebuildAiOverlay()
    {
        AiDetectedBoxes.Clear();
        if (!ShowAiCrackOverlay || string.IsNullOrEmpty(ImagePath))
            return;

        var imageId = Path.GetFileNameWithoutExtension(ImagePath);
        if (!_aiDetectionsByImageId.TryGetValue(imageId, out var polygons))
            return;

        foreach (var polygon in polygons)
        {
            var canvasPoints = new PointCollection();
            foreach (var p in polygon)
                canvasPoints.Add(new Point(p.X * Scale, p.Y * Scale));
            canvasPoints.Freeze();
            AiDetectedBoxes.Add(new CrackBoxItem { CanvasPoints = canvasPoints, OrigPoints = polygon });
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

    [RelayCommand]
    private void DismissFailure() => HasFailed = false;

    /// <summary>새로 만들 때: yolov8s-seg.pt (COCO-pretrained) 기준으로 처음부터 학습.</summary>
    [RelayCommand(CanExecute = nameof(CanTrain))]
    private Task StartTraining() => RunTraining("new");

    /// <summary>기존 것에 추가할 때: training/crack_seg/runs의 가장 최근 best.pt에서
    /// 이어서 학습 (find_latest_checkpoint, train_from_viewer.py 쪽 로직).</summary>
    [RelayCommand(CanExecute = nameof(CanTrain))]
    private Task StartFineTune() => RunTraining("finetune");

    /// <summary>Runs training/crack_seg/train_from_viewer.py (별도 파일 -- train.py는
    /// 손대지 않음, 위 RunPipelineFromFolder와 같은 이유). 실행마다 고유 run id를 만들고
    /// logs/training_runs/<run_id>/status.json을 폴링한다. 전체 이력은 같은 폴더의
    /// events.jsonl/summary.json에 남기므로 이전 학습 로그를 덮어쓰지 않는다.</summary>
    private async Task RunTraining(string mode)
    {
        if (SelectedSource == "labeled" && !ValidateLabeledDatasetFolder(SelectedFolderPath, out var validationError))
        {
            TrainingErrorText = validationError;
            TrainingStatusText = validationError;
            HasTrainingResult = true;
            return;
        }

        // CanTrain already gates the buttons on this, but RunTraining is a plain
        // private method (not itself a guarded entry point) -- defensive check
        // mirrors the labeled-folder validation above.
        if (string.IsNullOrWhiteSpace(TrainingOutputName))
        {
            var message = "학습 산출물 이름을 입력하세요.";
            TrainingErrorText = message;
            TrainingStatusText = message;
            HasTrainingResult = true;
            return;
        }

        IsTraining = true;
        TrainingEpoch = 0;
        TrainingEpochs = 0;
        TrainingStatusText = "준비 중...";
        HasTrainingResult = false;
        MetricsText = "";
        TrainingErrorText = "";

        // 운용자가 입력한 이름을 산출물 식별자로 쓰되, ultralytics가 이 값을 그대로
        // runs/ 폴더명(project/name)과 logs/training_runs/<run_id>로 쓰므로
        // 경로에 쓸 수 없는 문자는 제거/치환 -- 타임스탬프를 뒤에 붙여 같은 이름을
        // 여러 번 써도 기존 run을 덮어쓰지 않게 함.
        var sanitizedOutputName = SanitizeForFileName(TrainingOutputName);
        var runId = $"{SelectedSource}_{mode}_{sanitizedOutputName}_{DateTime.Now:yyyyMMdd_HHmmss}";
        var statusPath = Path.Combine(RootPath, "logs", "training_runs", runId, "status.json");
        var pollTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        pollTimer.Tick += (_, _) => PollTrainingStatus(statusPath);
        pollTimer.Start();

        try
        {
            var scriptPath = Path.Combine(RootPath, "training", "crack_seg", "train_from_viewer.py");
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
            psi.ArgumentList.Add("--source");
            psi.ArgumentList.Add(SelectedSource);
            psi.ArgumentList.Add("--mode");
            psi.ArgumentList.Add(mode);
            psi.ArgumentList.Add("--run-id");
            psi.ArgumentList.Add(runId);
            if (SelectedSource == "labeled" && !string.IsNullOrWhiteSpace(SelectedFolderPath))
            {
                psi.ArgumentList.Add("--labeled-input");
                psi.ArgumentList.Add(SelectedFolderPath);
            }

            using var process = new Process { StartInfo = psi };
            process.Start();
            ChildProcessRegistry.Register(process);
            try
            {
                var stderrTask = process.StandardError.ReadToEndAsync();
                // ultralytics' training output is verbose enough that leaving stdout
                // unread risks filling the OS pipe buffer and deadlocking the child
                // process -- always drain it, same as DetectCracks/PrepareLabeledDataset.
                var stdoutTask = process.StandardOutput.ReadToEndAsync();
                await process.WaitForExitAsync();

                if (process.ExitCode != 0)
                {
                    // PollTrainingStatus (via the 1s timer) usually already picked up the
                    // script's own write_status(status="error", ...) by now -- but if it
                    // crashed before ever writing one (e.g. an argparse/import error), this
                    // is the fallback that still makes SOME failure reason visible.
                    PollTrainingStatus(statusPath);
                    if (!HasTrainingResult)
                    {
                        var stderr = await stderrTask;
                        var firstLine = stderr.Split('\n').FirstOrDefault(l => !string.IsNullOrWhiteSpace(l)) ?? "(no stderr output)";
                        TrainingErrorText = firstLine.Trim();
                        TrainingStatusText = $"학습 실패 (exit {process.ExitCode}): {firstLine.Trim()}";
                        HasTrainingResult = true;
                    }
                }
                else
                {
                    PollTrainingStatus(statusPath); // pick up the script's final "done" write
                }
            }
            finally
            {
                ChildProcessRegistry.Unregister(process);
            }
        }
        catch (Exception ex)
        {
            TrainingStatusText = $"학습을 시작할 수 없습니다: {ex.Message}";
            TrainingErrorText = ex.Message;
            HasTrainingResult = true;
        }
        finally
        {
            pollTimer.Stop();
            IsTraining = false;
        }
    }

    private void PollTrainingStatus(string path)
    {
        if (!File.Exists(path))
            return;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            var status = root.TryGetProperty("status", out var s) ? s.GetString() : null;
            TrainingEpoch = root.TryGetProperty("epoch", out var e) ? e.GetInt32() : 0;
            TrainingEpochs = root.TryGetProperty("epochs", out var t) ? t.GetInt32() : 0;
            TrainingStatusText = status switch
            {
                "preparing" => "학습 데이터 준비 중...",
                "training" => TrainingEpochs > 0 ? $"학습 중 ({TrainingEpoch}/{TrainingEpochs} epoch)" : "학습 중...",
                "validating" => "검증 중 (정확도 계산)...",
                "done" => "학습 완료" + (root.TryGetProperty("run_dir", out var rd) ? $" — {rd.GetString()}" : ""),
                "error" => root.TryGetProperty("error", out var err) ? err.GetString() ?? "오류" : "오류",
                _ => status ?? "",
            };

            if (status == "done" && root.TryGetProperty("metrics", out var metricsEl) && metricsEl.ValueKind == JsonValueKind.Object)
            {
                // Only the mask (segmentation) metrics matter here, not the box ones
                // ultralytics also reports internally -- "(M)" suffix picks those out.
                var lines = metricsEl.EnumerateObject()
                    .Where(p => p.Name.EndsWith("(M)"))
                    .Select(p => $"{FormatMetricName(p.Name)}: {p.Value.GetDouble():0.0000}");
                MetricsText = string.Join("\n", lines);
                TrainingErrorText = "";
                HasTrainingResult = true;
            }
            else if (status == "error")
            {
                MetricsText = "";
                TrainingErrorText = root.TryGetProperty("error", out var errText) ? errText.GetString() ?? "오류" : "오류";
                HasTrainingResult = true;
            }
            else if (status != "done")
            {
                MetricsText = "";
            }
        }
        catch (IOException)
        {
            // python process still mid-write this tick -- retry next poll
        }
        catch (JsonException)
        {
        }
    }

    /// <summary>"metrics/mAP50-95(M)" -> "mAP50-95" -- strips ultralytics' results_dict
    /// prefix/suffix noise for display, keeping just the metric name.</summary>
    private static string FormatMetricName(string key)
    {
        var name = key.StartsWith("metrics/") ? key["metrics/".Length..] : key;
        return name.EndsWith("(M)") ? name[..^3] : name;
    }

    private static IEnumerable<string> GetLabeledImageCandidates(string dir)
    {
        if (!Directory.Exists(dir))
            return [];
        return new[] { "*.jpg", "*.jpeg", "*.tif", "*.tiff", "*.bmp" }
            .SelectMany(pattern => Directory.GetFiles(dir, pattern))
            .OrderBy(p => p, StringComparer.OrdinalIgnoreCase);
    }

    /// <summary>TrainingOutputName은 ultralytics의 project/name(=runs 폴더명)과
    /// logs/training_runs/<run_id>에 그대로 쓰이므로, 경로에 쓸 수 없는 문자
    /// (Path.GetInvalidFileNameChars, 공백 포함)는 '_'로 치환. 전부 치환돼서
    /// 빈 문자열이 되면(예: 특수문자만 입력) "run"으로 대체.</summary>
    private static string SanitizeForFileName(string name)
    {
        var invalid = Path.GetInvalidFileNameChars();
        var chars = name.Trim().Select(c => invalid.Contains(c) || char.IsWhiteSpace(c) ? '_' : c).ToArray();
        var sanitized = new string(chars).Trim('_');
        return string.IsNullOrEmpty(sanitized) ? "run" : sanitized;
    }

    private bool ValidateLabeledDatasetFolder(string dir, out string error)
    {
        if (string.IsNullOrWhiteSpace(dir) || !Directory.Exists(dir))
        {
            error = "기존 마스크 데이터셋 폴더를 먼저 선택하세요.";
            return false;
        }

        var images = GetLabeledImageCandidates(dir).ToList();
        if (images.Count == 0)
        {
            error = "이미지 파일이 없습니다. 지원 확장자: .jpg, .jpeg, .tif, .tiff, .bmp";
            return false;
        }

        var missingMasks = images
            .Where(path => !File.Exists(Path.ChangeExtension(path, ".png")))
            .Take(10)
            .Select(Path.GetFileName)
            .ToList();
        if (missingMasks.Count > 0)
        {
            error = "같은 파일명(stem)의 .png 마스크가 없는 이미지가 있습니다:\n" + string.Join("\n", missingMasks);
            return false;
        }

        error = "";
        return true;
    }

    private string GetTrainingDataDir()
    {
        // Only "raw_crops" ever writes here (AddPolygon early-returns for "labeled",
        // the read-only mask-preview source), so a single fixed folder is enough now
        // that "stitched" no longer exists as a source.
        return Path.Combine(RootPath, "training_data_raw_crops");
    }

    private void LoadSavedAnnotationsForCurrentImage()
    {
        if (string.IsNullOrEmpty(ImagePath))
            return;
        var path = Path.Combine(GetTrainingDataDir(), $"{FacadeId}.json");
        if (!File.Exists(path))
            return;

        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            if (root.TryGetProperty("image_path", out var imagePathElement))
            {
                var savedImagePath = imagePathElement.GetString();
                if (!string.IsNullOrWhiteSpace(savedImagePath) &&
                    !Path.GetFullPath(savedImagePath).Equals(Path.GetFullPath(ImagePath), StringComparison.OrdinalIgnoreCase))
                    return;
            }

            var loaded = new List<CrackBoxItem>();
            if (root.TryGetProperty("regions", out var regions) && regions.ValueKind == JsonValueKind.Array)
            {
                foreach (var region in regions.EnumerateArray())
                {
                    if (!region.TryGetProperty("points", out var pointsElement) || pointsElement.ValueKind != JsonValueKind.Array)
                        continue;
                    var points = pointsElement.EnumerateArray()
                        .Select(p => new CrackPolygonPoint(p.GetProperty("x").GetInt32(), p.GetProperty("y").GetInt32()));
                    var item = BuildPolygonItemFromOrig(points);
                    if (item != null)
                        loaded.Add(item);
                }
            }
            else if (root.TryGetProperty("boxes", out var boxes) && boxes.ValueKind == JsonValueKind.Array)
            {
                foreach (var box in boxes.EnumerateArray())
                {
                    var x0 = box.GetProperty("x0").GetInt32();
                    var y0 = box.GetProperty("y0").GetInt32();
                    var x1 = box.GetProperty("x1").GetInt32();
                    var y1 = box.GetProperty("y1").GetInt32();
                    var item = BuildPolygonItemFromOrig(new[]
                    {
                        new CrackPolygonPoint(x0, y0),
                        new CrackPolygonPoint(x1, y0),
                        new CrackPolygonPoint(x1, y1),
                        new CrackPolygonPoint(x0, y1),
                    });
                    if (item != null)
                        loaded.Add(item);
                }
            }

            foreach (var item in loaded)
                Boxes.Add(item);
            RenumberAnnotations(markDirty: false);
            if (loaded.Count > 0)
                SaveStatus = $"불러옴: {path}";
        }
        catch (Exception ex)
        {
            SaveStatus = $"저장된 라벨을 읽을 수 없습니다: {ex.Message}";
        }
    }

    private void LoadImage(string path)
    {
        try
        {
            using (var stream = File.OpenRead(path))
            {
                // Header-only read (BitmapCacheOption.None + DelayCreation) to get the
                // TRUE original pixel size without decoding the full (possibly huge)
                // image -- mirrors build.py's cv2.imread(...).shape read, just via WIC.
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
            ZoomFactor = 1.0;
            Boxes.Clear();
            _boxesDirty = false;
            StatusText = $"{SourceImageName} · {OrigWidth}×{OrigHeight}px · {Math.Round(scale * 100)}%로 표시 중";
            NotifyImageViewStateChanged();
            OnPropertyChanged(nameof(FacadeId));
            OnPropertyChanged(nameof(SourceImageName));
            if (SelectedSource != "labeled")
                LoadSavedAnnotationsForCurrentImage();
            RebuildAiOverlay();
            UpdateJson();
            if (ViewMode != "original")
                _ = ApplyViewModeAsync();
        }
        catch (Exception ex)
        {
            StatusText = $"이미지를 열 수 없습니다: {ex.Message}";
        }
    }

    /// <summary>"labeled" source: read-only preview -- loads the base RGB image via the
    /// normal LoadImage, then overlays crack_org's own ground-truth mask (same stem,
    /// .png) as semi-transparent red so it's a QA view of existing labels, not a
    /// place to draw new ones (see AddPolygon's early-return for this source).</summary>
    private void LoadLabeledPreview(string path)
    {
        LoadImage(path);
        var maskPath = Path.ChangeExtension(path, ".png");
        MaskOverlayBitmap = BuildMaskOverlay(maskPath);
        if (MaskOverlayBitmap == null)
            StatusText += " · 마스크 없음";
    }

    /// <summary>Decodes a binary mask PNG at the same pixel width the base image was
    /// displayed at (so Stretch="Fill" lines the two Image controls up exactly) and
    /// recolors nonzero pixels as semi-transparent red.</summary>
    private BitmapSource? BuildMaskOverlay(string maskPath)
    {
        if (!File.Exists(maskPath))
            return null;
        try
        {
            var mask = new BitmapImage();
            mask.BeginInit();
            mask.CacheOption = BitmapCacheOption.OnLoad;
            mask.DecodePixelWidth = Math.Max(1, (int)DisplayWidth);
            mask.UriSource = new Uri(maskPath);
            mask.EndInit();
            var gray = new FormatConvertedBitmap(mask, PixelFormats.Gray8, null, 0);

            var w = gray.PixelWidth;
            var h = gray.PixelHeight;
            var pixels = new byte[w * h];
            gray.CopyPixels(pixels, w, 0);

            var bgra = new byte[w * h * 4];
            for (var i = 0; i < w * h; i++)
            {
                if (pixels[i] == 0)
                    continue;
                bgra[i * 4 + 2] = 255; // R
                bgra[i * 4 + 3] = 140; // A -- semi-transparent so the crack photo underneath stays visible
            }

            var overlay = BitmapSource.Create(w, h, 96, 96, PixelFormats.Bgra32, null, bgra, w * 4);
            overlay.Freeze();
            return overlay;
        }
        catch (Exception)
        {
            return null;
        }
    }

    /// <summary>Called from AiTrainingView's code-behind once a freehand region
    /// completes, in display-space pixels. The points are converted to source-image
    /// pixels and saved as the YOLO segmentation polygon.</summary>
    public void AddPolygon(IEnumerable<Point> displayPoints)
    {
        if (SelectedSource == "labeled")
            return; // read-only mask preview -- no manual annotation for this source

        var item = BuildPolygonItem(displayPoints);
        if (item == null)
            return;
        Boxes.Add(item);
        RenumberAndUpdate();
    }

    private CrackBoxItem? BuildPolygonItem(IEnumerable<Point> displayPoints)
    {
        var canvasPoints = new PointCollection();
        var origPoints = new List<CrackPolygonPoint>();
        CrackPolygonPoint? previous = null;

        foreach (var point in displayPoints)
        {
            var x = Math.Clamp(point.X, 0, Math.Max(0, DisplayWidth - 1));
            var y = Math.Clamp(point.Y, 0, Math.Max(0, DisplayHeight - 1));
            var orig = new CrackPolygonPoint(
                Math.Clamp((int)Math.Round(x / Scale), 0, Math.Max(0, OrigWidth - 1)),
                Math.Clamp((int)Math.Round(y / Scale), 0, Math.Max(0, OrigHeight - 1)));

            if (previous is not null && previous.X == orig.X && previous.Y == orig.Y)
                continue;

            canvasPoints.Add(new Point(x, y));
            origPoints.Add(orig);
            previous = orig;
        }

        if (origPoints.Count > 1 && origPoints[0].Equals(origPoints[^1]))
        {
            origPoints.RemoveAt(origPoints.Count - 1);
            canvasPoints.RemoveAt(canvasPoints.Count - 1);
        }

        if (origPoints.Count < 3)
            return null;
        if (origPoints.Max(p => p.X) - origPoints.Min(p => p.X) < 4 ||
            origPoints.Max(p => p.Y) - origPoints.Min(p => p.Y) < 4)
            return null;

        canvasPoints.Freeze();
        return new CrackBoxItem
        {
            CanvasPoints = canvasPoints,
            OrigPoints = origPoints,
            OrigAreaPx = CalculatePolygonArea(origPoints),
            OrigPerimeterPx = CalculatePolygonPerimeter(origPoints),
        };
    }

    private CrackBoxItem? BuildPolygonItemFromOrig(IEnumerable<CrackPolygonPoint> origPoints)
    {
        var canvasPoints = new PointCollection();
        var normalized = new List<CrackPolygonPoint>();
        CrackPolygonPoint? previous = null;

        foreach (var point in origPoints)
        {
            var orig = new CrackPolygonPoint(
                Math.Clamp(point.X, 0, Math.Max(0, OrigWidth - 1)),
                Math.Clamp(point.Y, 0, Math.Max(0, OrigHeight - 1)));
            if (previous is not null && previous.X == orig.X && previous.Y == orig.Y)
                continue;
            normalized.Add(orig);
            canvasPoints.Add(new Point(orig.X * Scale, orig.Y * Scale));
            previous = orig;
        }

        if (normalized.Count > 1 && normalized[0].Equals(normalized[^1]))
        {
            normalized.RemoveAt(normalized.Count - 1);
            canvasPoints.RemoveAt(canvasPoints.Count - 1);
        }

        if (normalized.Count < 3)
            return null;

        canvasPoints.Freeze();
        return new CrackBoxItem
        {
            CanvasPoints = canvasPoints,
            OrigPoints = normalized,
            OrigAreaPx = CalculatePolygonArea(normalized),
            OrigPerimeterPx = CalculatePolygonPerimeter(normalized),
        };
    }

    private static double CalculatePolygonArea(IReadOnlyList<CrackPolygonPoint> points)
    {
        if (points.Count < 3)
            return 0;

        double sum = 0;
        for (var i = 0; i < points.Count; i++)
        {
            var current = points[i];
            var next = points[(i + 1) % points.Count];
            sum += current.X * next.Y - next.X * current.Y;
        }
        return Math.Abs(sum) / 2.0;
    }

    private static double CalculatePolygonPerimeter(IReadOnlyList<CrackPolygonPoint> points)
    {
        if (points.Count < 2)
            return 0;

        double sum = 0;
        for (var i = 0; i < points.Count; i++)
        {
            var current = points[i];
            var next = points[(i + 1) % points.Count];
            var dx = current.X - next.X;
            var dy = current.Y - next.Y;
            sum += Math.Sqrt(dx * dx + dy * dy);
        }
        return sum;
    }

    [RelayCommand(CanExecute = nameof(HasBoxes))]
    private void UndoLast()
    {
        if (Boxes.Count == 0)
            return;
        Boxes.RemoveAt(Boxes.Count - 1);
        RenumberAndUpdate();
    }

    [RelayCommand(CanExecute = nameof(HasBoxes))]
    private void ClearAll()
    {
        if (Boxes.Count == 0)
            return;
        if (MessageBox.Show($"표시한 {Boxes.Count}개 영역을 모두 지울까요?", "전체 삭제",
                MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes)
            return;
        Boxes.Clear();
        RenumberAndUpdate();
    }

    [RelayCommand]
    private void RemoveBox(CrackBoxItem? box)
    {
        if (box == null)
            return;
        Boxes.Remove(box);
        RenumberAndUpdate();
    }

    private bool HasBoxes() => Boxes.Count > 0;

    private void RenumberAndUpdate()
    {
        RenumberAnnotations(markDirty: true);
    }

    private void RenumberAnnotations(bool markDirty)
    {
        _boxesDirty = true;
        if (!markDirty)
            _boxesDirty = false;
        for (var i = 0; i < Boxes.Count; i++)
            Boxes[i].Index = i + 1;
        UndoLastCommand.NotifyCanExecuteChanged();
        ClearAllCommand.NotifyCanExecuteChanged();
        CopyJsonCommand.NotifyCanExecuteChanged();
        SaveTrainingDataCommand.NotifyCanExecuteChanged();
        UpdateJson();
    }

    private void UpdateJson()
    {
        if (Boxes.Count == 0)
        {
            JsonPreview = "";
            return;
        }
        var payload = new
        {
            facade_id = FacadeId,
            source_image = SourceImageName,
            image_width_px = OrigWidth,
            image_height_px = OrigHeight,
            regions = Boxes.Select(b => new
            {
                region_id = b.Index,
                pixel_measurements = BuildViewerPixelMeasurements(b),
                points = b.OrigPoints.Select(p => new { x = p.X, y = p.Y }),
            }),
        };
        JsonPreview = JsonSerializer.Serialize(payload, new JsonSerializerOptions { WriteIndented = true });
    }

    private static object BuildViewerPixelMeasurements(CrackBoxItem box) => new
    {
        measurement_basis = "pixel_only_original_image_coordinates",
        measurement_warning = "Physical unit conversion requires preserved original image, camera metadata, capture distance/pose, and visible reference scale.",
        polygon_area_px = Math.Round(box.OrigAreaPx, 3),
        polygon_perimeter_px = Math.Round(box.OrigPerimeterPx, 3),
        bounding_width_px = box.OrigWidthPx,
        bounding_height_px = box.OrigHeightPx,
    };

    [RelayCommand(CanExecute = nameof(HasBoxes))]
    private void CopyJson()
    {
        try
        {
            Clipboard.SetText(JsonPreview);
            CopyStatus = $"{Boxes.Count}개 영역을 클립보드에 복사했습니다.";
        }
        catch (Exception ex)
        {
            CopyStatus = $"클립보드 복사 실패: {ex.Message}";
        }
    }

    /// <summary>Persists this image's polygon annotations to <source folder>/<facade_id>.json
    /// so multiple images' annotations accumulate into a real dataset on disk instead
    /// of only ever living in the clipboard. Unlike JsonPreview (which deliberately
    /// matches tools/crack_annotator/template.html's external export contract exactly),
    /// this file also carries image_path -- train_from_viewer.py needs it to find the
    /// actual pixels to crop, and this file is purely an internal artifact of this
    /// app's own training pipeline, not something any external tool reads.
    ///
    /// "raw_crops" writes into its own training_data_raw_crops folder
    /// (train_from_viewer.py's SOURCES dict) so it never mixes into the "labeled"
    /// source's dataset/model -- exactly the separation the AI 학습 탭 redesign was for.</summary>
    [RelayCommand(CanExecute = nameof(HasBoxes))]
    private void SaveTrainingData()
    {
        try
        {
            var dir = GetTrainingDataDir();
            Directory.CreateDirectory(dir);
            var path = Path.Combine(dir, $"{FacadeId}.json");
            var payload = new
            {
                facade_id = FacadeId,
                source_image = SourceImageName,
                image_path = ImagePath,
                image_width_px = OrigWidth,
                image_height_px = OrigHeight,
                regions = Boxes.Select(b => new
                {
                    region_id = b.Index,
                    pixel_measurements = BuildViewerPixelMeasurements(b),
                    points = b.OrigPoints.Select(p => new { x = p.X, y = p.Y }),
                }),
            };
            File.WriteAllText(path, JsonSerializer.Serialize(payload, new JsonSerializerOptions { WriteIndented = true }));
            SaveStatus = $"저장됨: {path}";
            _boxesDirty = false;
        }
        catch (Exception ex)
        {
            SaveStatus = $"저장 실패: {ex.Message}";
        }
    }
}

file sealed class AiCracksEntry
{
    [JsonPropertyName("image_id")] public string ImageId { get; set; } = "";
    [JsonPropertyName("file_name")] public string FileName { get; set; } = "";
    [JsonPropertyName("width")] public int Width { get; set; }
    [JsonPropertyName("height")] public int Height { get; set; }
    [JsonPropertyName("detections")] public List<AiCrackDetection> Detections { get; set; } = new();
}

file sealed class AiCrackDetection
{
    [JsonPropertyName("polygon_px")] public List<List<double>> PolygonPx { get; set; } = new();
    [JsonPropertyName("confidence")] public double Confidence { get; set; }
}
