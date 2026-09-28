"""Structured JSON logging (CLAUDE.local.md #35)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # 2026-09-28 (뷰어 LIVE LOG에 시간 표시 요청, 사용자): 기존엔 로그 줄에 시간이 전혀 없어서
        # logs/pipeline.log를 grep할 때도 줄 번호로만 위치를 짚어야 했음 -- record.created(로그
        # 기록 시점, 초 단위 float epoch)를 로컬 시간 ISO 문자열로 남김. 밀리초까지 포함해 같은 초에
        # 여러 줄이 찍혀도 순서 구분 가능.
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(sep=" ", timespec="milliseconds"),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str, log_dir: str | Path | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(JsonFormatter())
    logger.addHandler(stream_handler)

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, level: str, message: str, **fields: Any) -> None:
    getattr(logger, level.lower())(message, extra={"fields": fields})
