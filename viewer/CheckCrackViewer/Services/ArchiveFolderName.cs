namespace CheckCrackViewer.Services;

/// <summary>내규 (2026-10-08, 사용자 지시): 프로그램이 만드는 작업 폴더 이름은 영문/숫자만 쓴다.
/// 받은 archive를 푸는 폴더는 회사·동 이름(한글일 수 있음, 예: "수목토_1100_1") 대신 "A{archive_id}"로 만든다 --
/// Windows의 OpenCV(cv2.imread/imwrite)는 한글 경로를 오류 없이 못 읽어 처리가 통째로 건너뛰어진 실제 사고가 있었다
/// (2026-10-07 원격 분석, 벽 질감 보정 0 px). 회사·동 이름은 화면/DB 표시용 정보로만 쓴다(RegisterExtractedArchive 인자).
/// Python 쪽은 별도로 모든 이미지 입출력을 imread_unicode/imwrite_unicode로 강제한다(tools/check_unicode_io.py).</summary>
public static class ArchiveFolderName
{
    public static string For(long archiveId) => $"A{archiveId}";

    /// <summary>예전 이름("{회사}_{동}_{id}")과 새 이름("A{id}") 모두 이 archive의 폴더로 인정 -- 계약 종료 정리용.</summary>
    public static bool Matches(string folderName, long archiveId) =>
        folderName == For(archiveId) || folderName.EndsWith($"_{archiveId}", StringComparison.Ordinal);
}
