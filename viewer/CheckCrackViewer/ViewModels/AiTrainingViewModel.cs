using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
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

    // How long PrepareImage tolerates zero new log activity for the running facade
    // before treating the subprocess as hung and killing it -- confirmed necessary
    // live: a stitch run sat frozen mid-pair (no new pipeline.log lines, CPU time
    // on the child process not advancing) for 10+ minutes with no way to notice
    // short of watching logs by hand. This is an IDLE timeout (resets on every new
    // log line for this facade), not a total-runtime cap, so a legitimately slow
    // but still-progressing COLMAP run isn't cut off.
    private static readonly TimeSpan PipelineIdleTimeout = TimeSpan.FromMinutes(5);

    private static readonly Dictionary<string, string> StageLabels = new()
    {
        ["METADATA_PARSED"] = "메타데이터 파싱 완료",
        ["FACADE_ASSIGNED"] = "Facade 자동분류 완료",
        ["PAIR_GRAPH_BUILT"] = "매칭 쌍 구성 완료",
        ["MATCH_GEOMETRY"] = "매칭 · Geometry 계산 중",
        ["GEOMETRY_SOLVED"] = "매칭 · Geometry 완료",
        ["STITCHED"] = "스티칭 완료",
        ["NEEDS_MANUAL_REVIEW"] = "검토 필요 (Drift 감지)",
        ["COLMAP_EXTRACT"] = "CM 특징점 추출 중",
        ["COLMAP_MATCH"] = "CM 매칭 중",
        ["COLMAP_MAPPING"] = "CM SfM 재구성 중",
        ["COLMAP_MAPPING_PROGRESS"] = "CM 이미지 등록 중",
        ["COLMAP_FALLBACK"] = "CM 보정 완료",
        ["RECTIFIED_COLMAP"] = "CM 정밀 재투영 완료",
        ["FAILED_GEOMETRY"] = "실패 (품질 게이트 통과 pair 없음)",
        ["DONE"] = "완료",
        ["PREVIEW_UPDATED"] = "모자이크 미리보기 갱신 중",
    };

    /// <summary>Set by MainViewModel (constructor + OnRootPathChanged) -- this
    /// ViewModel doesn't own RootPath itself, it just needs it to find
    /// tools/stitch_for_ai_training.py and logs/pipeline.log.</summary>
    public string RootPath { get; set; } = "";

    [ObservableProperty] private bool _isProcessing;
    [ObservableProperty] private string _progressText = "";
    [ObservableProperty] private bool _hasFailed;
    [ObservableProperty] private string _failureLog = "";

    [ObservableProperty] private bool _isTraining;
    [ObservableProperty] private string _trainingStatusText = "";
    [ObservableProperty] private int _trainingEpoch;
    [ObservableProperty] private int _trainingEpochs;
    [ObservableProperty] private string _metricsText = "";
    [ObservableProperty] private string _trainingErrorText = "";
    // True once a training run has reached a terminal state (done or error) --
    // distinct from MetricsText being non-empty so a FAILED run also leaves a
    // persistent, visible result instead of just flashing inside the
    // "학습 진행 중" overlay and then disappearing once IsTraining goes false.
    [ObservableProperty] private bool _hasTrainingResult;

    /// <summary>Three independent training sources (see the AI 학습 탭 plan): each
    /// gets its own dataset dir/model runs so results are never silently mixed,
    /// which is exactly the "디버깅을 수월하게" requirement this was built for.
    /// "labeled" = already-masked source folder (no polygon drawing needed),
    /// "raw_crops" = plain unlabeled crack photos (polygon drawing, many images via
    /// Next/Back), "stitched" = the original facade-mosaic flow (unchanged).</summary>
    [ObservableProperty] private string _selectedSource = "stitched";
    [ObservableProperty] private bool _isLabeledSelected;
    [ObservableProperty] private bool _isRawCropsSelected;
    [ObservableProperty] private bool _isStitchedSelected = true;

    private bool _syncingSource;

    partial void OnIsLabeledSelectedChanged(bool value) => HandleSourceCheckboxChanged(value, "labeled");
    partial void OnIsRawCropsSelectedChanged(bool value) => HandleSourceCheckboxChanged(value, "raw_crops");
    partial void OnIsStitchedSelectedChanged(bool value) => HandleSourceCheckboxChanged(value, "stitched");

    /// <summary>Three CheckBoxes behaving like a radio group (user's explicit request:
    /// "체크박스로 선택" but only one active source at a time) -- checking one
    /// unchecks the other two; unchecking the active one just re-checks itself,
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
            IsStitchedSelected = SelectedSource == "stitched";
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
        NotifyImageViewStateChanged();

        StatusText = source switch
        {
            "labeled" => "\"이미지 폴더 열기\"로 기존 마스크 데이터셋 폴더를 선택하세요.",
            "raw_crops" => "\"이미지 폴더 열기\"로 일반 크랙 사진 폴더를 선택하세요.",
            _ => "\"이미지 폴더 열기\"로 스티칭할 원본 사진 폴더를 선택하세요.",
        };

        PrepareImageCommand.NotifyCanExecuteChanged();
        PrepareLabeledDatasetCommand.NotifyCanExecuteChanged();
        SaveTrainingDataCommand.NotifyCanExecuteChanged();
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

    private void NotifyBusyCommands()
    {
        SelectImageFolderCommand.NotifyCanExecuteChanged();
        PrepareImageCommand.NotifyCanExecuteChanged();
        PrepareLabeledDatasetCommand.NotifyCanExecuteChanged();
        StartTrainingCommand.NotifyCanExecuteChanged();
        StartFineTuneCommand.NotifyCanExecuteChanged();
    }

    partial void OnSelectedFolderPathChanged(string value)
    {
        PrepareImageCommand.NotifyCanExecuteChanged();
        PrepareLabeledDatasetCommand.NotifyCanExecuteChanged();
    }

    // Stitching (IsProcessing) and training (IsTraining) both shell out a python
    // subprocess and both want the canvas-covering overlay -- never let one start
    // while the other is running.
    private bool CanRunPipeline() => !IsProcessing && !IsTraining;
    private bool CanPrepareImage() => !IsProcessing && !IsTraining && SelectedSource == "stitched" && !string.IsNullOrEmpty(SelectedFolderPath);
    private bool CanPrepareLabeledDataset() => !IsProcessing && !IsTraining && SelectedSource == "labeled";
    private bool CanTrain() => !IsProcessing && !IsTraining;

    /// <summary>Set by SelectImageFolder, consumed by PrepareImage -- kept as two
    /// separate steps/buttons (선택 -> 준비) so picking a folder doesn't immediately
    /// commit to a potentially long stitching+COLMAP run.</summary>
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

    /// <summary>Step 1/2: just picks a folder of raw drone photos and remembers it --
    /// does NOT start the (potentially long) stitching+COLMAP run by itself. Split
    /// out from the pipeline call (which used to fire immediately on folder pick) so
    /// choosing a folder and committing to running the pipeline on it are two
    /// separate, deliberate actions/buttons ("이미지 폴더 열기" -> "이미지 준비") for the
    /// "stitched" source. For "raw_crops" there's no separate prepare step -- picking
    /// the folder immediately lists its images for Next/Back polygon drawing.</summary>
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

        if (SelectedSource == "raw_crops")
        {
            var files = new[] { "*.jpg", "*.jpeg", "*.png" }
                .SelectMany(pattern => Directory.GetFiles(dialog.FolderName, pattern))
                .OrderBy(p => p, StringComparer.OrdinalIgnoreCase)
                .ToList();
            ImageList = files;
            if (files.Count > 0)
            {
                CurrentImageIndex = 0;
                LoadImageForCurrentSource();
            }
            else
            {
                CurrentImageIndex = -1;
            }
            StatusText = $"선택됨: {dialog.FolderName}  ({files.Count}장 -- 영역을 그리고 저장하며 Next로 넘어가세요)";
            return;
        }

        StatusText = $"선택됨: {SelectedFolderPath}  (\"이미지 준비\"를 눌러 스티칭 + CM 실행)";
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

    /// <summary>Step 2/2: runs tools/stitch_for_ai_training.py (스티칭 + 필요 시 COLMAP
    /// fallback) on the folder SelectImageFolder picked -- a SEPARATE script from
    /// tools/stitch_folder.py (used by MainViewModel's RunFacadeCommand)
    /// deliberately, since stitch_folder.py is expected to grow its own
    /// general-purpose CLI features over time and this tab's pipeline call should
    /// not shift underneath it when that happens. Shows live stage progress while it
    /// runs, and once done auto-loads the resulting analysis mosaic straight into
    /// the annotation canvas -- so this tab never annotates raw un-stitched photos,
    /// only the same stitched-mosaic-tile input the production Crack Segmentation
    /// model actually sees at inference (see the facade-vs-raw-photo review this
    /// button replaced).</summary>
    [RelayCommand(CanExecute = nameof(CanPrepareImage))]
    private async Task PrepareImage()
    {
        var folder = SelectedFolderPath;
        var facadeId = Path.GetFileName(folder.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));

        IsProcessing = true;
        HasFailed = false;
        FailureLog = "";
        ProgressText = "시작하는 중...";
        StatusText = $"{facadeId} 처리 중...";

        // Accumulated WARNING/ERROR lines for this facade, shown verbatim in the
        // failure panel so a gate failure (e.g. FAILED_GEOMETRY) is explained by
        // the pipeline's own words instead of a generic "실패했습니다".
        var failureLines = new List<string>();
        var failed = false;
        Process? runningProcess = null;
        var lastActivityUtc = DateTime.UtcNow;

        void ReportFailure(string status, string log)
        {
            failed = true;
            HasFailed = true;
            FailureLog = log;
            StatusText = status;
            IsProcessing = false;
            try { runningProcess?.Kill(entireProcessTree: true); } catch { }
        }

        PipelineLogTailer? tailer = null;
        var watchdog = new DispatcherTimer { Interval = TimeSpan.FromSeconds(5) };
        try
        {
            var logPath = Path.Combine(RootPath, "logs", "pipeline.log");
            tailer = new PipelineLogTailer(logPath);
            tailer.EntryParsed += entry =>
            {
                if (entry.FacadeId != facadeId)
                    return;
                lastActivityUtc = DateTime.UtcNow;
                var label = StageLabels.TryGetValue(entry.Stage ?? "", out var known) ? known : entry.Stage;
                ProgressText = entry.Progress != null ? $"{label}  ({entry.Progress})" : label ?? "";

                if (entry.Level is "WARNING" or "ERROR")
                {
                    var statusSuffix = entry.Status != null ? $" (status: {entry.Status})" : "";
                    failureLines.Add($"[{entry.Level}] {entry.Message}{statusSuffix}");
                }

                // A FAILED_* stage is terminal for this facade -- stop the "실행 중"
                // framing immediately instead of waiting for the subprocess to fully
                // exit (which left the "실행 중" overlay and the already-failed stage
                // label showing at the same time, reading as self-contradictory) and
                // kill the now-pointless subprocess right away.
                if (!failed && entry.Stage != null && entry.Stage.StartsWith("FAILED_"))
                {
                    ReportFailure($"{facadeId} 실패: {label}",
                        failureLines.Count > 0 ? string.Join("\n", failureLines) : entry.Message);
                }
            };
            tailer.Start();

            // Idle watchdog: confirmed live that a stuck subprocess can sit with zero
            // new log lines and flat CPU time for 10+ minutes with nothing else to
            // signal it -- past PipelineIdleTimeout with no new line for this facade,
            // treat it as hung and kill it instead of leaving the "실행 중" overlay up
            // indefinitely.
            watchdog.Tick += (_, _) =>
            {
                if (failed)
                    return;
                var idle = DateTime.UtcNow - lastActivityUtc;
                if (idle <= PipelineIdleTimeout)
                    return;
                var minutes = (int)Math.Round(PipelineIdleTimeout.TotalMinutes);
                var log = (failureLines.Count > 0 ? string.Join("\n", failureLines) + "\n\n" : "")
                    + $"{minutes}분 동안 새 진행 로그가 없어 중단했습니다. (마지막 상태: {ProgressText})";
                ReportFailure($"{facadeId} 실패: 응답 없음 ({minutes}분 초과)", log);
            };
            watchdog.Start();

            var scriptPath = Path.Combine(RootPath, "tools", "stitch_for_ai_training.py");
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
            psi.ArgumentList.Add(folder);
            psi.ArgumentList.Add(facadeId);
            psi.ArgumentList.Add("--in-place");

            using var process = new Process { StartInfo = psi };
            process.Start();
            runningProcess = process;
            ChildProcessRegistry.Register(process);
            try
            {
                var stderrTask = process.StandardError.ReadToEndAsync();
                // Every pipeline.log line is mirrored to stdout too (src/common/logging.py's
                // StreamHandler(sys.stdout)) -- RedirectStandardOutput=true without ever
                // reading it fills the OS pipe buffer after enough pairs, and the python
                // process then blocks forever on its next stdout write. Confirmed live:
                // this, not anything GPU/LoFTR-related, was the real cause of runs freezing
                // at the same pair count every time -- MainViewModel's RunFacade already
                // drains stdout the same way; this tab's PrepareImage was missing it.
                var stdoutTask = process.StandardOutput.ReadToEndAsync();
                await process.WaitForExitAsync();

                // Already reported (message + log + kill) from the tailer callback above.
                if (failed)
                    return;

                if (process.ExitCode != 0)
                {
                    var stderr = await stderrTask;
                    var firstLine = stderr.Split('\n').FirstOrDefault(l => !string.IsNullOrWhiteSpace(l)) ?? "(no stderr output)";
                    HasFailed = true;
                    FailureLog = failureLines.Count > 0
                        ? string.Join("\n", failureLines) + "\n\n" + firstLine.Trim()
                        : firstLine.Trim();
                    StatusText = $"파이프라인 실행 실패 (exit {process.ExitCode})";
                    return;
                }

                // --in-place lands the result under <folder>/output/ -- COLMAP fallback
                // (when triggered) produces the _analysis_colmap variant, so prefer
                // that one when present, same as the main "분석 · 스티칭" tab's
                // VisualColmapImagePath/VisualImagePath precedence.
                var outputDir = Path.Combine(folder, "output");
                var candidate = new[] { $"{facadeId}_analysis_colmap.tif", $"{facadeId}_analysis.tif" }
                    .Select(name => Path.Combine(outputDir, name))
                    .FirstOrDefault(File.Exists);

                if (candidate != null)
                {
                    LoadImage(candidate);
                }
                else
                {
                    HasFailed = true;
                    FailureLog = failureLines.Count > 0
                        ? string.Join("\n", failureLines)
                        : "파이프라인은 종료됐지만 결과 이미지를 찾지 못했습니다.";
                    StatusText = "파이프라인 실패: 결과 이미지를 찾을 수 없습니다.";
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
            StatusText = $"파이프라인을 시작할 수 없습니다: {ex.Message}";
        }
        finally
        {
            watchdog.Stop();
            tailer?.Dispose();
            IsProcessing = false;
        }
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

        IsTraining = true;
        TrainingEpoch = 0;
        TrainingEpochs = 0;
        TrainingStatusText = "준비 중...";
        HasTrainingResult = false;
        MetricsText = "";
        TrainingErrorText = "";

        var runId = $"{SelectedSource}_{mode}_{DateTime.Now:yyyyMMdd_HHmmss}";
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
                // See PrepareImage's identical fix above -- ultralytics' training output is
                // even more verbose than the stitch pipeline's, so this is at least as
                // exposed to the same stdout-pipe deadlock.
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
        var folderName = SelectedSource == "raw_crops" ? "training_data_raw_crops" : "training_data_stitched";
        return Path.Combine(RootPath, folderName);
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
            UpdateJson();
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
    /// "raw_crops" and "stitched" each write into their OWN training_data_* folder
    /// (train_from_viewer.py's SOURCES dict) so the two never mix into the same
    /// dataset/model -- exactly the separation the AI 학습 탭 redesign was for.</summary>
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
