"""Tiện ích dùng chung: embed, logging và đọc/ghi JSON an toàn."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import sys
import tempfile
import time
from typing import Any, Optional

import discord

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EMBED_COLOR = 0xE60012
ERROR_COLOR = 0xFFB000
FOOTER_TEXT = "Joker • Phantom Thieves of Hearts"

_DEFAULT_GIFS = [
    "https://i.pinimg.com/originals/1e/12/1c/1e121c1f64dcb8c549a7fe13264cfe1d.gif",
    "https://i.pinimg.com/originals/08/a4/a8/08a4a843a478531cb22606971da3c9bb.gif",
]


# ==========================================
# EMBED
# ==========================================

def _gif_pool() -> list[str]:
    # Đặt JOKER_GIFS="url1,url2" trong .env để dùng GIF tự host thay cho link pinimg.
    custom = [u.strip() for u in os.getenv("JOKER_GIFS", "").split(",") if u.strip()]
    return custom or _DEFAULT_GIFS


def footer_text(guild: Optional[discord.Guild] = None) -> str:
    return f"Joker • {guild.name}" if guild else FOOTER_TEXT


def make_embed(
    description: str,
    *,
    title: Optional[str] = None,
    color: int = EMBED_COLOR,
    gif: bool = False,
    guild: Optional[discord.Guild] = None,
) -> discord.Embed:
    embed = discord.Embed(description=description, color=color)
    if title:
        embed.title = title
    embed.set_footer(text=footer_text(guild))
    if gif:
        embed.set_image(url=random.choice(_gif_pool()))
    return embed


def err_embed(text: str, guild: Optional[discord.Guild] = None) -> discord.Embed:
    return make_embed(text, color=ERROR_COLOR, guild=guild)


# ==========================================
# LOGGING
# ==========================================

_LOGGER_NAME = "joker"
_logger = logging.getLogger(_LOGGER_NAME)

OK = 25  # nằm giữa INFO (20) và WARNING (30)
logging.addLevelName(OK, "OK")

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "ok": OK,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
_TAGS = {
    logging.DEBUG: "DBG ",
    logging.INFO: "INFO",
    OK: "  OK",
    logging.WARNING: "WARN",
    logging.ERROR: " ERR",
    logging.CRITICAL: "CRIT",
}
_RESET, _DIM = "\033[0m", "\033[2m"
_COLORS = {
    logging.DEBUG: _DIM,
    logging.INFO: "\033[96m",
    OK: "\033[92m",
    logging.WARNING: "\033[93m",
    logging.ERROR: "\033[91m",
    logging.CRITICAL: "\033[91m",
}


class _Formatter(logging.Formatter):
    def __init__(self, color: bool):
        super().__init__("%(dim)s%(asctime)s%(reset)s %(tag)s %(prefix)s%(message)s", "%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        tag = _TAGS.get(record.levelno, record.levelname[:4])
        if self.color:
            record.dim, record.reset = _DIM, _RESET
            tag = f"{_COLORS.get(record.levelno, '')}[{tag}]{_RESET}"
        else:
            record.dim = record.reset = ""
            tag = f"[{tag}]"
        record.tag = tag
        record.prefix = "" if record.name == _LOGGER_NAME else f"{record.name}: "
        return super().format(record)


def setup_logging() -> None:
    """Gom log của Joker và discord.py vào cùng một handler. Gọi một lần ở main().

    Biến môi trường: DEBUG=1 để bật log debug, NO_COLOR=1 để tắt mã màu ANSI.
    Màu cũng tự tắt khi stdout không phải terminal (nohup, ghi ra file...).
    """
    debug = os.getenv("DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    use_color = sys.stdout.isatty() and "NO_COLOR" not in os.environ

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter(use_color))

    root = logging.getLogger()
    root.handlers[:] = [handler]  # gọi lại nhiều lần cũng không bị nhân đôi log
    root.setLevel(logging.INFO)
    _logger.setLevel(logging.DEBUG if debug else logging.INFO)


def log(msg: str, level: str = "info") -> None:
    _logger.log(_LEVELS.get(level, logging.INFO), msg)


def log_exc(msg: str, exc: BaseException) -> None:
    _logger.error(msg, exc_info=exc)


# ==========================================
# JSON
# ==========================================

def read_json(path: str, default: Any = None, *, label: Optional[str] = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:  # JSONDecodeError và UnicodeDecodeError đều là ValueError
        log(f"Không đọc được {label or os.path.basename(path)}, dùng dữ liệu mặc định: {e}", "warn")
        if isinstance(e, ValueError):
            with contextlib.suppress(OSError):
                os.replace(path, f"{path}.{int(time.time())}.corrupt")
        return default


def _fsync_dir(directory: str) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json_atomic(path: str, data: Any, *, mode: int = 0o600, **dump_kwargs) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # File tạm riêng cho mỗi lần ghi, nên hai lần ghi đồng thời vào cùng một path không đè nhau.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    _fsync_dir(directory)


async def aread_json(path: str, default: Any = None, *, label: Optional[str] = None) -> Any:
    """Bản không chặn event loop của read_json."""
    return await asyncio.to_thread(read_json, path, default, label=label)


async def awrite_json_atomic(path: str, data: Any, *, mode: int = 0o600, **dump_kwargs) -> None:
    """Bản không chặn event loop của write_json_atomic."""
    await asyncio.to_thread(write_json_atomic, path, data, mode=mode, **dump_kwargs)
