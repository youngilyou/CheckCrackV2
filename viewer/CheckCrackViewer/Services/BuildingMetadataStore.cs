using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Services;

/// <summary>한 동(Building)의 총 층수/층고 -- 입면도 스타일 층수 표시(왼쪽에 층 라벨)를
/// 만들려면 반드시 필요한 값인데, 지금 이 프로젝트에는 실제 BIM/설계 도면이 없어서 정확한
/// 값을 자동으로 알아낼 방법이 없음(2026-09-17 사용자 확인, memory/floor_labeling_needs_bim.md
/// 참고). 그때까지의 임시 방안으로 사용자가 직접 입력 -- TotalFloors/FloorHeightM 둘 다
/// null이면 "아직 입력 안 됨"이라는 뜻이고, 층수 표시 기능은 이 값이 있을 때만 동작해야 한다.
///
/// 사용자 확정: "나중에 DB 연결되면 고객이 입력한 정보를 읽어 활용함" -- 지금은 로컬 JSON
/// 저장이지만, 나중에 DB 백엔드로 바꿀 때 이 클래스의 Get/Upsert 시그니처만 유지하면 호출부
/// (MainViewModel)는 안 바뀌도록 이 store 뒤로 저장 방식을 감춰둠.</summary>
public sealed class BuildingMetadataEntry
{
    [JsonPropertyName("key")] public string Key { get; set; } = "";
    [JsonPropertyName("complex_id")] public string ComplexId { get; set; } = "";
    [JsonPropertyName("building_id")] public string BuildingId { get; set; } = "";
    [JsonPropertyName("total_floors")] public int? TotalFloors { get; set; }
    [JsonPropertyName("floor_height_m")] public double? FloorHeightM { get; set; }
}

public sealed class BuildingMetadataIndex
{
    [JsonPropertyName("buildings")] public List<BuildingMetadataEntry> Buildings { get; set; } = new();
}

public static class BuildingMetadataStore
{
    private const string FileName = "building_metadata.json";
    private static readonly JsonSerializerOptions SerializerOptions = new() { WriteIndented = true };

    /// <summary>FacadeHierarchyStore.KeyFor와 같은 합성 키 관례 -- 한 단지 안에 같은
    /// 이름의 동이 없다는 보장이 없으므로 ComplexId까지 같이 묶는다.</summary>
    public static string KeyFor(string complexId, string buildingId) => $"{complexId}/{buildingId}";

    private static string IndexPath(string rootPath) => Path.Combine(rootPath, FileName);

    public static BuildingMetadataIndex Load(string rootPath)
    {
        var path = IndexPath(rootPath);
        if (!File.Exists(path))
            return new BuildingMetadataIndex();
        try
        {
            using var stream = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return JsonSerializer.Deserialize<BuildingMetadataIndex>(stream) ?? new BuildingMetadataIndex();
        }
        catch (JsonException)
        {
            return new BuildingMetadataIndex(); // corrupt/partial -- treat as "nothing entered yet"
        }
        catch (IOException)
        {
            return new BuildingMetadataIndex();
        }
    }

    private static void Save(string rootPath, BuildingMetadataIndex index)
    {
        Directory.CreateDirectory(rootPath);
        var path = IndexPath(rootPath);
        var tempPath = path + ".tmp";
        File.WriteAllText(tempPath, JsonSerializer.Serialize(index, SerializerOptions));
        File.Move(tempPath, path, overwrite: true); // FacadeHierarchyStore와 동일한 atomic-rename 패턴
    }

    public static BuildingMetadataEntry? Get(string rootPath, string complexId, string buildingId)
    {
        var key = KeyFor(complexId, buildingId);
        return Load(rootPath).Buildings.FirstOrDefault(b => b.Key == key);
    }

    public static void Upsert(string rootPath, BuildingMetadataEntry entry)
    {
        entry.Key = KeyFor(entry.ComplexId, entry.BuildingId);
        var index = Load(rootPath);
        var existing = index.Buildings.FirstOrDefault(b => b.Key == entry.Key);
        if (existing != null)
            index.Buildings.Remove(existing);
        index.Buildings.Add(entry);
        Save(rootPath, index);
    }
}
