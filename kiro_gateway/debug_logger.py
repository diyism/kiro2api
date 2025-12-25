# -*- coding: utf-8 -*-

# Kiro OpenAI Gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Модуль для отладочного логирования запросов.

Поддерживает три режима (DEBUG_MODE):
- off: логирование отключено
- errors: логи сохраняются только при ошибках (4xx, 5xx)
- all: логи перезаписываются на каждый запрос

В режиме "errors" данные буферизуются в памяти и сбрасываются в файлы
только при вызове flush_on_error().

Также захватывает логи приложения (loguru) для каждого запроса и сохраняет
их в файл app_logs.txt для удобства отладки.
"""

import io
import json
import shutil
from pathlib import Path
from typing import Optional
from loguru import logger

from kiro_gateway.config import DEBUG_MODE, DEBUG_DIR


class DebugLogger:
    """
    Синглтон для управления отладочными логами запросов.
    
    Режимы работы:
    - off: ничего не делает
    - errors: буферизует данные, сбрасывает в файлы только при ошибках
    - all: пишет данные сразу в файлы (как раньше)
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DebugLogger, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.debug_dir = Path(DEBUG_DIR)
        self._initialized = True

        # Буферы для режима "errors"
        self._request_body_buffer: Optional[bytes] = None
        self._kiro_request_body_buffer: Optional[bytes] = None
        self._raw_chunks_buffer: bytearray = bytearray()
        self._modified_chunks_buffer: bytearray = bytearray()

        # Буфер для логов приложения (loguru)
        self._app_logs_buffer: io.StringIO = io.StringIO()
        self._loguru_sink_id: Optional[int] = None

        # Временная метка для текущего запроса
        self._request_timestamp: str = ""
    
    def _is_enabled(self) -> bool:
        """Проверяет, включено ли логирование."""
        return DEBUG_MODE in ("errors", "all")
    
    def _is_immediate_write(self) -> bool:
        """Проверяет, нужно ли писать сразу в файлы (режим all)."""
        return DEBUG_MODE == "all"
    
    def _clear_buffers(self):
        """Очищает все буферы."""
        self._request_body_buffer = None
        self._kiro_request_body_buffer = None
        self._raw_chunks_buffer.clear()
        self._modified_chunks_buffer.clear()
        self._clear_app_logs_buffer()
    
    def _clear_app_logs_buffer(self):
        """Очищает буфер логов приложения и удаляет sink."""
        # Удаляем sink из loguru
        if self._loguru_sink_id is not None:
            try:
                logger.remove(self._loguru_sink_id)
            except ValueError:
                # Sink уже удалён
                pass
            self._loguru_sink_id = None
        
        # Очищаем буфер
        self._app_logs_buffer = io.StringIO()
    
    def _setup_app_logs_capture(self):
        """
        Настраивает захват логов приложения в буфер.
        
        Добавляет временный sink в loguru, который пишет в StringIO буфер.
        Захватывает ВСЕ логи без фильтрации, так как sink активен только
        на время обработки конкретного запроса.
        """
        # Удаляем предыдущий sink если есть
        self._clear_app_logs_buffer()
        
        # Добавляем новый sink для захвата ВСЕХ логов
        # Формат: время | уровень | модуль:функция:строка | сообщение
        self._loguru_sink_id = logger.add(
            self._app_logs_buffer,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
            level="DEBUG",  # Захватываем все уровни от DEBUG и выше
            colorize=False,  # Без ANSI цветов в файле
            # Без фильтра - захватываем ВСЕ логи во время обработки запроса
        )

    def prepare_new_request(self):
        """
        Подготавливает логгер для нового запроса.

        В режиме "all": НЕ очищает папку (сохраняет все запросы с временными метками).
        В режиме "errors": очищает буферы.
        В обоих режимах: настраивает захват логов приложения.
        """
        if not self._is_enabled():
            return

        # Генерируем временную метку для этого запроса
        from datetime import datetime
        self._request_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        # Очищаем буферы в любом случае
        self._clear_buffers()

        # Настраиваем захват логов приложения
        self._setup_app_logs_capture()

        if self._is_immediate_write():
            # Режим "all" - НЕ очищаем папку, просто создаём если не существует
            try:
                self.debug_dir.mkdir(parents=True, exist_ok=True)
                logger.debug(f"[DebugLogger] Directory {self.debug_dir} ready for new request (timestamp={self._request_timestamp}).")
            except Exception as e:
                logger.error(f"[DebugLogger] Error preparing directory: {e}")

    def log_request_body(self, body: bytes):
        """
        Сохраняет тело запроса (от клиента, OpenAI формат).
        
        В режиме "all": пишет сразу в файл.
        В режиме "errors": буферизует.
        """
        if not self._is_enabled():
            return

        if self._is_immediate_write():
            self._write_request_body_to_file(body)
        else:
            # Режим "errors" - буферизуем
            self._request_body_buffer = body

    def log_kiro_request_body(self, body: bytes):
        """
        Сохраняет модифицированное тело запроса (к Kiro API).
        
        В режиме "all": пишет сразу в файл.
        В режиме "errors": буферизует.
        """
        if not self._is_enabled():
            return

        if self._is_immediate_write():
            self._write_kiro_request_body_to_file(body)
        else:
            # Режим "errors" - буферизуем
            self._kiro_request_body_buffer = body

    def log_raw_chunk(self, chunk: bytes):
        """
        Дописывает сырой чанк ответа (от провайдера).
        
        В режиме "all": пишет сразу в файл.
        В режиме "errors": буферизует.
        """
        if not self._is_enabled():
            return

        if self._is_immediate_write():
            self._append_raw_chunk_to_file(chunk)
        else:
            # Режим "errors" - буферизуем
            self._raw_chunks_buffer.extend(chunk)

    def log_modified_chunk(self, chunk: bytes):
        """
        Дописывает модифицированный чанк (клиенту).
        
        В режиме "all": пишет сразу в файл.
        В режиме "errors": буферизует.
        """
        if not self._is_enabled():
            return

        if self._is_immediate_write():
            self._append_modified_chunk_to_file(chunk)
        else:
            # Режим "errors" - буферизуем
            self._modified_chunks_buffer.extend(chunk)
    
    def log_error_info(self, status_code: int, error_message: str = ""):
        """
        Записывает информацию об ошибке в файл.
        
        Работает в обоих режимах (errors и all).
        В режиме "all" записывает сразу в файл.
        В режиме "errors" вызывается из flush_on_error().
        
        Args:
            status_code: HTTP статус код ошибки
            error_message: Сообщение об ошибке (опционально)
        """
        if not self._is_enabled():
            return
        
        try:
            # Убеждаемся что директория существует
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            
            error_info = {
                "status_code": status_code,
                "error_message": error_message
            }
            error_file = self.debug_dir / "error_info.json"
            with open(error_file, "w", encoding="utf-8") as f:
                json.dump(error_info, f, indent=2, ensure_ascii=False)
            
            logger.debug(f"[DebugLogger] Error info saved (status={status_code})")
        except Exception as e:
            logger.error(f"[DebugLogger] Error writing error_info: {e}")

    def flush_on_error(self, status_code: int, error_message: str = ""):
        """
        Сбрасывает буферы в файлы при ошибке.
        
        В режиме "errors": сбрасывает буферы и сохраняет error_info.
        В режиме "all": только сохраняет error_info (данные уже записаны).
        
        Args:
            status_code: HTTP статус код ошибки
            error_message: Сообщение об ошибке (опционально)
        """
        if not self._is_enabled():
            return
        
        # В режиме "all" данные уже записаны, добавляем error_info и логи приложения
        if self._is_immediate_write():
            self.log_error_info(status_code, error_message)
            self._write_app_logs_to_file()
            self._clear_app_logs_buffer()
            return
        
        # Проверяем, есть ли что сбрасывать
        if not any([
            self._request_body_buffer,
            self._kiro_request_body_buffer,
            self._raw_chunks_buffer,
            self._modified_chunks_buffer
        ]):
            return
        
        try:
            # Создаём директорию если не существует
            if self.debug_dir.exists():
                shutil.rmtree(self.debug_dir)
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            
            # Сбрасываем буферы в файлы
            if self._request_body_buffer:
                self._write_request_body_to_file(self._request_body_buffer)
            
            if self._kiro_request_body_buffer:
                self._write_kiro_request_body_to_file(self._kiro_request_body_buffer)
            
            if self._raw_chunks_buffer:
                file_path = self.debug_dir / "response_stream_raw.txt"
                with open(file_path, "wb") as f:
                    f.write(self._raw_chunks_buffer)
            
            if self._modified_chunks_buffer:
                file_path = self.debug_dir / "response_stream_modified.txt"
                with open(file_path, "wb") as f:
                    f.write(self._modified_chunks_buffer)
            
            # Сохраняем информацию об ошибке
            self.log_error_info(status_code, error_message)
            
            # Сохраняем логи приложения
            self._write_app_logs_to_file()
            
            logger.info(f"[DebugLogger] Error logs flushed to {self.debug_dir} (status={status_code})")
            
        except Exception as e:
            logger.error(f"[DebugLogger] Error flushing buffers: {e}")
        finally:
            # Очищаем буферы после сброса
            self._clear_buffers()
    
    def discard_buffers(self):
        """
        Очищает буферы без записи в файлы.
        
        Вызывается когда запрос завершился успешно в режиме "errors".
        Также вызывается в режиме "all" для сохранения логов успешного запроса.
        """
        if DEBUG_MODE == "errors":
            self._clear_buffers()
        elif DEBUG_MODE == "all":
            # В режиме "all" сохраняем логи даже для успешных запросов
            self._write_app_logs_to_file()
            self._clear_app_logs_buffer()
    
    # ==================== Приватные методы записи в файлы ====================
    
    def _write_request_body_to_file(self, body: bytes):
        """Записывает тело запроса в файл."""
        try:
            file_path = self.debug_dir / f"{self._request_timestamp}_request_body.json"
            try:
                json_obj = json.loads(body)
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(json_obj, f, indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                with open(file_path, "wb") as f:
                    f.write(body)
        except Exception as e:
            logger.error(f"[DebugLogger] Error writing request_body: {e}")

    def _write_kiro_request_body_to_file(self, body: bytes):
        """Записывает тело запроса к Kiro в файл."""
        try:
            file_path = self.debug_dir / f"{self._request_timestamp}_kiro_request_body.json"
            try:
                json_obj = json.loads(body)
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(json_obj, f, indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                with open(file_path, "wb") as f:
                    f.write(body)
        except Exception as e:
            logger.error(f"[DebugLogger] Error writing kiro_request_body: {e}")

    def _append_raw_chunk_to_file(self, chunk: bytes):
        """Дописывает сырой чанк в файл."""
        try:
            file_path = self.debug_dir / f"{self._request_timestamp}_response_stream_raw.txt"
            with open(file_path, "ab") as f:
                f.write(chunk)
        except Exception:
            pass
    
    def _append_modified_chunk_to_file(self, chunk: bytes):
        """Дописывает модифицированный чанк в файл."""
        try:
            file_path = self.debug_dir / f"{self._request_timestamp}_response_stream_modified.txt"
            with open(file_path, "ab") as f:
                f.write(chunk)
        except Exception:
            pass

    def _write_app_logs_to_file(self):
        """Записывает захваченные логи приложения в файл."""
        try:
            # Получаем содержимое буфера
            logs_content = self._app_logs_buffer.getvalue()

            if not logs_content.strip():
                return

            # Убеждаемся что директория существует
            self.debug_dir.mkdir(parents=True, exist_ok=True)

            file_path = self.debug_dir / f"{self._request_timestamp}_app_logs.txt"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(logs_content)

            logger.debug(f"[DebugLogger] App logs saved to {file_path}")
        except Exception as e:
            # Не логируем ошибку через logger чтобы избежать рекурсии
            pass


# Глобальный экземпляр
debug_logger = DebugLogger()