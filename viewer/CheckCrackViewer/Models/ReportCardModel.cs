using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Models;

/// <summary>{facade_id}_report_cards.json (src/report/pdf_report.py::_render_with_cards가 보고서 PDF와 함께 쓴다).
/// 2026-09-27 (사용자 요청): 결과 보기의 보고서 화면에서 크랙 카드를 클릭하면 왼쪽에 그 크랙의 원본 사진과
/// 위치가 표시된다. 뷰어는 PDF를 이미지로만 그리므로 "클릭한 곳이 어느 카드인지"는 이 파일이 알려준다.</summary>
public sealed class ReportCardsFile
{
    [JsonPropertyName("version")] public int Version { get; set; }
    [JsonPropertyName("cards")] public List<ReportCardModel> Cards { get; set; } = new();
}

public sealed class ReportCardModel
{
    /// <summary>0부터 시작하는 PDF 페이지 번호(ComparePanelState.ReportPageIndex와 같은 기준).</summary>
    [JsonPropertyName("page")] public int Page { get; set; }
    [JsonPropertyName("crack_id")] public string CrackId { get; set; } = "";
    /// <summary>보고서 카드의 "No.N" 번호.</summary>
    [JsonPropertyName("no")] public int? No { get; set; }

    /// <summary>카드 사각형, 페이지 크기에 대한 비율(0..1) -- 어떤 배율로 그려도 그대로 쓸 수 있다.</summary>
    [JsonPropertyName("x0")] public double X0 { get; set; }
    [JsonPropertyName("y0")] public double Y0 { get; set; }
    [JsonPropertyName("x1")] public double X1 { get; set; }
    [JsonPropertyName("y1")] public double Y1 { get; set; }

    /// <summary>이 크랙을 가장 정면으로 찍은 원본 사진(source_observations[0])과 그 안의 위치
    /// [x0, y0, x1, y1](원본 픽셀). 관리자가 직접 그린 크랙은 사진 근거가 없어 null.</summary>
    [JsonPropertyName("image_id")] public string? ImageId { get; set; }
    [JsonPropertyName("bbox_px_in_source")] public double[]? BboxPxInSource { get; set; }
}
