#!/usr/bin/env bash
# CheckCrack Viewer용 MySQL 최소 설치/설정 스크립트 (Ubuntu).
#
# 하는 일:
#   1. mysql-server 설치 여부 확인, 없으면 apt-get install
#   2. 서비스 시작 + 부팅 시 자동 시작 활성화
#   3. checkcrack 데이터베이스 생성
#   4. 전용 앱 계정 생성(루트 아님), checkcrack DB에만 권한 부여
#   5. users_table.sql 적용
#
# 사용법:
#   sudo APP_DB_PASSWORD='실제비밀번호' ./scripts/setup_mysql_ubuntu.sh
# APP_DB_PASSWORD를 안 주면 랜덤 비밀번호를 생성해서 한 번만 화면에 출력한다 --
# 그 값을 CheckCrack Viewer의 로그인 화면 "DB 연결 설정"(또는 설정 페이지)의
# User ID=checkcrack_app / Password로 그대로 입력하면 된다.

set -euo pipefail

DB_NAME="checkcrack"
APP_DB_USER="checkcrack_app"
APP_DB_PASSWORD="${APP_DB_PASSWORD:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USERS_SQL="${SCRIPT_DIR}/users_table.sql"

if [[ $EUID -ne 0 ]]; then
    echo "root 권한으로 실행해야 합니다 (sudo)." >&2
    exit 1
fi

if [[ -z "$APP_DB_PASSWORD" ]]; then
    APP_DB_PASSWORD="$(openssl rand -base64 24 | tr -d '=+/' | cut -c1-24)"
    GENERATED_PASSWORD=1
else
    GENERATED_PASSWORD=0
fi

echo "== 1. mysql-server 설치 확인 =="
if ! dpkg -l mysql-server >/dev/null 2>&1; then
    echo "mysql-server가 없어서 설치합니다..."
    apt-get update
    apt-get install -y mysql-server
else
    echo "mysql-server 이미 설치됨."
fi

echo "== 2. 서비스 시작 + 자동 시작 등록 =="
systemctl enable mysql
systemctl start mysql

echo "== 3~4. checkcrack DB + 전용 앱 계정 생성 =="
mysql <<SQL
CREATE DATABASE IF NOT EXISTS ${DB_NAME} CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS '${APP_DB_USER}'@'%' IDENTIFIED BY '${APP_DB_PASSWORD}';
ALTER USER '${APP_DB_USER}'@'%' IDENTIFIED BY '${APP_DB_PASSWORD}';
GRANT SELECT, INSERT, UPDATE, DELETE ON ${DB_NAME}.* TO '${APP_DB_USER}'@'%';
FLUSH PRIVILEGES;
SQL

echo "== 5. users 테이블 적용 =="
if [[ -f "$USERS_SQL" ]]; then
    mysql "${DB_NAME}" < "$USERS_SQL"
else
    echo "경고: ${USERS_SQL} 을 찾을 수 없어 users 테이블 생성을 건너뜁니다." >&2
fi

echo ""
echo "== 완료 =="
echo "Database : ${DB_NAME}"
echo "App User : ${APP_DB_USER}"
if [[ "$GENERATED_PASSWORD" -eq 1 ]]; then
    echo "Password : ${APP_DB_PASSWORD}   (자동 생성됨 -- 지금 반드시 안전한 곳에 기록해두세요)"
else
    echo "Password : (직접 지정한 값 사용)"
fi
echo ""
echo "원격 접속을 허용하려면 /etc/mysql/mysql.conf.d/mysqld.cnf의 bind-address를"
echo "0.0.0.0(또는 필요한 대역)으로 바꾸고 방화벽에서 3306 포트를 열어야 합니다."
echo "(이 스크립트는 그 부분까지는 자동으로 바꾸지 않습니다 -- 네트워크 노출 범위는"
echo "운영 환경에 맞게 직접 판단해서 설정하세요.)"
echo ""
echo "관리자 계정은 아직 없습니다 -- scripts/users_table.sql 상단 안내대로 BCrypt"
echo "해시를 만들어서 users 테이블에 직접 INSERT 하세요."
