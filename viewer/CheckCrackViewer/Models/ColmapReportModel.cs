using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Models;

/// <summary>Maps {facade_id}_colmap_report.json (src/sfm/colmap_runner.py: ColmapResult).</summary>
public class ColmapReportModel
{
    [JsonPropertyName("facade_id")]
    public string FacadeId { get; set; } = "";

    [JsonPropertyName("num_images_requested")]
    public int NumImagesRequested { get; set; }

    [JsonPropertyName("num_images_registered")]
    public int NumImagesRegistered { get; set; }

    [JsonPropertyName("registered_image_names")]
    public List<string> RegisteredImageNames { get; set; } = new();

    [JsonPropertyName("num_points3d")]
    public int NumPoints3d { get; set; }

    [JsonPropertyName("mean_reprojection_error_px")]
    public double? MeanReprojectionErrorPx { get; set; }

    [JsonPropertyName("sparse_dir")]
    public string? SparseDir { get; set; }
}
