using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using CheckCrackViewer.Models;

namespace CheckCrackViewer.Services;

public class FacadeSnapshot
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

        return new FacadeSnapshot
        {
            FacadeId = facadeId,
            OutputDir = outputDir,
            Quality = ReadJson<QualityReportModel>(Path.Combine(outputDir, $"{facadeId}_quality_report.json")),
            QualityColmap = ReadJson<QualityReportModel>(Path.Combine(outputDir, $"{facadeId}_quality_report_colmap.json")),
            Colmap = ReadJson<ColmapReportModel>(Path.Combine(outputDir, $"{facadeId}_colmap_report.json")),
            Cracks = ReadJson<List<CrackResultModel>>(Path.Combine(outputDir, $"{facadeId}_cracks.json")),
            AnalysisImagePath = ExistsOrNull(Path.Combine(outputDir, $"{facadeId}_analysis.tif")),
            VisualImagePath = ExistsOrNull(Path.Combine(outputDir, $"{facadeId}_visual.tif")),
            AnalysisColmapImagePath = ExistsOrNull(Path.Combine(outputDir, $"{facadeId}_analysis_colmap.tif")),
            VisualColmapImagePath = ExistsOrNull(Path.Combine(outputDir, $"{facadeId}_visual_colmap.tif")),
            ReportPath = ExistsOrNull(Path.Combine(outputDir, $"{facadeId}_report.pdf")),
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
