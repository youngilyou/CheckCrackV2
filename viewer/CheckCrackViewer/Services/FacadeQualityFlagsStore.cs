using System.IO;
using System.Text.Json;
using CheckCrackViewer.Models;

namespace CheckCrackViewer.Services;

/// <summary>Owns {facade_id}_quality_flags.json -- same atomic temp-file-then-rename write
/// pattern as CrackReviewStore/FacadeVersionStore use for their own JSON files.</summary>
public static class FacadeQualityFlagsStore
{
    private static readonly JsonSerializerOptions SerializerOptions = new() { WriteIndented = true };

    /// <summary>2026-08-29: 운영자 요청("정밀촬영 당신이 먼저 의심된다고 판단되면 당신이
    /// 먼저 판단, 최종 운용자 판단")에 따른 초기 자동 의심 기준 -- 제가 제안한 값이며,
    /// 프로젝트가 이미 확정한 임계값이 아니므로 실제 운용 데이터로 맞지 않으면 조정
    /// 필요. 두 가지 독립 신호 중 하나라도 걸리면 의심:
    ///
    /// 1) MeanInlierRatio &lt; 0.35 -- config/pipeline.yaml의 min_inlier_ratio와 동일한
    ///    값을 재사용(이미 이 프로젝트가 "이 정도면 매칭이 부실하다"고 정해둔 기준).
    ///    체인 스티칭 단계의 매칭 신뢰도가 이 밑이면, COLMAP 보정 이후에도 원본 사진
    ///    자체의 특징점 부족(각도/거리/블러)이 의심됨.
    /// 2) COLMAP 등록률(ColmapRegistered/ColmapRequested) &lt; 0.7 -- 요청한 사진 중
    ///    70% 미만만 COLMAP에 실제로 등록됐다면, 나머지 사진들이 기하학적으로 안 맞을
    ///    만큼 촬영 품질에 문제가 있다는 구체적 신호(실제 BACK 촬영 건에서 16/33=48%로
    ///    관측된 것과 같은 종류의 문제).</summary>
    public const double SuspectMeanInlierRatioBelow = 0.35;
    public const double SuspectColmapRegistrationRatioBelow = 0.7;

    public static bool SuggestNeedsDetailCapture(double? meanInlierRatio, int colmapRequested, int colmapRegistered)
    {
        if (meanInlierRatio.HasValue && meanInlierRatio.Value < SuspectMeanInlierRatioBelow)
            return true;
        if (colmapRequested > 0 && (double)colmapRegistered / colmapRequested < SuspectColmapRegistrationRatioBelow)
            return true;
        return false;
    }

    private static string PathFor(string outputDir, string facadeId) =>
        Path.Combine(outputDir, $"{facadeId}_quality_flags.json");

    public static FacadeQualityFlagsFile? Load(string outputDir, string facadeId)
    {
        var path = PathFor(outputDir, facadeId);
        if (!File.Exists(path))
            return null;
        try
        {
            using var stream = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return JsonSerializer.Deserialize<FacadeQualityFlagsFile>(stream);
        }
        catch (JsonException)
        {
            return null; // partially-written/corrupt -- treat as "no flags yet"
        }
        catch (IOException)
        {
            return null;
        }
    }

    public static void Save(string outputDir, string facadeId, FacadeQualityFlagsFile file)
    {
        Directory.CreateDirectory(outputDir);
        var path = PathFor(outputDir, facadeId);
        var tempPath = path + ".tmp";
        File.WriteAllText(tempPath, JsonSerializer.Serialize(file, SerializerOptions));
        File.Move(tempPath, path, overwrite: true);
    }

    /// <summary>Called once per scan (FacadeOutputScanner.ScanOne) with whatever quality
    /// signals are currently available. Loads the existing file if present; if none exists
    /// yet, or the existing one is still just the untouched auto-suggestion (never confirmed/
    /// overridden by an operator), recomputes NeedsDetailCapture and persists it -- once an
    /// operator has touched it (DetailCaptureAutoSuggested == false), this never overwrites
    /// their decision again, no matter how the quality numbers change on a later re-run.</summary>
    public static FacadeQualityFlagsFile Reconcile(
        string outputDir, string facadeId, double? meanInlierRatio, int colmapRequested, int colmapRegistered)
    {
        var existing = Load(outputDir, facadeId);
        if (existing != null && !existing.DetailCaptureAutoSuggested)
            return existing; // operator already made the final call -- never re-suggest over it

        var suggested = SuggestNeedsDetailCapture(meanInlierRatio, colmapRequested, colmapRegistered);
        var file = new FacadeQualityFlagsFile
        {
            NeedsRetake = existing?.NeedsRetake ?? false, // purely operator-set, never auto-touched
            NeedsDetailCapture = suggested,
            DetailCaptureAutoSuggested = true,
        };

        // Avoid rewriting the file every single scan when nothing actually changed --
        // this runs on a 2s poll timer (FacadeOutputScanner's own doc comment), a needless
        // write every tick would just be disk churn for a value that's usually stable.
        if (existing == null || existing.NeedsDetailCapture != file.NeedsDetailCapture || existing.NeedsRetake != file.NeedsRetake)
            Save(outputDir, facadeId, file);

        return file;
    }
}
