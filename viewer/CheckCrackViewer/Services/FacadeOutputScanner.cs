using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using CheckCrackViewer.Models;
using CommunityToolkit.Mvvm.ComponentModel;

namespace CheckCrackViewer.Services;

/// <summary>2026-08-29: ObservableObject로 전환(NeedsRetake/NeedsDetailCapture만 [ObservableProperty]
/// -- 나머지 필드는 그대로 plain get/set). 결과 목록(ResultsCompareView)의 체크박스가 IsChecked를
/// TwoWay 바인딩하면서 화면이 즉시 갱신되려면 PropertyChanged 알림이 필요했음(기존 plain class로는
/// 체크는 되지만 다른 바인딩 갱신 트리거가 없었음). 저장(파일 쓰기)은 이 클래스 자체가 하지 않고
/// ResultsCompareViewModel의 Command가 명시적으로 호출 -- rescan(CopySnapshot)이 매 2초마다
/// 같은 값을 다시 대입할 때마다 불필요하게 재저장되는 것을 피하기 위해 property-changed를
/// "저장 트리거"로 쓰지 않음(MVVM Toolkit의 SetProperty는 값이 실제로 바뀔 때만 알림을 내므로
/// 값이 같으면 어차피 무시되긴 하지만, 저장 책임 자체를 명시적 Command로 분리하는 게 더 명확함).</summary>
public partial class FacadeSnapshot : ObservableObject
{
    public string FacadeId { get; set; } = "";
    /// <summary>The resolved output dir this snapshot was actually read from --
    /// lets consumers (e.g. the crack review feature) write a sibling file
    /// next to {facade_id}_cracks.json without re-deriving the path from
    /// AnalysisImagePath's directory.</summary>
    public string? OutputDir { get; set; }

    // --- 단지/동/방위 분류 (표시용) -- FacadeItemViewModel의 같은 필드와 동일한
    // 기본값/의미. 결과보기 화면(ResultsCompareViewModel)이 분석·스티칭 화면(FACADES
    // 트리)과 같은 분류 데이터를 보여주도록 여기서 함께 채워서 두 화면의 트리가
    // 항상 일치하게 한다(사용자가 스크린샷 두 장으로 지적한 불일치 문제).
    public string ComplexId { get; set; } = "__UNSORTED__";
    public string ComplexName { get; set; } = "미분류";
    public string? BuildingId { get; set; }
    public string? BuildingName { get; set; }
    public string Side { get; set; } = "";
    /// <summary>"+ 폴더"로 추가된 facade만 채워짐 -- FacadeHierarchyStore.KeyFor의
    /// 분류 키 계산에 필요(MainViewModel.ApplyFacadeClassifications과 동일 규칙).</summary>
    public string? SourceFolderPath { get; set; }

    public QualityReportModel? Quality { get; set; }
    public QualityReportModel? QualityColmap { get; set; }
    public ColmapReportModel? Colmap { get; set; }
    public List<CrackResultModel>? Cracks { get; set; }

    // 2026-08-29: 운영자가 직접(누락 재촬영 필요) 또는 앱의 초기 의심 판정 + 운영자 최종
    // 확인(정밀촬영 필요)으로 채워짐 -- {facade_id}_quality_flags.json, FacadeQualityFlagsStore
    // 참고. 결과보기 화면(이 FacadeSnapshot 자체가 결과 목록의 leaf)과 분석·스티칭 화면
    // (FacadeItemViewModel, MainViewModel.ApplySnapshot이 여기서 복사)이 OutputDir+FacadeId를
    // 키로 하는 같은 파일을 읽고 쓰므로, 두 화면 중 어디서 체크해도 즉시 일관됨.
    [ObservableProperty] private bool _needsRetake;
    [ObservableProperty] private bool _needsDetailCapture;

    public string? AnalysisImagePath { get; set; }
    public string? VisualImagePath { get; set; }
    public string? AnalysisColmapImagePath { get; set; }
    public string? VisualColmapImagePath { get; set; }
    public string? ReportPath { get; set; }
}

/// <summary>Reads facades/*/output/*.json + checks which mosaic images exist.
/// Pure file scan, no caching — called on a slow poll timer, not per-frame.</summary>
public static class FacadeOutputScanner
{
    public static List<FacadeSnapshot> ScanAll(string rootDir)
    {
        var results = new List<FacadeSnapshot>();
        var seenFacadeIds = new HashSet<string>();
        var index = FacadeHierarchyStore.Load(rootDir);

        var facadesDir = Path.Combine(rootDir, "facades");
        if (Directory.Exists(facadesDir))
        {
            foreach (var dir in Directory.GetDirectories(facadesDir))
            {
                var facadeId = Path.GetFileName(dir);
                var baseOutputDir = Path.Combine(dir, "output");
                var resolvedDir = FacadeVersionStore.ResolveCurrentDir(baseOutputDir);
                var snap = ScanOne(facadeId, resolvedDir);
                if (snap != null)
                {
                    ApplyClassification(snap, index);
                    results.Add(snap);
                    seenFacadeIds.Add(facadeId);
                }
            }
        }

        // "+ 폴더"로 추가된 facade는 SourceFolderPath가 facades/ 바깥(또는 facades/ 밑
        // 두 단계 이상 깊이, 예: facades/SU_APARTMENT/TOP)일 수 있어 위 한 단계 스캔으로는
        // 안 잡힌다 — 분류 저장소(FacadeHierarchyStore)에 남은 실제 폴더 경로를 근거로
        // 마저 스캔한다. MainViewModel.ApplyFacadeClassifications이 Facades 컬렉션에
        // 대해 하는 것과 같은 이유를 여기(ScanAll의 모든 소비자 — 분석·스티칭 화면과
        // 결과보기 화면 둘 다)에 적용한 것.
        foreach (var entry in index.Facades)
        {
            if (seenFacadeIds.Contains(entry.FacadeId))
                continue;
            if (!Path.IsPathRooted(entry.Key) || !Directory.Exists(entry.Key))
                continue;

            var baseOutputDir = Path.Combine(entry.Key, "output");
            var resolvedDir = FacadeVersionStore.ResolveCurrentDir(baseOutputDir);
            var snap = ScanOne(entry.FacadeId, resolvedDir);
            if (snap != null)
            {
                snap.SourceFolderPath = entry.Key;
                ApplyClassification(snap, index);
                results.Add(snap);
                seenFacadeIds.Add(entry.FacadeId);
            }
        }

        return results;
    }

    /// <summary>Same key rule as MainViewModel.ApplyFacadeClassifications/
    /// ReclassifyFacade: rooted SourceFolderPath when known, else the synthetic
    /// "facades/{facadeId}" key -- so 결과보기's tree groups facades under the
    /// exact same 단지/동/방위 the 분석·스티칭 FACADES tree does, instead of each
    /// screen deriving its own (previously: 결과보기 showed no grouping at all).</summary>
    private static void ApplyClassification(FacadeSnapshot snap, FacadeHierarchyIndex index)
    {
        var key = FacadeHierarchyStore.KeyFor(snap.SourceFolderPath, snap.FacadeId);
        var entry = index.Facades.FirstOrDefault(e => e.Key == key);
        if (entry == null)
            return;
        snap.ComplexId = entry.ComplexId;
        snap.ComplexName = entry.ComplexName;
        snap.BuildingId = entry.BuildingId;
        snap.BuildingName = entry.BuildingName;
        snap.Side = entry.Side;
    }

    /// <summary>Scans one facade's own output/ folder directly — used both by
    /// ScanAll (facades/*/output/) and by the "+ 폴더" in-place run flow, where
    /// output lands at <sourceFolder>/output/ instead of under facades/.</summary>
    public static FacadeSnapshot? ScanOne(string facadeId, string outputDir)
    {
        if (!Directory.Exists(outputDir))
            return null;

        var quality = ReadJson<QualityReportModel>(Path.Combine(outputDir, $"{facadeId}_quality_report.json"));
        var colmap = ReadJson<ColmapReportModel>(Path.Combine(outputDir, $"{facadeId}_colmap_report.json"));
        var flags = FacadeQualityFlagsStore.Reconcile(
            outputDir, facadeId, quality?.MeanInlierRatio, colmap?.NumImagesRequested ?? 0, colmap?.NumImagesRegistered ?? 0);

        return new FacadeSnapshot
        {
            FacadeId = facadeId,
            OutputDir = outputDir,
            Quality = quality,
            QualityColmap = ReadJson<QualityReportModel>(Path.Combine(outputDir, $"{facadeId}_quality_report_colmap.json")),
            Colmap = colmap,
            Cracks = ReadJson<List<CrackResultModel>>(Path.Combine(outputDir, $"{facadeId}_cracks.json")),
            AnalysisImagePath = ExistsOrNull(Path.Combine(outputDir, $"{facadeId}_analysis.tif")),
            VisualImagePath = ExistsOrNull(Path.Combine(outputDir, $"{facadeId}_visual.tif")),
            AnalysisColmapImagePath = ExistsOrNull(Path.Combine(outputDir, $"{facadeId}_analysis_colmap.tif")),
            VisualColmapImagePath = ExistsOrNull(Path.Combine(outputDir, $"{facadeId}_visual_colmap.tif")),
            ReportPath = ExistsOrNull(Path.Combine(outputDir, $"{facadeId}_report.pdf")),
            NeedsRetake = flags.NeedsRetake,
            NeedsDetailCapture = flags.NeedsDetailCapture,
        };
    }

    private static T? ReadJson<T>(string path) where T : class
    {
        if (!File.Exists(path))
            return null;
        try
        {
            using var stream = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return JsonSerializer.Deserialize<T>(stream);
        }
        catch (JsonException)
        {
            return null; // partially-written file (pipeline mid-write) — retried next scan
        }
        catch (IOException)
        {
            return null;
        }
    }

    private static string? ExistsOrNull(string path) => File.Exists(path) ? path : null;
}
