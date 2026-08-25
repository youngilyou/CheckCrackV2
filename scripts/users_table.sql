-- CheckCrack Viewer 로그인용 users 테이블.
-- 마이그레이션 도구 없이 수동 적용 (setup_mysql_ubuntu.sh가 최초 1회 자동 적용도 해줌).
--
-- 비밀번호는 절대 평문으로 넣지 않는다 -- password_hash는 BCrypt 해시만 저장한다.
-- 관리자 계정 해시 생성 예시 (PowerShell, .NET 있는 아무 머신에서):
--   dotnet script -e 'Console.WriteLine(BCrypt.Net.BCrypt.HashPassword("실제비밀번호"));'
-- 또는 CheckCrack Viewer 프로젝트 안에서 BCrypt.Net-Next NuGet이 이미 참조돼 있으므로
-- 짧은 콘솔 스니펫으로 생성해서 아래 INSERT의 플레이스홀더에 붙여넣는다.

CREATE TABLE IF NOT EXISTS users (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    username        VARCHAR(64)  NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    display_name    VARCHAR(128) NULL,
    role            VARCHAR(32)  NULL,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at   DATETIME     NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 최초 관리자 계정 예시 -- <BCRYPT_HASH_HERE>를 위 방법으로 만든 실제 해시로 바꿔서 실행.
-- INSERT INTO users (username, password_hash, display_name, role)
-- VALUES ('admin', '<BCRYPT_HASH_HERE>', '관리자', 'admin');
