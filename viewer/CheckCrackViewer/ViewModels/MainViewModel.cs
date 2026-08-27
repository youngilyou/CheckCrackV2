using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Data;
using System.Windows.Threading;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using CheckCrackViewer.Models;
using CheckCrackViewer.Services;
using CheckCrackViewer.Views;
using Microsoft.Win32;

namespace CheckCrackViewer.ViewModels;

public partial class MainViewModel : ObservableObject
{
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

    private static readonly HashSet<string> ImageExtensions = new(StringComparer.OrdinalIgnoreCase)
    {
        ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp",
    };

    [ObservableProperty] private string _rootPath = "";
    [ObservableProperty] private FacadeItemViewModel? _selectedFacade;
    [ObservableProperty] private string _statusText = "대기 중";
    [ObservableProperty] private bool _autoScrollLog = true;

    /// <summary>Set by App.xaml.cs right after MainWindow is constructed,
    /// from LoginWindow's AppUser -- MainViewModel itself has no dependency
    /// on the login flow (no constructor param), it just displays whatever
    /// gets assigned here.</summary>
    [ObservableProperty] private string _loggedInUsername = "";

    /// <summary>Left hamburger menu selection -- "analysis" (기존 3단 화면),
    /// "training" (AI 학습, 준비 중), "settings" (설정, 준비 중).</summary>
    [ObservableProperty] private string _selectedMenu = "dashboard";
    [ObservableProperty] private bool _menuCollapsed;
    [ObservableProperty] private string _dbHost = "";
    [ObservableProperty] private int _dbPort = 3306;
    [ObservableProperty] private string _dbName = "";
    [ObservableProperty] private string _dbUser = "";
    [ObservableProperty] private string _dbPassword = "";
    [ObservableProperty] private bool _dbUseSsl;
    [ObservableProperty] private int _dbTimeoutSeconds = 5;
    [ObservableProperty] private bool _dbSavePassword;
    [ObservableProperty] private string _dbSettingsStatus = "DB 연결 정보를 입력하세요.";

    // --- 설정 > 시스템 상태 탭: 읽기 전용 모니터링 값 (실제 신호만, 지어내지 않음) ---
    [ObservableProperty] private string _aiModelPath = "확인 중...";
    [ObservableProperty] private string _pythonExePath = "확인 중...";
    [ObservableProperty] private string _storageStatusText = "확인 중...";

    // --- 계정(로그인) 아이디/비밀번호 변경, "설정" 페이지 -- UserStore(SQLite)가
    // 저장소, MySQL DbConnectionSettings와는 완전히 별개. 계정명 변경도 비밀번호
    // 변경과 마찬가지로 현재 비밀번호 확인이 필요해서 CurrentPassword를 공유한다. ---
    [ObservableProperty] private string _newUsername = "";
    [ObservableProperty] private string _currentPassword = "";
    [ObservableProperty] private string _newPassword = "";
    [ObservableProperty] private string _confirmNewPassword = "";
    [ObservableProperty] private string _accountStatus = "";

    /// <summary>Recomputed whenever MenuCollapsed changes (see the partial method
    /// below) -- bound directly to the sidebar's ColumnDefinition.Width.</summary>
    public GridLength SidebarWidth => new(MenuCollapsed ? 56 : 200);

    partial void OnMenuCollapsedChanged(bool value) => OnPropertyChanged(nameof(SidebarWidth));

    [RelayCommand]
    private void ToggleMenu() => MenuCollapsed = !MenuCollapsed;

    [RelayCommand]
    private void SelectMenu(string menu) => SelectedMenu = menu;

    /// <summary>Backs the "AI 학습" hamburger-menu page (Views/AiTrainingView.xaml) --
    /// its own independent ViewModel, not folded into MainViewModel, since it's a
    /// self-contained tool (crack box annotation / YOLO training-data export).</summary>
    public AiTrainingViewModel AiTraining { get; } = new();

    /// <summary>"원본 AI" 하마버거 메뉴 페이지 -- AiTraining과 마찬가지로 독립된
    /// 도구(원본 사진 단위 크랙 후보 확인, 스티칭/DB와 무관).</summary>
    public OriginalAiViewModel OriginalAi { get; } = new();

    /// <summary>"결과보기" 하마버거 메뉴 페이지 -- facade별 원본/스티칭/보고서 비교,
    /// 자체 폴링으로 독립 동작.</summary>
    public ResultsCompareViewModel ResultsCompare { get; } = new();

    public ObservableCollection<FacadeItemViewModel> Facades { get; } = new();

    /// <summary>단지(Complex) → 동(Building, 선택) → 방위(Side) → Facade 트리 —
    /// Facades로부터 파생되는 표시 전용 데이터. 구조가 실제로 바뀔 때(폴더 추가/재분류)만
    /// RebuildFacadeTree()로 재구성한다 — 2초 폴링(RescanFacadeOutputs)에서는 절대 재구성
    /// 안 함(펼침/접힘 상태가 매번 초기화되는 것을 막기 위함 — ResultsCompareViewModel의
    /// 스냅샷 폴링에서 이미 겪은 것과 같은 종류의 object-identity 버그 재발 방지).</summary>
    public ObservableCollection<ComplexNode> FacadeTree { get; } = new();

    /// <summary>"작업 현황" 대시보드 12개 카드 — 순서 고정, 항목 자체는 절대 재생성하지
    /// 않고 .Count만 갱신(RecomputeDashboardCounts). 촬영예정/촬영중/촬영완료/누락
    /// 재촬영 필요/정밀촬영 필요는 이 Viewer가 실제 신호를 가진 적이 없으므로 항상 0
    /// (DashboardStatusCount.cs 참고).</summary>
    public ObservableCollection<DashboardStatusCount> DashboardCounts { get; } = BuildDashboardCounts();

    private static ObservableCollection<DashboardStatusCount> BuildDashboardCounts()
    {
        (DashboardStatusCategory Category, string Label)[] defs =
        {
            //(DashboardStatusCategory.PlannedCapture, "촬영예정"),
            //(DashboardStatusCategory.Capturing, "촬영중"),
            //(DashboardStatusCategory.CaptureDone, "촬영완료"),
            (DashboardStatusCategory.CoverageRetakeNeeded, "누락 재촬영 필요"),
            (DashboardStatusCategory.StitchQueued, "Stitching 대기"),
            (DashboardStatusCategory.Stitching, "Stitching 중"),
            (DashboardStatusCategory.AiAnalyzing, "AI 분석중"),
            (DashboardStatusCategory.DetailCaptureNeeded, "정밀촬영 필요"),
            (DashboardStatusCategory.NeedsManualReview, "사용자 검토 필요"),
            (DashboardStatusCategory.GeneratingReport, "보고서 생성중"),
            (DashboardStatusCategory.Done, "완료"),
            (DashboardStatusCategory.Failed, "실패"),
        };
        return new ObservableCollection<DashboardStatusCount>(
            defs.Select(d => new DashboardStatusCount { Category = d.Category, Label = d.Label }));
    }

    public ObservableCollection<PipelineLogEntry> LogEntries { get; } = new();

    /// <summary>Facades currently mid-스티칭/크랙탐지/보고서생성, for the "시스템 상태"
    /// 탭's read-only "처리 Job" card. Recomputed (clear + re-add) at the same two
    /// points RecomputeDashboardCounts() already runs from -- never guessed, always
    /// derived straight from FacadeItemViewModel's own real IsRunning/IsDetectingCracks/
    /// IsGeneratingReport flags.</summary>
    public ObservableCollection<FacadeItemViewModel> ActiveJobs { get; } = new();

    /// <summary>Independent (non-shared) filtered view over LogEntries for the "오류/로그"
    /// card -- deliberately NOT CollectionViewSource.GetDefaultView(LogEntries), which
    /// would be the same view instance the LIVE LOG ListBox uses and would corrupt it.
    /// Safe as a plain Filter (no IsLiveFiltering needed) because PipelineLogEntry.Level
    /// never changes after an entry is added -- new entries get filtered as they're
    /// added; nothing about an existing entry ever mutates.</summary>
    public ICollectionView ErrorLogView { get; }

    private readonly Dictionary<string, HashSet<string>> _facadeWarningKeys = new();
    private PipelineLogTailer? _tailer;
    private readonly DispatcherTimer _facadeScanTimer;

    /// <summary>Total concurrent-analysis cap shared by manual "실행" clicks (RunFacade below)
    /// and remote-triggered runs (AnalysisAssignment, see AnalysisBridgeService) -- see
    /// AnalysisConcurrencyManager's own doc comment for why this didn't exist before.</summary>
    private readonly AnalysisConcurrencyManager _concurrency = new();

    private readonly AnalysisBridgeService _analysisBridge = new();
    private readonly DispatcherTimer _heartbeatTimer;

    /// <summary>Backs the "원격 분석 작업" window (see RemoteAnalysisJobsWindow) -- exposed so
    /// MainWindow's "원격 분석 작업" button can open a window bound to this same instance.</summary>
    public RemoteAnalysisJobsViewModel RemoteJobs { get; }

    public MainViewModel()
    {
        ErrorLogView = new ListCollectionView(LogEntries)
        {
            Filter = o => o is PipelineLogEntry e && e.Level != "INFO",
        };

        _facadeScanTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
        _facadeScanTimer.Tick += (_, _) => RescanFacadeOutputs();

        RootPath = DiscoverDefaultRoot();
        AiTraining.RootPath = RootPath;
        OriginalAi.RootPath = RootPath;
        ResultsCompare.RootPath = RootPath;
        LoadDbSettings();
        LoadCrackVisionSettings();
        AttachToRoot();

        // Fire-and-forget: GPU detection shells out to python (see GpuDetectionService), so it
        // can't block the constructor. _concurrency.MaxConcurrent stays at its safe default (1)
        // until this resolves -- a few seconds of "CPU-only" caution at startup is harmless.
        _ = InitializeGpuDetectionAsync();

        RemoteJobs = new RemoteAnalysisJobsViewModel(_analysisBridge, CrackVisionDbSettingsStore.Load,
            RegisterAndRunExtractedArchiveAsync, RootPath);
        _analysisBridge.AssignmentReceived += RemoteJobs.HandleAssignment;
        // Retry/Stop: only meaningful after an AnalysisErrorNotify the operator has acted on
        // (see FacadeAnalysis.idl's own comment on these two) -- this pass surfaces them as a
        // status update on the matching job row; actually re-invoking/aborting the underlying
        // python process per archive_id is left as a follow-up (RunFacade has no per-archive
        // handle to cancel/retry against yet).
        _analysisBridge.RetryReceived += r => RemoteJobs.MarkControlReceived(r.ArchiveId, "재시도 요청됨 (미구현)");
        _analysisBridge.StopReceived += s => RemoteJobs.MarkControlReceived(s.ArchiveId, "정지 요청됨 (미구현)");

        var workerId = string.IsNullOrWhiteSpace(CrackVisionWorkerId) ? Environment.MachineName : CrackVisionWorkerId;
        _analysisBridge.Start(domainId: 31, workerId: workerId);

        _heartbeatTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(5) };
        _heartbeatTimer.Tick += (_, _) => _analysisBridge.SendHeartbeat(
            _concurrency.MaxConcurrent, (uint)Math.Max(0, _concurrency.RunningCount), (uint)_concurrency.QueuedCount);
        _heartbeatTimer.Start();
    }

    private async Task InitializeGpuDetectionAsync()
    {
        var gpuAvailable = await GpuDetectionService.DetectCudaAvailableAsync();
        _concurrency.MaxConcurrent = GpuDetectionService.ComputeMaxConcurrent(gpuAvailable);
    }

    partial void OnRootPathChanged(string value)
    {
        AiTraining.RootPath = value;
        OriginalAi.RootPath = value;
        ResultsCompare.RootPath = value;
    }

    /// <summary>App.xaml.cs sets LoggedInUsername right after login (before
    /// RootPath propagation even runs, in the constructor's case) -- this
    /// mirrors that same propagation for the crack-review feature's
    /// attribution field, same reasoning as OnRootPathChanged above.</summary>
    partial void OnLoggedInUsernameChanged(string value)
    {
        ResultsCompare.LoggedInUsername = value;
    }

    private static string DiscoverDefaultRoot()
    {
        // Walk up from the exe's folder looking for CLAUDE.local.md (unique
        // marker for this project) — falls back to the known dev path so the
        // app is still useful for the one machine it's built on today.
        var dir = AppDomain.CurrentDomain.BaseDirectory;
        var probe = new DirectoryInfo(dir);
        for (int i = 0; i < 8 && probe != null; i++, probe = probe.Parent)
        {
            if (File.Exists(Path.Combine(probe.FullName, "CLAUDE.local.md")))
                return probe.FullName;
        }
        return @"D:\ClaudePr\CheckCrack";
    }

    private void LoadDbSettings()
    {
        try
        {
            var settings = DbSettingsStore.Load();
            if (settings == null)
                return;

            DbHost = settings.Host;
            DbPort = settings.Port <= 0 ? 3306 : settings.Port;
            DbName = settings.Database;
            DbUser = settings.User;
            DbPassword = settings.SavePassword ? settings.Password : "";
            DbUseSsl = settings.UseSsl;
            DbTimeoutSeconds = settings.TimeoutSeconds <= 0 ? 5 : settings.TimeoutSeconds;
            DbSavePassword = settings.SavePassword;
            DbSettingsStatus = $"불러옴: {DbSettingsStore.SettingsPath}";
        }
        catch (Exception ex)
        {
            DbSettingsStatus = $"DB 설정을 읽을 수 없습니다: {ex.Message}";
        }
    }

    // === CrackVisionDB(PostgreSQL)/SFTP 설정 + 수동 다운로드 (SettingsView) ===
    // 별도 파일/클래스(CrackVisionDbSettingsStore)로 저장 -- 위 DbHost/DbPort 등(MySQL, 완전히
    // 다른 미래 기능용)과는 절대 혼동하지 않게 분리, 원격 자동 경로는 이 설정을 전혀 안 씀(archive_id
    // 하나당 필요한 모든 정보가 이미 AnalysisAssignment에 담겨 옴 -- SFTP 접속 정보만 예외).

    [ObservableProperty] private string _crackVisionPostgresHost = "";
    [ObservableProperty] private int _crackVisionPostgresPort = 5432;
    [ObservableProperty] private string _crackVisionPostgresDatabase = "mngdata";
    [ObservableProperty] private string _crackVisionPostgresUser = "mngdata";
    [ObservableProperty] private string _crackVisionPostgresPassword = "";
    [ObservableProperty] private string _crackVisionSftpHost = "";
    [ObservableProperty] private int _crackVisionSftpPort = 22;
    [ObservableProperty] private string _crackVisionSftpUser = "";
    [ObservableProperty] private string _crackVisionSftpPassword = "";
    [ObservableProperty] private string _crackVisionSftpPrivateKeyPath = "";
    [ObservableProperty] private string _crackVisionWorkerId = "";
    [ObservableProperty] private string _crackVisionSettingsStatus = "";
    [ObservableProperty] private bool _isLoadingRemoteArchives;

    /// <summary>수동 다운로드 목록 -- "새로고침" 클릭 시 CrackVisionDB(crackvision_archives)를
    /// 직접 조회해서 채움. 자동(원격 명령) 경로는 이 목록/쿼리와 전혀 무관. 각 행은
    /// RemoteArchiveRowViewModel로 감싸서 다운로드 진행 상태를 그 행 자리에서 바로 보여줌
    /// (예전엔 저장 버튼 옆의 공용 상태 텍스트 하나뿐이라 실제로는 안 보였음).</summary>
    public ObservableCollection<RemoteArchiveRowViewModel> RemoteArchives { get; } = new();

    private void LoadCrackVisionSettings()
    {
        var s = CrackVisionDbSettingsStore.Load();
        CrackVisionPostgresHost = s.PostgresHost;
        CrackVisionPostgresPort = s.PostgresPort;
        CrackVisionPostgresDatabase = s.PostgresDatabase;
        CrackVisionPostgresUser = s.PostgresUser;
        CrackVisionPostgresPassword = s.PostgresPassword;
        CrackVisionSftpHost = s.SftpHost;
        CrackVisionSftpPort = s.SftpPort;
        CrackVisionSftpUser = s.SftpUser;
        CrackVisionSftpPassword = s.SftpPassword;
        CrackVisionSftpPrivateKeyPath = s.SftpPrivateKeyPath;
        CrackVisionWorkerId = s.WorkerId;
    }

    private CrackVisionDbSettings BuildCrackVisionSettings() => new()
    {
        PostgresHost = CrackVisionPostgresHost.Trim(),
        PostgresPort = CrackVisionPostgresPort,
        PostgresDatabase = CrackVisionPostgresDatabase.Trim(),
        PostgresUser = CrackVisionPostgresUser.Trim(),
        PostgresPassword = CrackVisionPostgresPassword,
        SftpHost = CrackVisionSftpHost.Trim(),
        SftpPort = CrackVisionSftpPort,
        SftpUser = CrackVisionSftpUser.Trim(),
        SftpPassword = CrackVisionSftpPassword,
        SftpPrivateKeyPath = CrackVisionSftpPrivateKeyPath.Trim(),
        WorkerId = CrackVisionWorkerId.Trim(),
    };

    [RelayCommand]
    private void SaveCrackVisionSettings()
    {
        try
        {
            CrackVisionDbSettingsStore.Save(BuildCrackVisionSettings());
            CrackVisionSettingsStatus = $"저장됨: {CrackVisionDbSettingsStore.SettingsPath} (worker_id 변경은 앱 재시작 후 반영됩니다)";
        }
        catch (Exception ex)
        {
            CrackVisionSettingsStatus = $"저장 실패: {ex.Message}";
        }
    }

    /// <summary>CrackVisionDB(crackvision_archives)를 직접 조회 -- 자동 경로와 무관, 관리자가
    /// "수동 다운로드" 목록을 채울 때만 호출됨.</summary>
    [RelayCommand]
    private async Task RefreshRemoteArchives()
    {
        IsLoadingRemoteArchives = true;
        try
        {
            var records = await CrackVisionArchiveQueryService.ListArchivesAsync(BuildCrackVisionSettings());
            RemoteArchives.Clear();
            foreach (var r in records)
                RemoteArchives.Add(new RemoteArchiveRowViewModel(r));
            CrackVisionSettingsStatus = $"{records.Count}건 조회됨";
        }
        catch (Exception ex)
        {
            CrackVisionSettingsStatus = $"조회 실패: {ex.Message}";
        }
        finally
        {
            IsLoadingRemoteArchives = false;
        }
    }

    /// <summary>관리자가 "수동 다운로드" 목록에서 직접 선택 -- 다운로드+압축해제+등록까지만 하고
    /// 분석은 시작하지 않는다(수동으로 폴더를 추가했을 때와 동일한 관례, 실행은 별도 "실행"
    /// 클릭으로). 자동(원격 명령) 경로의 RegisterAndRunExtractedArchiveAsync와 다른 점이 바로
    /// 이 부분 -- "수동은 관리자가 선택"(다운로드 시점 + 실행 시점 둘 다). 진행 상태는 해당
    /// row.Status에 바로 표시(리스트 항목 자리, 클릭한 버튼 바로 옆) -- 예전엔 저장 버튼 옆
    /// 공용 텍스트 하나뿐이라 실제로는 안 보였음(2026-08-27 피드백).</summary>
    [RelayCommand]
    private async Task DownloadAndRegisterArchive(RemoteArchiveRowViewModel row)
    {
        var archive = row.Record;
        row.IsBusy = true;
        try
        {
            row.Status = "다운로드 중...";
            var settings = BuildCrackVisionSettings();
            var localZipPath = Path.Combine(RootPath, "remote_downloads", "zips", $"{archive.ArchiveId}.zip");
            await SftpDownloadService.DownloadAsync(settings, archive.ZipPath, localZipPath);

            row.Status = "압축 해제 중...";
            var extractDir = Path.Combine(RootPath, "remote_downloads", "extracted",
                $"{archive.Company}_{archive.Building}_{archive.ArchiveId}");
            if (Directory.Exists(extractDir))
                Directory.Delete(extractDir, recursive: true);
            Directory.CreateDirectory(Path.GetDirectoryName(extractDir)!);
            System.IO.Compression.ZipFile.ExtractToDirectory(localZipPath, extractDir);

            RegisterExtractedArchive(extractDir, archive.Company, archive.Building);
            row.Status = $"등록 완료 -- 왼쪽 목록에서 직접 실행하세요. 저장 위치: {extractDir}";
        }
        catch (Exception ex)
        {
            row.Status = $"실패: {ex.Message}";
        }
        finally
        {
            row.IsBusy = false;
        }
    }

    private void AttachToRoot()
    {
        _tailer?.Dispose();
        _facadeScanTimer.Stop();
        LogEntries.Clear();
        Facades.Clear();
        FacadeTree.Clear();
        _facadeWarningKeys.Clear();

        var logPath = Path.Combine(RootPath, "logs", "pipeline.log");
        _tailer = new PipelineLogTailer(logPath);
        _tailer.EntryParsed += OnLogEntry;
        _tailer.Start();

        RescanFacadeOutputs();
        ApplyFacadeClassifications();
        RebuildFacadeTree();
        RecomputeDashboardCounts();
        AiModelPath = PipelineConfigReader.ReadCrackModelPath(RootPath)
            ?? "확인 불가 (config/pipeline.yaml에서 crack.model을 찾을 수 없음)";
        PythonExePath = PythonEnvironment.DiscoverPythonExe();
        RefreshStorageStatus();
        RecomputeActiveJobs();
        _facadeScanTimer.Start();

        StatusText = Directory.Exists(RootPath)
            ? $"모니터링 중: {RootPath}"
            : $"경로를 찾을 수 없음: {RootPath}";
    }

    [RelayCommand]
    private void BrowseRoot()
    {
        var dialog = new OpenFolderDialog
        {
            Title = "CheckCrack 프로젝트 루트 폴더 선택 (logs/, facades/ 가 있는 위치)",
            InitialDirectory = Directory.Exists(RootPath) ? RootPath : @"D:\",
        };
        if (dialog.ShowDialog() == true)
        {
            RootPath = dialog.FolderName;
            AttachToRoot();
        }
    }

    [RelayCommand]
    private void RefreshNow() => RescanFacadeOutputs();

    [RelayCommand]
    private void ClearLog() => LogEntries.Clear();

    [RelayCommand]
    private void ExitApplication() => Application.Current.Shutdown();

    /// <summary>Raised by LogoutCommand -- App.xaml.cs subscribes to this to close
    /// MainWindow and loop back to a fresh LoginWindow without shutting the whole
    /// process down (unlike ExitApplication/종료, which does end the process).</summary>
    public event Action? LogoutRequested;

    [RelayCommand]
    private void Logout() => LogoutRequested?.Invoke();

    [RelayCommand]
    private void SaveDbSettings()
    {
        try
        {
            var settings = new DbConnectionSettings
            {
                Host = DbHost.Trim(),
                Port = DbPort,
                Database = DbName.Trim(),
                User = DbUser.Trim(),
                Password = DbSavePassword ? DbPassword : "",
                UseSsl = DbUseSsl,
                TimeoutSeconds = DbTimeoutSeconds,
                SavePassword = DbSavePassword,
            };
            DbSettingsStore.Save(settings);
            DbSettingsStatus = $"저장됨: {DbSettingsStore.SettingsPath}";
        }
        catch (Exception ex)
        {
            DbSettingsStatus = $"저장 실패: {ex.Message}";
        }
    }

    [RelayCommand]
    private async Task ChangeUsername()
    {
        if (string.IsNullOrWhiteSpace(NewUsername))
        {
            AccountStatus = "새 계정명을 입력하세요.";
            return;
        }
        if (string.IsNullOrEmpty(CurrentPassword))
        {
            AccountStatus = "현재 비밀번호를 입력하세요.";
            return;
        }

        try
        {
            var (ok, error) = await UserStore.ChangeUsernameAsync(LoggedInUsername, NewUsername.Trim(), CurrentPassword);
            if (!ok)
            {
                AccountStatus = error ?? "계정명 변경 실패";
                return;
            }
            LoggedInUsername = NewUsername.Trim();
            NewUsername = "";
            CurrentPassword = "";
            AccountStatus = "계정명이 변경되었습니다.";
        }
        catch (Exception ex)
        {
            AccountStatus = $"변경 실패: {ex.Message}";
        }
    }

    [RelayCommand]
    private async Task ChangePassword()
    {
        if (string.IsNullOrEmpty(CurrentPassword) || string.IsNullOrEmpty(NewPassword))
        {
            AccountStatus = "현재/새 비밀번호를 모두 입력하세요.";
            return;
        }
        if (NewPassword != ConfirmNewPassword)
        {
            AccountStatus = "새 비밀번호가 일치하지 않습니다.";
            return;
        }
        if (NewPassword.Length < 4)
        {
            AccountStatus = "새 비밀번호는 4자 이상이어야 합니다.";
            return;
        }

        try
        {
            var ok = await UserStore.ChangePasswordAsync(LoggedInUsername, CurrentPassword, NewPassword);
            if (!ok)
            {
                AccountStatus = "현재 비밀번호가 올바르지 않습니다.";
                return;
            }
            CurrentPassword = "";
            NewPassword = "";
            ConfirmNewPassword = "";
            AccountStatus = "비밀번호가 변경되었습니다.";
        }
        catch (Exception ex)
        {
            AccountStatus = $"변경 실패: {ex.Message}";
        }
    }

    [RelayCommand]
    private async Task TestDbConnection()
    {
        if (string.IsNullOrWhiteSpace(DbHost))
        {
            DbSettingsStatus = "DB Host/IP를 입력하세요.";
            return;
        }
        if (DbPort <= 0 || DbPort > 65535)
        {
            DbSettingsStatus = "Port는 1~65535 범위로 입력하세요.";
            return;
        }

        try
        {
            using var client = new TcpClient();
            var timeout = TimeSpan.FromSeconds(Math.Clamp(DbTimeoutSeconds, 1, 60));
            var connectTask = client.ConnectAsync(DbHost.Trim(), DbPort);
            var completed = await Task.WhenAny(connectTask, Task.Delay(timeout));
            if (completed != connectTask)
            {
                DbSettingsStatus = $"연결 실패: {DbHost}:{DbPort} 응답 없음 ({timeout.TotalSeconds:0}s)";
                return;
            }

            await connectTask;
            DbSettingsStatus = $"연결 가능: {DbHost}:{DbPort} 포트 응답 확인";
        }
        catch (Exception ex)
        {
            DbSettingsStatus = $"연결 실패: {ex.Message}";
        }
    }

    /// <summary>Lets the user point at a folder of drone photos and adds it to the
    /// facade list with a Run button, without leaving the app to use the CLI tools
    /// (tools/stitch_folder.py / stitch_all_folders.py) directly. Mirrors their
    /// exact folder-shape rule: a folder of subfolders (each containing images) is
    /// one facade per subfolder; a folder of images directly is one facade.
    ///
    /// 등록 전에 FacadeClassifyDialog로 단지/동(선택)/방위를 확인·수정받는다 — 폴더명에서
    /// 자동 제안은 하되(단지명 = 선택한 상위 폴더 이름, 방위 = 각 하위 폴더 이름), 최종
    /// 확정은 항상 사용자가 한다(CLAUDE.local.md #7: 방향은 별도 metadata, Viewer가 추측해서
    /// 확정하지 않음).</summary>
    [RelayCommand]
    private void BrowseImagesFolder()
    {
        var dialog = new OpenFolderDialog
        {
            Title = "이미지 폴더 선택 (하위 폴더가 있으면 각각 별도 facade로 인식)",
            InitialDirectory = Directory.Exists(RootPath) ? RootPath : @"D:\",
        };
        if (dialog.ShowDialog() != true)
            return;

        var selected = dialog.FolderName;
        var subfoldersWithImages = Directory.Exists(selected)
            ? Directory.GetDirectories(selected).Where(HasImages).OrderBy(p => p, StringComparer.OrdinalIgnoreCase).ToList()
            : new List<string>();

        List<(string FolderPath, string FacadeId, string ProposedSide)> candidates;
        string proposedComplexName;
        if (subfoldersWithImages.Count > 0)
        {
            candidates = subfoldersWithImages
                .Select(sub => (FolderPath: sub, FacadeId: Path.GetFileName(sub), ProposedSide: Path.GetFileName(sub)))
                .ToList();
            proposedComplexName = Path.GetFileName(selected.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
        }
        else if (HasImages(selected))
        {
            var facadeId = Path.GetFileName(selected.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
            candidates = new List<(string, string, string)> { (selected, facadeId, facadeId) };
            proposedComplexName = facadeId; // 상위 폴더 개념이 없어 자기 이름을 기본 제안값으로 씀 -- 다이얼로그에서 수정 가능
        }
        else
        {
            StatusText = $"이미지가 없는 폴더입니다: {selected}";
            return;
        }

        var classifyDialog = new FacadeClassifyDialog(candidates, proposedComplexName)
        {
            Owner = Application.Current.MainWindow,
        };
        if (classifyDialog.ShowDialog() != true)
            return;

        foreach (var result in classifyDialog.ViewModel.Result)
            AddRunnableCandidate(result);

        RebuildFacadeTree();
    }

    /// <summary>Called by RemoteAnalysisJobsViewModel once a downloaded archive has been
    /// extracted to <paramref name="extractedRootDir"/> -- registers each direction subfolder as
    /// its own facade (same "1 Facade = 1 Flight" mapping BrowseImagesFolder uses for a manually
    /// browsed multi-direction folder) and immediately starts analysis for each, automatically
    /// (no operator confirmation step -- the remote path is always "자동 실행", see
    /// AnalysisLoadBalancer README). Falls back to treating the extracted root itself as a
    /// single facade if it has no direction subfolders (a single-direction archive).</summary>
    /// <summary>Registers each direction subfolder under <paramref name="extractedRootDir"/> as
    /// its own facade (same "1 Facade = 1 Flight" mapping BrowseImagesFolder uses for a manually
    /// browsed multi-direction folder) WITHOUT starting analysis -- same "add now, run later"
    /// split as BrowseImagesFolder/AddRunnableCandidate already uses for manually-browsed
    /// folders. Falls back to treating the extracted root itself as a single facade if it has no
    /// direction subfolders (a single-direction archive). Returns the registered facades so the
    /// caller can decide whether to also run them.</summary>
    public List<FacadeItemViewModel> RegisterExtractedArchive(string extractedRootDir, string company, string building)
    {
        var directionDirs = Directory.Exists(extractedRootDir)
            ? Directory.GetDirectories(extractedRootDir).Where(HasImages).OrderBy(p => p, StringComparer.OrdinalIgnoreCase).ToList()
            : new List<string>();

        List<FacadeClassifyResult> candidates;
        if (directionDirs.Count > 0)
        {
            candidates = directionDirs
                .Select(d => new FacadeClassifyResult(d, Path.GetFileName(d), company, company, building, building, Path.GetFileName(d)))
                .ToList();
        }
        else
        {
            var facadeId = Path.GetFileName(extractedRootDir.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
            candidates = new List<FacadeClassifyResult> { new(extractedRootDir, facadeId, company, company, building, building, "") };
        }

        var facades = new List<FacadeItemViewModel>();
        foreach (var candidate in candidates)
        {
            AddRunnableCandidate(candidate);
            facades.Add(GetOrCreateFacade(candidate.FacadeId));
        }
        RebuildFacadeTree();
        return facades;
    }

    /// <summary>Remote (자동) path only -- registers AND immediately starts analysis for every
    /// resulting facade, no operator confirmation step (see AnalysisLoadBalancer README: the
    /// remote path is always "자동 실행"). The manual download path (SettingsView) calls
    /// RegisterExtractedArchive alone instead and leaves running as a separate deliberate
    /// "실행" click, matching how a manually browsed folder already works.</summary>
    public async Task<bool> RegisterAndRunExtractedArchiveAsync(string extractedRootDir, string company, string building)
    {
        var facades = RegisterExtractedArchive(extractedRootDir, company, building);

        // Each RunFacadeCommand call independently acquires its own slot from the same
        // _concurrency gate the manual "실행" button uses -- see RemoteAnalysisJobsViewModel's
        // own doc comment for why this method does NOT also acquire an archive-level slot itself.
        await Task.WhenAll(facades.Select(f => RunFacadeCommand.ExecuteAsync(f)));

        // RunFacade swallows its own per-facade pipeline failures (logs to facade.AddIssue,
        // never throws) -- awaiting it above always completes without an exception even if the
        // pipeline itself failed. Check the actual OverallStatus each run left behind instead, so
        // the AnalysisResult reported back to FacadePreviewer reflects reality rather than just
        // "the C# call didn't throw".
        return facades.All(f => f.OverallStatus != FacadeOverallStatus.Failed);
    }

    private static bool HasImages(string dir)
    {
        try
        {
            return Directory.EnumerateFiles(dir).Any(f => ImageExtensions.Contains(Path.GetExtension(f)));
        }
        catch (IOException)
        {
            return false;
        }
    }

    private void AddRunnableCandidate(FacadeClassifyResult r)
    {
        var facade = GetOrCreateFacade(r.FacadeId);
        facade.SourceFolderPath = r.FolderPath;
        ApplyClassification(facade, r.ComplexId, r.ComplexName, r.BuildingId, r.BuildingName, r.Side);

        FacadeHierarchyStore.Upsert(RootPath, new FacadeClassificationEntry
        {
            Key = FacadeHierarchyStore.KeyFor(r.FolderPath, r.FacadeId),
            FacadeId = r.FacadeId,
            ComplexId = r.ComplexId,
            ComplexName = r.ComplexName,
            BuildingId = r.BuildingId,
            BuildingName = r.BuildingName,
            Side = r.Side,
        });
    }

    private static void ApplyClassification(
        FacadeItemViewModel facade, string complexId, string complexName,
        string? buildingId, string? buildingName, string side)
    {
        facade.ComplexId = complexId;
        facade.ComplexName = complexName;
        facade.BuildingId = buildingId;
        facade.BuildingName = buildingName;
        facade.Side = side;
    }

    /// <summary>이미 등록된 facade 하나를 다른 단지/동/방위로 재분류 — 같은
    /// FacadeClassifyDialog를 단일 항목으로 재사용.</summary>
    [RelayCommand]
    private void ReclassifyFacade(FacadeItemViewModel facade)
    {
        var candidates = new List<(string, string, string)>
        {
            (facade.SourceFolderPath ?? "", facade.FacadeId, facade.Side),
        };
        var proposedComplexName = facade.ComplexId == "__UNSORTED__" ? "" : facade.ComplexName;
        var classifyDialog = new FacadeClassifyDialog(candidates, proposedComplexName, facade.BuildingName ?? "")
        {
            Owner = Application.Current.MainWindow,
        };
        if (classifyDialog.ShowDialog() != true)
            return;

        foreach (var result in classifyDialog.ViewModel.Result)
            AddRunnableCandidate(result);

        RebuildFacadeTree();
    }

    [RelayCommand]
    private void SelectFacade(FacadeItemViewModel facade) => SelectedFacade = facade;

    private Views.RemoteAnalysisJobsWindow? _remoteJobsWindow;

    /// <summary>Opens (or re-activates) the "원격 분석 작업" window -- a separate window, not a
    /// tab, per the 2026-08-27 requirement that incoming FacadePreviewer analysis commands show
    /// in their own list window rather than mixed into the main facade list directly.</summary>
    [RelayCommand]
    private void OpenRemoteJobsWindow()
    {
        if (_remoteJobsWindow != null)
        {
            _remoteJobsWindow.Activate();
            return;
        }
        _remoteJobsWindow = new Views.RemoteAnalysisJobsWindow(RemoteJobs) { Owner = Application.Current.MainWindow };
        _remoteJobsWindow.Closed += (_, _) => _remoteJobsWindow = null;
        _remoteJobsWindow.Show();
    }

    /// <summary>RootPath/facade_hierarchy.json에 저장된 분류를 지금 메모리에 있는
    /// Facades에 적용한다. "+ 폴더"로 추가했던 facade는 SourceFolderPath가 facades/
    /// 바깥일 수 있어 재시작 시 RescanFacadeOutputs(facades/* 스캔)로는 다시 발견되지
    /// 않으므로, 분류 저장소에 남아있는 실제(절대) 폴더 경로를 근거로 다시 등록한다 —
    /// 안 그러면 분류 데이터만 남고 정작 facade 자체가 트리에서 사라진다.</summary>
    private void ApplyFacadeClassifications()
    {
        var index = FacadeHierarchyStore.Load(RootPath);

        foreach (var entry in index.Facades)
        {
            if (!Path.IsPathRooted(entry.Key) || !Directory.Exists(entry.Key))
                continue; // 합성 키("facades/{id}")이거나 폴더가 사라짐 -- 지어내지 않음
            var facade = GetOrCreateFacade(entry.FacadeId);
            facade.SourceFolderPath = entry.Key;
            ApplyClassification(facade, entry.ComplexId, entry.ComplexName, entry.BuildingId, entry.BuildingName, entry.Side);
        }

        foreach (var facade in Facades)
        {
            var key = FacadeHierarchyStore.KeyFor(facade.SourceFolderPath, facade.FacadeId);
            var entry = index.Facades.FirstOrDefault(e => e.Key == key);
            if (entry != null)
                ApplyClassification(facade, entry.ComplexId, entry.ComplexName, entry.BuildingId, entry.BuildingName, entry.Side);
        }
    }

    /// <summary>Facades(평면)를 단지→동(선택)→방위 트리로 재구성. 구조가 실제로 바뀔 때만
    /// 호출 — RescanFacadeOutputs의 2초 폴링에서는 호출하지 않는다(FacadeTree 문서 참고).
    /// "미분류"는 정렬 순서와 무관하게 항상 맨 뒤에 고정.</summary>
    private void RebuildFacadeTree()
    {
        FacadeTree.Clear();

        var byComplex = Facades
            .GroupBy(f => (f.ComplexId, f.ComplexName))
            .OrderBy(g => g.Key.ComplexId == "__UNSORTED__" ? 1 : 0)
            .ThenBy(g => g.Key.ComplexName, StringComparer.Ordinal);

        foreach (var complexGroup in byComplex)
        {
            var complexNode = new ComplexNode { ComplexId = complexGroup.Key.ComplexId, ComplexName = complexGroup.Key.ComplexName };

            var withBuilding = complexGroup.Where(f => !string.IsNullOrEmpty(f.BuildingId));
            var withoutBuilding = complexGroup.Where(f => string.IsNullOrEmpty(f.BuildingId));

            var buildingGroups = withBuilding
                .GroupBy(f => (f.BuildingId, f.BuildingName))
                .OrderBy(g => g.Key.BuildingName, StringComparer.Ordinal);
            foreach (var buildingGroup in buildingGroups)
            {
                var buildingNode = new BuildingNode
                {
                    BuildingId = buildingGroup.Key.BuildingId!,
                    BuildingName = buildingGroup.Key.BuildingName ?? buildingGroup.Key.BuildingId!,
                };
                foreach (var sideGroup in BuildSideGroups(buildingGroup))
                    buildingNode.Children.Add(sideGroup);
                complexNode.Children.Add(buildingNode);
            }

            foreach (var sideGroup in BuildSideGroups(withoutBuilding))
                complexNode.Children.Add(sideGroup);

            FacadeTree.Add(complexNode);
        }
    }

    private static IEnumerable<SideGroupNode> BuildSideGroups(IEnumerable<FacadeItemViewModel> facades)
    {
        return facades
            .GroupBy(f => f.Side)
            .OrderBy(g => g.Key, StringComparer.Ordinal)
            .Select(g =>
            {
                var node = new SideGroupNode { Side = g.Key };
                foreach (var f in g.OrderBy(f => f.FacadeId, StringComparer.Ordinal))
                    node.Facades.Add(f);
                return node;
            });
    }

    /// <summary>Runs tools/stitch_folder.py for one facade's source folder as a
    /// background process. Progress and the growing live-preview mosaic are picked
    /// up separately through the normal log tailer (PREVIEW_UPDATED / MATCH_GEOMETRY
    /// events) — this command only owns process lifetime and IsRunning.</summary>
    [RelayCommand]
    private async Task RunFacade(FacadeItemViewModel facade)
    {
        if (facade.IsRunning || string.IsNullOrEmpty(facade.SourceFolderPath))
            return;

        facade.IsRunning = true;
        facade.LivePreviewImagePath = null;

        // Waits here (not before setting IsRunning above) if this workstation is already at
        // MaxConcurrent -- the facade still shows "진행 중" while queued, matching how a
        // remote-triggered job would report AnalysisJobQueued instead of a silent block.
        await _concurrency.WaitForSlotAsync();

        var baseOutputDir = Path.Combine(facade.SourceFolderPath, "output");
        var (versionLabel, versionDir) = FacadeVersionStore.AllocateNextVersionDir(baseOutputDir);
        var succeeded = false;
        try
        {
            var scriptPath = Path.Combine(RootPath, "tools", "stitch_folder.py");
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
            psi.ArgumentList.Add(facade.SourceFolderPath);
            psi.ArgumentList.Add(facade.FacadeId);
            // 새 버전 폴더에 결과를 쓴다 — 재실행해도 이전 버전을 덮어쓰지 않음
            // (FacadeVersionStore.AllocateNextVersionDir이 디스크의 실제 Vnnn
            // 폴더를 스캔해서 번호를 정하므로 실패한 실행이 남긴 부분 폴더와도
            // 충돌하지 않는다).
            psi.ArgumentList.Add("--output-dir");
            psi.ArgumentList.Add(versionDir);

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
                    facade.AddIssue($"[ERROR] 파이프라인 실행 실패 (exit {process.ExitCode}): {SummarizePythonError(stderr)}");
                }
                else
                {
                    succeeded = true;
                }
            }
            finally
            {
                ChildProcessRegistry.Unregister(process);
            }
        }
        catch (Exception ex)
        {
            facade.AddIssue($"[ERROR] 파이프라인을 시작할 수 없습니다: {ex.Message}");
        }
        finally
        {
            facade.IsRunning = false;
            facade.LivePreviewImagePath = null;
            _concurrency.Release();

            // 실패 시 current를 전진시키지 않는다 — 화면/이후 단계는 자동으로
            // 이전 성공 버전을 계속 가리킨다 (atomic_io.py의 "실패 시 버전을
            // 늘리지 않는다" 원칙을 실행-버전 단위로 확장).
            if (succeeded)
                FacadeVersionStore.RecordVersionSuccess(baseOutputDir, versionLabel, LoggedInUsername);

            // --in-place output lands at <SourceFolderPath>/output/, which is
            // outside RootPath/facades/ — RescanFacadeOutputs() (facades/* only)
            // won't find it, so read this facade's own output dir directly.
            var currentDir = FacadeVersionStore.ResolveCurrentDir(baseOutputDir);
            var snap = FacadeOutputScanner.ScanOne(facade.FacadeId, currentDir);
            if (snap != null)
                ApplySnapshot(facade, snap);
            RefreshRunVersions(facade, baseOutputDir);

            RescanFacadeOutputs();

            // ApplySnapshot/RescanFacadeOutputs 위에서 이전 성공 버전 기준으로 상태를
            // Done/NeedsManualReview로 되돌렸을 수 있다(실패해도 current는 그 버전을
            // 그대로 가리키므로) — 방금 실행이 실패했다는 사실을 마지막에 다시 덮어써서
            // 화면이 "진행 중" 문구에 멈춰 보이지 않게 한다. 라이브 로그가 구조화된
            // FAILED_GEOMETRY 이벤트 없이(예: Python 처리되지 않은 예외) 죽으면
            // OverallStatus/CurrentStageLabel이 마지막으로 본 단계에 멈춰버리는 문제였음.
            if (!succeeded)
            {
                facade.OverallStatus = FacadeOverallStatus.Failed;
                facade.CurrentStageLabel = "실패 (마지막 실행 오류 — 아래 이슈 확인)";
            }
        }
    }

    /// <summary>Python 서브프로세스 stderr에서 실제로 쓸모있는 에러 정보를 뽑아낸다.
    /// 기존 코드는 stderr의 "첫" 줄을 보여줬는데, 처리 안 된 Python 예외의 첫 줄은
    /// 거의 항상 "Traceback (most recent call last):" 뿐이라 원인도 위치도 전혀
    /// 안 보였다(실제로 사용자가 이 문구만 보고 리포트함). Python traceback은
    /// 마지막 줄이 실제 예외 메시지("ExceptionType: message")이고, 그 위의 마지막
    /// "File "...", line N, in ..." 줄이 예외가 실제로 발생한 위치이므로 그 둘을
    /// 뽑아서 "원인 [위치]" 형태로 합친다.</summary>
    private static string SummarizePythonError(string stderr)
    {
        var lines = stderr
            .Split('\n')
            .Select(l => l.TrimEnd('\r'))
            .Where(l => !string.IsNullOrWhiteSpace(l))
            .ToList();
        if (lines.Count == 0)
            return "(stderr 출력 없음)";

        var message = lines[^1].Trim();
        var location = lines.LastOrDefault(l => l.TrimStart().StartsWith("File \"", StringComparison.Ordinal))?.Trim();
        return location != null ? $"{message}  [위치: {location}]" : message;
    }

    /// <summary>Same "+ 폴더"-vs-facades/ output-location split RunFacade already
    /// has to reason about (see its comment above) — a facade run in-place has its
    /// output under SourceFolderPath, one only ever seen via RescanFacadeOutputs
    /// lives under facades/{id}/output.</summary>
    private string GetFacadeOutputDir(FacadeItemViewModel facade) =>
        !string.IsNullOrEmpty(facade.SourceFolderPath)
            ? Path.Combine(facade.SourceFolderPath, "output")
            : Path.Combine(RootPath, "facades", facade.FacadeId, "output");

    /// <summary>Second stage of the 분석·스티칭 tab's chained pipeline (스티칭 →
    /// COLMAP → 크랙탐지 → PDF): runs tools/detect_cracks_folder.py against the
    /// facade's already-stitched mosaic. Same Process-launch pattern as RunFacade
    /// (register with ChildProcessRegistry, drain both stdout+stderr before
    /// awaiting exit to avoid the pipe-deadlock RunFacade/AiTrainingViewModel hit
    /// previously).</summary>
    [RelayCommand]
    private async Task DetectCracks(FacadeItemViewModel facade)
    {
        if (facade.IsRunning || facade.IsDetectingCracks || !facade.HasMosaic)
            return;

        facade.IsDetectingCracks = true;
        var baseOutputDir = GetFacadeOutputDir(facade);
        var succeeded = false;
        try
        {
            var outputDir = FacadeVersionStore.ResolveCurrentDir(baseOutputDir);
            var scriptPath = Path.Combine(RootPath, "tools", "detect_cracks_folder.py");
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
            psi.ArgumentList.Add(outputDir);
            psi.ArgumentList.Add(facade.FacadeId);

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
                    facade.AddIssue($"[ERROR] 크랙 검사 실패 (exit {process.ExitCode}): {SummarizePythonError(stderr)}");
                }
                else
                {
                    succeeded = true;
                }
            }
            finally
            {
                ChildProcessRegistry.Unregister(process);
            }
        }
        catch (Exception ex)
        {
            facade.AddIssue($"[ERROR] 크랙 검사를 시작할 수 없습니다: {ex.Message}");
        }
        finally
        {
            facade.IsDetectingCracks = false;
            FacadeVersionStore.UpdateStageStatus(baseOutputDir, "crack", succeeded ? "OK" : "FAILED");
            var currentDir = FacadeVersionStore.ResolveCurrentDir(baseOutputDir);
            var snap = FacadeOutputScanner.ScanOne(facade.FacadeId, currentDir);
            if (snap != null)
                ApplySnapshot(facade, snap);
            RefreshRunVersions(facade, baseOutputDir);
        }
    }

    /// <summary>Third/final stage: builds "{facade_id}_report.pdf" via
    /// tools/generate_report.py (HTML/CSS rendered to PDF through WeasyPrint,
    /// src/report/pdf_report.py) as a background process — same Process-launch
    /// pattern RunFacade/DetectCracks already use. Moved off the previous
    /// in-process C#/PDFsharp-MigraDoc generator: MigraDoc's Word-style
    /// paragraph/table layout model could not produce the print-quality,
    /// designed cover-page look the reference template needed, while HTML/CSS
    /// does directly. This app was never actually Python-free anyway —
    /// stitching and crack detection already shell out to the same Python env.</summary>
    [RelayCommand]
    private async Task GenerateReport(FacadeItemViewModel facade)
    {
        if (facade.IsGeneratingReport || !facade.HasCrackResults)
            return;

        facade.IsGeneratingReport = true;
        var baseOutputDir = GetFacadeOutputDir(facade);
        var succeeded = false;
        try
        {
            var outputDir = FacadeVersionStore.ResolveCurrentDir(baseOutputDir);
            var scriptPath = Path.Combine(RootPath, "tools", "generate_report.py");
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
            psi.ArgumentList.Add("facade");
            psi.ArgumentList.Add(outputDir);
            psi.ArgumentList.Add(facade.FacadeId);

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
                    facade.AddIssue($"[ERROR] 보고서 생성 실패 (exit {process.ExitCode}): {SummarizePythonError(stderr)}");
                }
                else
                {
                    facade.ReportPath = Path.Combine(outputDir, $"{facade.FacadeId}_report.pdf");
                    facade.HasReport = true;
                    succeeded = true;
                }
            }
            finally
            {
                ChildProcessRegistry.Unregister(process);
            }
        }
        catch (Exception ex)
        {
            facade.AddIssue($"[ERROR] 보고서 생성을 시작할 수 없습니다: {ex.Message}");
        }
        finally
        {
            facade.IsGeneratingReport = false;
            FacadeVersionStore.UpdateStageStatus(baseOutputDir, "report", succeeded ? "OK" : "FAILED");
            RefreshRunVersions(facade, baseOutputDir);
        }
    }

    /// <summary>Recursively collects every FacadeItemViewModel leaf under a
    /// ComplexNode/BuildingNode/SideGroupNode — Children is polymorphic
    /// (ObservableCollection&lt;object&gt;, see FacadeTreeNodes.cs) since a
    /// Complex can mix classified-with-Building and classified-without-Building
    /// facades side by side (동 is optional).</summary>
    private static IEnumerable<FacadeItemViewModel> FlattenFacades(object node)
    {
        switch (node)
        {
            case ComplexNode complex:
                foreach (var child in complex.Children)
                    foreach (var f in FlattenFacades(child))
                        yield return f;
                break;
            case BuildingNode building:
                foreach (var child in building.Children)
                    foreach (var f in FlattenFacades(child))
                        yield return f;
                break;
            case SideGroupNode side:
                foreach (var f in side.Facades)
                    if (f is FacadeItemViewModel facade)
                        yield return facade;
                break;
        }
    }

    /// <summary>Triggered from the FACADES tree's Complex/Building node headers —
    /// aggregates every facade underneath into one combined PDF via
    /// tools/generate_report.py's "building" mode. The facade list (with each
    /// facade's already-resolved CURRENT version dir — this method owns that
    /// resolution, same division of responsibility RunFacade/DetectCracks have
    /// with their own scripts) is handed over as a temp manifest JSON file
    /// rather than one argument per facade, since a large complex can have far
    /// more facades than comfortably fits a command line. Follows this
    /// codebase's existing no-CanExecute convention: guard in the body +
    /// StatusText, same as every other command here, rather than a
    /// bound-disabled button.</summary>
    [RelayCommand]
    private async Task GenerateBuildingReport(object node)
    {
        var facades = FlattenFacades(node).ToList();
        if (facades.Count == 0)
        {
            StatusText = "종합보고서: 포함할 facade가 없습니다.";
            return;
        }

        string complexName;
        string? buildingName;
        switch (node)
        {
            case ComplexNode complex:
                complexName = complex.ComplexName;
                buildingName = null;
                break;
            case BuildingNode building:
                // BuildingNode 자체는 어느 단지 소속인지 모르므로(부모 포인터 없음),
                // 그 밑 facade 아무거나에서 ComplexName을 그대로 읽는다 — RebuildFacadeTree가
                // 이미 같은 (ComplexId, ComplexName)끼리만 묶어서 만들었으므로 안전.
                complexName = facades[0].ComplexName;
                buildingName = building.BuildingName;
                break;
            default:
                return;
        }

        var reportsDir = Path.Combine(RootPath, "reports");
        Directory.CreateDirectory(reportsDir);

        var manifest = new
        {
            complex_name = complexName,
            building_name = buildingName,
            facades = facades.Select(f => new
            {
                facade_id = f.FacadeId,
                side = f.Side,
                output_dir = FacadeVersionStore.ResolveCurrentDir(GetFacadeOutputDir(f)),
            }).ToList(),
        };
        var manifestPath = Path.Combine(Path.GetTempPath(), $"checkcrack_report_manifest_{Guid.NewGuid():N}.json");
        await File.WriteAllTextAsync(manifestPath, JsonSerializer.Serialize(manifest));

        try
        {
            var scriptPath = Path.Combine(RootPath, "tools", "generate_report.py");
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
            psi.ArgumentList.Add("building");
            psi.ArgumentList.Add(manifestPath);
            psi.ArgumentList.Add(reportsDir);

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
                    StatusText = $"종합보고서 생성 실패: {SummarizePythonError(stderr)}";
                }
                else
                {
                    var safeLabel = SanitizeFileName(buildingName != null ? $"{complexName}_{buildingName}" : complexName);
                    var reportPath = Path.Combine(reportsDir, $"{safeLabel}_종합보고서.pdf");
                    StatusText = $"종합보고서 생성 완료: {reportPath}";
                }
            }
            finally
            {
                ChildProcessRegistry.Unregister(process);
            }
        }
        catch (Exception ex)
        {
            StatusText = $"종합보고서 생성을 시작할 수 없습니다: {ex.Message}";
        }
        finally
        {
            try { File.Delete(manifestPath); } catch { /* best-effort cleanup */ }
        }
    }

    private static string SanitizeFileName(string name)
    {
        foreach (var c in Path.GetInvalidFileNameChars())
            name = name.Replace(c, '_');
        return name;
    }

    [RelayCommand]
    private void OpenReport(FacadeItemViewModel facade)
    {
        if (string.IsNullOrEmpty(facade.ReportPath) || !File.Exists(facade.ReportPath))
            return;
        try
        {
            Process.Start(new ProcessStartInfo(facade.ReportPath) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            facade.AddIssue($"[ERROR] PDF를 열 수 없습니다: {ex.Message}");
        }
    }

    private FacadeItemViewModel GetOrCreateFacade(string facadeId)
    {
        var existing = Facades.FirstOrDefault(f => f.FacadeId == facadeId);
        if (existing != null)
            return existing;
        var created = new FacadeItemViewModel { FacadeId = facadeId };
        // keep the list sorted by id so it doesn't reshuffle as events arrive
        var insertAt = Facades.TakeWhile(f => string.Compare(f.FacadeId, facadeId, StringComparison.Ordinal) < 0).Count();
        Facades.Insert(insertAt, created);
        return created;
    }

    private void OnLogEntry(PipelineLogEntry entry)
    {
        LogEntries.Add(entry);
        while (LogEntries.Count > 2000)
            LogEntries.RemoveAt(0);

        if (string.IsNullOrEmpty(entry.FacadeId))
            return; // building-level event (e.g. FACADE_ASSIGNED) — shown in global log only

        var facade = GetOrCreateFacade(entry.FacadeId);
        facade.AddEvent(entry);

        if (entry.ImageCount is int ic)
            facade.ImageCount = ic;
        if (entry.CoverageRatio is double cov)
            facade.CoverageRatio = cov;
        if (entry.GlobalDriftScorePx is double drift)
            facade.GlobalDriftScorePx = drift;
        if (entry.NeedsColmapFallback is bool needsColmap)
            facade.NeedsColmapFallback = needsColmap;
        if (entry.NumImagesRequested is int req)
            facade.ColmapRequested = req;
        if (entry.NumImagesRegistered is int reg)
        {
            facade.ColmapRegistered = reg;
            facade.HasColmapReport = true;
        }
        if (entry.MeanReprojectionErrorPx is double mre)
            facade.ColmapMeanReprojectionErrorPx = mre;
        if (entry.Stage == "PREVIEW_UPDATED" && entry.PreviewPath != null)
            facade.LivePreviewImagePath = entry.PreviewPath;

        var stage = entry.Stage ?? "";
        var label = StageLabels.TryGetValue(stage, out var known) ? known : stage;
        if ((stage == "MATCH_GEOMETRY" || stage == "COLMAP_MAPPING_PROGRESS") && entry.Progress != null)
            label += $"  ({entry.Progress})";
        facade.CurrentStageLabel = label;

        var warnKey = $"{entry.LineNumber}:{entry.Message}";
        var seen = _facadeWarningKeys.TryGetValue(entry.FacadeId, out var set) ? set : (_facadeWarningKeys[entry.FacadeId] = new HashSet<string>());
        if ((entry.Level == "WARNING" || entry.Level == "ERROR") && seen.Add(warnKey))
        {
            var reasonText = entry.Reasons != null && entry.Reasons.Count > 0 ? " — " + string.Join("; ", entry.Reasons) : "";
            facade.AddIssue($"[{entry.Level}] {entry.Message}{reasonText}");
        }

        facade.OverallStatus = stage switch
        {
            "FAILED_GEOMETRY" => FacadeOverallStatus.Failed,
            "NEEDS_MANUAL_REVIEW" => FacadeOverallStatus.NeedsManualReview,
            "DONE" => facade.OverallStatus == FacadeOverallStatus.NeedsManualReview
                ? FacadeOverallStatus.NeedsManualReview
                : FacadeOverallStatus.Done,
            "" => facade.OverallStatus,
            _ => FacadeOverallStatus.InProgress,
        };
    }

    private void RescanFacadeOutputs()
    {
        if (!Directory.Exists(RootPath))
            return;

        List<FacadeSnapshot> snapshots;
        try
        {
            snapshots = FacadeOutputScanner.ScanAll(RootPath);
        }
        catch (IOException)
        {
            return; // transient; retried on the next tick
        }

        foreach (var snap in snapshots)
        {
            var facade = GetOrCreateFacade(snap.FacadeId);

            // A facade currently running (RunFacadeCommand) reports its own live
            // status from the log tailer (OnLogEntry) — that takes priority over
            // whatever a possibly-stale output file from a *previous* run says.
            if (facade.IsRunning)
                continue;

            ApplySnapshot(facade, snap);

            var baseOutputDir = Path.Combine(RootPath, "facades", snap.FacadeId, "output");
            RefreshRunVersions(facade, baseOutputDir);
        }

        RecomputeDashboardCounts();
        RefreshStorageStatus();
        RecomputeActiveJobs();
    }

    /// <summary>"파일 저장소 상태" 카드 — RootPath가 위치한 드라이브의 실제 여유/전체
    /// 용량. UNC 경로나 준비되지 않은 드라이브는 조용히 크래시하지 않고 명확한 문구로
    /// 대체한다(지어낸 숫자를 보여주지 않는다).</summary>
    private void RefreshStorageStatus()
    {
        try
        {
            var fullPath = Path.GetFullPath(RootPath);
            var root = Path.GetPathRoot(fullPath);
            if (string.IsNullOrEmpty(root))
            {
                StorageStatusText = "드라이브를 확인할 수 없습니다.";
                return;
            }

            var drive = new DriveInfo(root);
            if (!drive.IsReady)
            {
                StorageStatusText = $"드라이브 {root} 준비되지 않음.";
                return;
            }

            var freeGb = drive.AvailableFreeSpace / 1024.0 / 1024.0 / 1024.0;
            var totalGb = drive.TotalSize / 1024.0 / 1024.0 / 1024.0;
            StorageStatusText = $"{root}  —  여유 {freeGb:0.0} GB / 전체 {totalGb:0.0} GB";
        }
        catch (Exception ex)
        {
            StorageStatusText = $"디스크 정보를 읽을 수 없습니다: {ex.Message}";
        }
    }

    /// <summary>"처리 Job" 카드 — Facades를 그대로 재사용(복제하지 않음), 3개 실행
    /// 플래그 중 하나라도 true인 facade만 담는다. RecomputeDashboardCounts와 동일한
    /// 2초 주기로만 갱신되므로 시작/종료 반영에 최대 2초 지연이 있을 수 있음 —
    /// 대시보드 카운트가 이미 갖고 있는 것과 동일한 기존 한계, 새로 생기는 문제 아님.</summary>
    private void RecomputeActiveJobs()
    {
        var running = Facades
            .Where(f => f.IsRunning || f.IsDetectingCracks || f.IsGeneratingReport)
            .ToList();

        ActiveJobs.Clear();
        foreach (var f in running)
            ActiveJobs.Add(f);
    }

    /// <summary>Facades를 순회해 12개 대시보드 카드의 Count만 갱신(항목 오브젝트는 재사용).
    /// ClassifyForDashboard가 절대 선택하지 않는 5개 카테고리(촬영예정 등)는 항상 0으로
    /// 남는다 — 이 Viewer에 그 상태들을 판정할 실제 신호가 없기 때문(지어내지 않는다).</summary>
    private void RecomputeDashboardCounts()
    {
        foreach (var c in DashboardCounts)
            c.Count = 0;

        foreach (var facade in Facades)
        {
            var category = ClassifyForDashboard(facade);
            var entry = DashboardCounts.First(c => c.Category == category);
            entry.Count++;
        }
    }

    /// <summary>우선순위 순서 분류 — IsRunning/IsDetectingCracks/IsGeneratingReport가
    /// OverallStatus보다 우선한다("지금 뭘 하고 있는지"가 마지막으로 기록된 결과보다
    /// 더 정확한 실시간 신호이기 때문).
    ///
    /// else 분기(StitchQueued)는 서로 다른 두 실제 상태를 하나로 합친 것이다: (a) 아직
    /// 한 번도 스티칭을 안 한 facade, (b) 스티칭/크랙탐지는 끝났지만 다음 수동 단계
    /// (크랙탐지 또는 보고서 생성)를 아직 안 누른 facade. 요청된 12개 카테고리에
    /// "스티칭 완료·후속 단계 대기"용 별도 항목이 없어서 의도적으로 하나로 합친 것 —
    /// 새 카테고리를 임의로 만들지 않는다.
    ///
    /// "실패"는 대부분 0으로 보인다 — 버그가 아니라 기존 코드의 이미 있는 한계다:
    /// RunFacade가 실패해도 그 finally 블록이 곧바로 마지막 성공 버전을 다시 스캔해서
    /// ApplySnapshot을 호출하고, 그게 OverallStatus를 Done/NeedsManualReview로
    /// 덮어써버린다(FacadeVersionStore의 "실패 시 버전 안 늘림"과 맞물려서). 한 번이라도
    /// 성공한 적 있는 facade는 재실행이 실패해도 Failed가 거의 안 남는다 — 여기서
    /// 고치지 않고 기존 동작 그대로 반영한다(범위 밖).</summary>
    private static DashboardStatusCategory ClassifyForDashboard(FacadeItemViewModel facade)
    {
        if (facade.IsRunning) return DashboardStatusCategory.Stitching;
        if (facade.IsDetectingCracks) return DashboardStatusCategory.AiAnalyzing;
        if (facade.IsGeneratingReport) return DashboardStatusCategory.GeneratingReport;
        if (facade.OverallStatus == FacadeOverallStatus.Failed) return DashboardStatusCategory.Failed;
        if (facade.OverallStatus == FacadeOverallStatus.NeedsManualReview) return DashboardStatusCategory.NeedsManualReview;
        if (facade.HasReport) return DashboardStatusCategory.Done;
        return DashboardStatusCategory.StitchQueued;
    }

    /// <summary>Repopulates facade.RunVersions from output/version_index.json
    /// (most recent first). A no-op — clears to empty — for a facade with no
    /// version history yet (legacy flat output, or nothing run yet).</summary>
    private static void RefreshRunVersions(FacadeItemViewModel facade, string baseOutputDir)
    {
        var history = FacadeVersionStore.GetRunHistory(baseOutputDir);
        facade.RunVersions.Clear();
        foreach (var entry in history)
            facade.RunVersions.Add(entry);
    }

    private static void ApplySnapshot(FacadeItemViewModel facade, FacadeSnapshot snap)
    {
        if (snap.Quality != null)
        {
            facade.ImageCount = snap.Quality.ImageCount;
            facade.CoverageRatio = snap.Quality.CoverageRatio;
            facade.MeanInlierRatio = snap.Quality.MeanInlierRatio;
            facade.GlobalDriftScorePx = snap.Quality.GlobalDriftScorePx;
            facade.NeedsColmapFallback = snap.Quality.NeedsColmapFallback;

            // Status/stage label used to come only from replaying the log's
            // history on every launch (removed — it made old runs flash by
            // as if happening live). Deriving "완료" straight from the
            // output files on disk is the actually-correct source of truth
            // for a facade the app didn't watch complete live.
            facade.OverallStatus = snap.Quality.NeedsColmapFallback == true && snap.QualityColmap == null
                ? FacadeOverallStatus.NeedsManualReview
                : FacadeOverallStatus.Done;
            facade.CurrentStageLabel = facade.OverallStatus == FacadeOverallStatus.NeedsManualReview
                ? "검토 필요 (Drift 감지)"
                : "완료";
        }
        if (snap.Colmap != null)
        {
            facade.HasColmapReport = true;
            facade.ColmapRequested = snap.Colmap.NumImagesRequested;
            facade.ColmapRegistered = snap.Colmap.NumImagesRegistered;
            facade.ColmapMeanReprojectionErrorPx = snap.Colmap.MeanReprojectionErrorPx;
        }
        if (snap.QualityColmap != null)
        {
            facade.HasRectifiedMosaic = true;
            facade.CoverageRatioColmap = snap.QualityColmap.CoverageRatio;
        }
        if (snap.Cracks != null)
        {
            facade.HasCrackResults = true;
            facade.CrackCount = snap.Cracks.Count;
        }
        if (snap.ReportPath != null)
        {
            facade.HasReport = true;
            facade.ReportPath = snap.ReportPath;
        }

        facade.AnalysisImagePath = snap.AnalysisImagePath;
        facade.VisualImagePath = snap.VisualImagePath;
        facade.AnalysisColmapImagePath = snap.AnalysisColmapImagePath;
        facade.VisualColmapImagePath = snap.VisualColmapImagePath;
    }
}
