from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core.common import (
    EMBED_COLOR, ERROR_COLOR, make_embed, log,
    ROOT_DIR, read_json, write_json_atomic,
)
from cogs.badge_cog import get_build_number, PROXIES


SCIENCE_URL = "https://discord.com/api/v9/science"
ME_URL = "https://discord.com/api/v9/users/@me?with_analytics_token=true"
GAMES_CDN_URL = "https://cdn.discordapp.com/detectables/games.json"

BATCH_SIZE = 50
BATCH_DELAY = 1.0
GAMES_TTL = 24 * 3600

COOLDOWN_SECONDS = 3600
MAX_CONCURRENT = 3
MAX_GAMES = 200

USAGE_FILE = os.path.join(ROOT_DIR, "data", "badge_usage.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) discord/1.0.9253 Chrome/148.0.7778.280 "
    "Electron/42.7.1 Safari/537.36"
)

# Dữ liệu mốc (Đã bỏ price)
TIME_MILESTONES = {
    "1":  {"name": "Casual",      "hours": 1},
    "2":  {"name": "Recreational","hours": 5},
    "3":  {"name": "Dedicated",   "hours": 20},
    "4":  {"name": "Committed",   "hours": 75},
    "5":  {"name": "Serious",     "hours": 150},
    "6":  {"name": "Devoted",     "hours": 300},
    "7":  {"name": "Seasoned",    "hours": 500},
    "8":  {"name": "Ironclad",    "hours": 1000},
    "9":  {"name": "Unshakeable", "hours": 2000},
    "10": {"name": "Eternal",     "hours": 5000},
}

GAME_MILESTONES = {
    "1":  {"name": "Sampler",      "games": 2},
    "2":  {"name": "Dabbler",      "games": 5},
    "3":  {"name": "Enthusiast",   "games": 10},
    "4":  {"name": "Ranger",       "games": 15},
    "5":  {"name": "Explorer",     "games": 20},
    "6":  {"name": "Adventurer",   "games": 30},
    "7":  {"name": "Voyager",      "games": 40},
    "8":  {"name": "Maverick",     "games": 60},
    "9":  {"name": "Polymath",     "games": 80},
    "10": {"name": "Universalist", "games": 100},
}

# Biến toàn cục quản lý state
active_tasks: dict[int, asyncio.Task] = {}
_running_lock = asyncio.Lock()
_running_count = 0
_last_used: dict[int, float] = {}
_games_cache: dict = {"data": None, "at": 0.0}


def _save_usage():
    try:
        write_json_atomic(USAGE_FILE, {str(k): v for k, v in _last_used.items()}, indent=2)
    except Exception:
        pass


def _cleanup_usage():
    """Dọn dẹp những user không dùng quá 7 ngày để file JSON không phình to"""
    now = time.time()
    to_delete = [uid for uid, ts in _last_used.items() if now - ts > 7 * 86400]
    if to_delete:
        for uid in to_delete:
            del _last_used[uid]
        _save_usage()


# Khởi động: load file và dọn dẹp
_raw_usage = read_json(USAGE_FILE, {})
if isinstance(_raw_usage, dict):
    _last_used = {int(k): float(v) for k, v in _raw_usage.items()}
_cleanup_usage()


# ---------- Validation ----------

def _is_likely_token(t: str) -> bool:
    """Discord token thường có 2 dấu chấm, độ dài từ 50-200 ký tự."""
    return isinstance(t, str) and t.count(".") == 2 and 50 < len(t) < 200


def _is_likely_fingerprint(t: str) -> bool:
    if not t:
        return True  # optional
    t = t.strip()
    if not (100 <= len(t) <= 300):
        return False
    return bool(re.match(r"^[A-Za-z0-9+/=]+$", t))


# ---------- Games ----------

@dataclass(frozen=True)
class SpooferGame:
    id: str
    name: str
    exe: str


def _win_exe(game: dict) -> str:
    for e in game.get("executables", []):
        if e.get("os") == "win32" and e.get("name"):
            return e["name"]
    return "game.exe"


def _parse_games(raw: str) -> list[SpooferGame]:
    data = json.loads(raw)
    games, seen = [], set()
    for entry in data:
        gid = str(entry.get("id", ""))
        if not gid.isdigit() or gid in seen:
            continue
        if not any(e.get("os") == "win32" and e.get("name") for e in entry.get("executables", [])):
            continue
        seen.add(gid)
        games.append(SpooferGame(gid, entry.get("name", "Unknown"), _win_exe(entry)))
    return games


async def _load_games(proxy: Optional[str] = None) -> list[SpooferGame]:
    now = time.time()
    if _games_cache["data"] is not None and now - _games_cache["at"] < GAMES_TTL:
        return _games_cache["data"]

    cache_path = os.path.join(ROOT_DIR, "data", "games.json")
    raw = ""
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError:
            raw = ""

    if not raw:
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(GAMES_CDN_URL, proxy=proxy) as r:
                    if r.status == 200:
                        raw = await r.text()
                        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                        with open(cache_path, "w", encoding="utf-8") as f:
                            f.write(raw)
        except Exception as e:
            log(f"[Badge] Tải games.json lỗi: {type(e).__name__}", "warn")

    if not raw:
        return []
    try:
        games = _parse_games(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    _games_cache.update(data=games, at=now)
    return games


# ---------- Science Client ----------

def _build_super_props(build_number: int, launch_signature: str, heartbeat_session: str) -> str:
    obj = {
        "os": "Windows", "browser": "Discord Client", "release_channel": "stable",
        "client_version": "1.0.9253", "os_version": "10.0.26200", "os_arch": "x64", "app_arch": "x64",
        "system_locale": "vi-VN",
        "browser_user_agent": USER_AGENT, "browser_version": "42.7.1",
        "client_build_number": build_number, "native_build_number": 88414,
        "client_event_source": None,
        "client_launch_id": str(uuid.uuid4()),
        "launch_signature": launch_signature,
        "client_heartbeat_session_id": heartbeat_session,
    }
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


async def _fetch_analytics_token(token: str, super_props: str, proxy: Optional[str] = None) -> str:
    headers = {"authorization": token, "user-agent": USER_AGENT, "x-super-properties": super_props}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
        async with s.get(ME_URL, proxy=proxy) as r:
            if r.status == 401:
                raise RuntimeError("Token bị từ chối (401) — token sai hoặc đã hết hạn")
            if r.status == 403:
                raise RuntimeError("Bị Cloudflare chặn (403) — thử lại hoặc dùng proxy")
            if r.status == 429:
                raise RuntimeError("Discord rate limit (429) — đợi vài phút")
            if r.status != 200:
                raise RuntimeError(f"Không lấy được analytics token (mã {r.status})")
            data = await r.json(content_type=None)
            tok = data.get("analytics_token")
            if not tok:
                raise RuntimeError("Discord không trả analytics_token — token có thể đã bị vô hiệu")
            return tok


class ScienceSpoofer:
    def __init__(self, token: str, analytics_token: str, super_props: str,
                 heartbeat_session: str, launch_signature: str,
                 fingerprint: str = "", proxy: Optional[str] = None):
        self.token = token
        self.analytics_token = analytics_token
        self.super_props = super_props
        self.fingerprint = fingerprint
        self.proxy = proxy
        self._heartbeat = heartbeat_session
        self._signature = launch_signature
        self._seq = 0
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        headers = {
            "accept": "*/*",
            "accept-language": "vi-VN,vi;q=0.9,en;q=0.8",
            "authorization": self.token,
            "content-type": "application/json",
            "origin": "https://discord.com",
            "referer": "https://discord.com/channels/@me",
            "user-agent": USER_AGENT,
            "x-debug-options": "bugReporterEnabled",
            "x-discord-locale": "vi-VN",
            "x-discord-timezone": "Asia/Ho_Chi_Minh",
            "x-super-properties": self.super_props,
        }
        self._session = aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=20))
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _build_launch(self, game: SpooferGame) -> dict:
        now = int(time.time() * 1000)
        props = {
            "client_track_timestamp": now,
            "client_heartbeat_session_id": self._heartbeat,
            "event_sequence_number": self._next_seq(),
            "game": game.name, "game_id": game.id,
            "verified": True, "elevated": False, "is_launcher": False,
            "game_platform": "desktop", "detection_method": "verified_game",
            "is_overlay_enabled": False, "is_overlay_game_enabled": True,
            "is_overlay_game_source": "OOP_DEFAULT_DATABASE",
            "fullscreen_type": "UNKNOWN", "hardware_display_count": 1,
            "overlay_method": "Disabled", "activity_status_enabled": True,
            "activity_status_shared_guilds": [], "current_user_status": "online",
            "game_detection_enabled": True, "executable_path": game.exe,
            "voice_channel_id": None, "voice_channel_type": None,
            "voice_channel_bitrate": None, "voice_channel_guild_id": None,
            "hidden_by_distributor": False, "game_metadata": None,
            "client_performance_cpu": None, "client_performance_memory": None,
            "cpu_core_count": None, "accessibility_features": 0,
            "rendered_locale": "vi-VN",
            "launch_signature": self._signature,
            "client_rtc_state": None, "client_app_state": "focused",
            "client_send_timestamp": now,
        }
        if self.fingerprint:
            props["executable_fingerprint"] = self.fingerprint
        return {"type": "launch_game", "properties": props}

    def _build_heartbeat(self, game: SpooferGame, duration_ms: int, session_id: str,
                         initial: bool, final: bool, ts: int) -> dict:
        return {"type": "running_game_heartbeat", "properties": {
            "client_track_timestamp": ts,
            "client_heartbeat_session_id": self._heartbeat,
            "event_sequence_number": self._next_seq(),
            "game_id": game.id, "game_name": game.name,
            "game_metadata": None, "game_executable": game.exe,
            "game_detection_enabled": True,
            "initial_heartbeat": initial, "final_heartbeat": final,
            "game_session_id": session_id, "duration_tracked_ms": duration_ms,
            "rtc_connection_id": None, "media_session_id": None,
            "launch_signature": self._signature,
            "client_app_state": "focused", "client_send_timestamp": ts,
        }}

    def build_events(self, game: SpooferGame, duration_ms: int) -> list[dict]:
        sid = str(uuid.uuid4())
        now = int(time.time() * 1000)
        start = max(0, now - duration_ms)
        return [
            self._build_heartbeat(game, 0, sid, initial=True, final=False, ts=start),
            self._build_launch(game),
            self._build_heartbeat(game, duration_ms, sid, initial=False, final=True, ts=now),
        ]

    async def post(self, events: list[dict]) -> int:
        payload = {"token": self.analytics_token, "events": events}
        try:
            async with self._session.post(SCIENCE_URL, json=payload, proxy=self.proxy) as r:
                if r.status == 204:
                    return 204
                if r.status == 429:
                    try:
                        body = await r.json(content_type=None)
                        wait = float(body.get("retry_after", 5))
                    except Exception:
                        wait = 5.0
                    await asyncio.sleep(wait + 1.0)
                return r.status
        except Exception as e:
            log(f"[Badge] POST /science lỗi: {type(e).__name__}", "warn")
            return 0


# ---------- Progress ----------

class BadgeProgress:
    def __init__(self, interaction: discord.Interaction, hours_per_game: float, total: int):
        self.interaction = interaction
        self.hours = hours_per_game
        self.total = total
        self.done = 0
        self.started_at = time.time()
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()
        self.phase = "starting"
        self.error: Optional[str] = None

    def set(self, phase: str, error: Optional[str] = None):
        self.phase, self.error = phase, error

    def inc(self, n: int):
        self.done += n

    def _embed(self) -> discord.Embed:
        pct = int(self.done / self.total * 100) if self.total else 0
        filled = max(0, min(12, round(12 * pct / 100)))
        bar = "▰" * filled + "▱" * (12 - filled)
        
        if self.phase == "starting":
            desc = "Đang xác thực token và kết nối Discord..."
        elif self.phase == "running":
            total_claimed = self.done * self.hours
            desc = f"{bar} **{pct}%**\nĐã gửi: **{self.done}/{self.total}** game · **{total_claimed:,.0f}h**"
        elif self.phase == "done":
            total_claimed = self.done * self.hours
            desc = (f"✅ **Hoàn tất** {self.done}/{self.total} game\n"
                    f"Đã buff **{total_claimed:,.0f} giờ** trong {int(time.time() - self.started_at)}s\n\n"
                    f"Badge sẽ cập nhật sau **1-2 ngày**.")
        elif self.phase == "error":
            desc = f"⚠️ **Dừng giữa chừng**: {self.error}\nĐã gửi: {self.done}/{self.total}"
        else:
            desc = "..."

        kwargs = {"color": ERROR_COLOR} if self.phase == "error" else {}
        return make_embed(desc, title="Buff Huy Hiệu Discord", guild=self.interaction.guild, **kwargs)

    async def start(self):
        try:
            self.message = await self.interaction.followup.send(
                embed=self._embed(), ephemeral=True, wait=True,
            )
        except discord.HTTPException as e:
            log(f"[Badge] Không gửi được progress: {e}", "warn")

    async def update(self):
        if self.message is None:
            return
        async with self._lock:
            try:
                await self.message.edit(embed=self._embed())
            except discord.HTTPException:
                pass


# ---------- Modal ----------

class BadgeModal(discord.ui.Modal, title="Xác nhận Buff"):
    token_input = discord.ui.TextInput(
        label="Discord Token (bắt buộc)",
        placeholder="Token bot không lưu lại...",
        style=discord.TextStyle.short,
        required=True,
        min_length=50,
        max_length=200,
    )
    fingerprint_input = discord.ui.TextInput(
        label="Fingerprint (tùy chọn)",
        placeholder="Bỏ trống nếu không có.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, cog: "BadgeSpooferCog", time_key: str, game_key: str):
        super().__init__()
        self.cog = cog
        self.time_key = time_key
        self.game_key = game_key

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.begin(
            interaction,
            token=self.token_input.value.strip(),
            fingerprint=self.fingerprint_input.value.strip(),
            time_key=self.time_key,
            game_key=self.game_key,
        )


# ---------- Milestone View ----------

class MilestoneView(discord.ui.View):
    def __init__(self, cog: "BadgeSpooferCog"):
        super().__init__(timeout=300)
        self.cog = cog
        self.selected_time = None
        self.selected_games = None
        
        # Menu chọn Game Time
        time_opts = [
            discord.SelectOption(
                label=f"Mốc {k}: {v['name']} ({v['hours']} giờ)",
                value=k, emoji="🗡️"
            ) for k, v in TIME_MILESTONES.items()
        ]
        self.time_select = discord.ui.Select(placeholder="Chọn mốc Game Time...", options=time_opts, custom_id="time_select")
        self.time_select.callback = self.on_time_select
        self.add_item(self.time_select)
        
        # Menu chọn Game Variety
        game_opts = [
            discord.SelectOption(
                label=f"Mốc {k}: {v['name']} ({v['games']} game)",
                value=k, emoji="🎮"
            ) for k, v in GAME_MILESTONES.items()
        ]
        self.game_select = discord.ui.Select(placeholder="Chọn mốc Game Variety...", options=game_opts, custom_id="game_select")
        self.game_select.callback = self.on_game_select
        self.add_item(self.game_select)
        
        # Nút bắt đầu
        self.start_btn = discord.ui.Button(label="Bắt đầu", style=discord.ButtonStyle.green, row=2, disabled=True)
        self.start_btn.callback = self.on_start
        self.add_item(self.start_btn)

    def _build_embed(self, interaction: discord.Interaction) -> discord.Embed:
        desc = "Chọn mốc **Game Time** và **Game Variety** ở bên dưới để bắt đầu.\n\n"
        
        if self.selected_time:
            t = TIME_MILESTONES[self.selected_time]
            desc += f"🗡️ **Game Time:** Mốc {self.selected_time} - {t['name']} ({t['hours']} giờ)\n"
        else:
            desc += "🗡️ **Game Time:** Chưa chọn\n"
            
        if self.selected_games:
            g = GAME_MILESTONES[self.selected_games]
            desc += f"🎮 **Game Variety:** Mốc {self.selected_games} - {g['name']} ({g['games']} game)\n"
        else:
            desc += "🎮 **Game Variety:** Chưa chọn\n"
            
        if self.selected_time and self.selected_games:
            t = TIME_MILESTONES[self.selected_time]
            g = GAME_MILESTONES[self.selected_games]
            total_hours = t['hours']
            hours_per_game = total_hours / g['games']
            
            desc += f"\n⏳ **Tổng cộng:** {g['games']} game · **{total_hours:,} giờ** (~{hours_per_game:.2f}h/game)"
            desc += "\n\n⚠️ *Badge mất 1-2 ngày mới cập nhật.*"
            
        return make_embed(desc, title="Buff Huy Hiệu Discord", gif=True, guild=interaction.guild)

    async def on_time_select(self, interaction: discord.Interaction):
        self.selected_time = interaction.data["values"][0]
        self.start_btn.disabled = not (self.selected_time and self.selected_games)
        await interaction.response.edit_message(embed=self._build_embed(interaction), view=self)
        
    async def on_game_select(self, interaction: discord.Interaction):
        self.selected_games = interaction.data["values"][0]
        self.start_btn.disabled = not (self.selected_time and self.selected_games)
        await interaction.response.edit_message(embed=self._build_embed(interaction), view=self)
        
    async def on_start(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BadgeModal(self.cog, self.selected_time, self.selected_games))


# ---------- Cog ----------

class BadgeSpooferCog(commands.Cog, name="Badge"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_unload(self):
        for task in list(active_tasks.values()):
            task.cancel()
        active_tasks.clear()

    @app_commands.command(name="badge", description="Buff thời gian chơi và số game đã chơi lên Discord")
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def badge_cmd(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        remaining = self._cooldown_remaining(user_id)
        if remaining > 0:
            return await interaction.response.send_message(
                embed=make_embed(
                    f"Cậu mới dùng gần đây. Đợi thêm **{remaining // 60} phút {remaining % 60}s** nữa.",
                    color=ERROR_COLOR, guild=interaction.guild,
                ),
                ephemeral=True,
            )

        if user_id in active_tasks:
            return await interaction.response.send_message(
                embed=make_embed("Cậu đang có 1 phiên chạy rồi, đợi xong đã.",
                                 color=ERROR_COLOR, guild=interaction.guild),
                ephemeral=True,
            )

        async with _running_lock:
            if _running_count >= MAX_CONCURRENT:
                return await interaction.response.send_message(
                    embed=make_embed(
                        f"Bot đang chạy tối đa ({MAX_CONCURRENT} người). Thử lại sau vài phút.",
                        color=ERROR_COLOR, guild=interaction.guild,
                    ),
                    ephemeral=True,
                )

        view = MilestoneView(self)
        embed = view._build_embed(interaction)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @staticmethod
    def _cooldown_remaining(user_id: int) -> int:
        last = _last_used.get(user_id, 0)
        elapsed = time.time() - last
        return max(0, int(COOLDOWN_SECONDS - elapsed)) if elapsed < COOLDOWN_SECONDS else 0

    async def begin(self, interaction: discord.Interaction, token: str, fingerprint: str, time_key: str, game_key: str):
        user_id = interaction.user.id
        time_cfg = TIME_MILESTONES.get(time_key)
        game_cfg = GAME_MILESTONES.get(game_key)
        
        if not time_cfg or not game_cfg:
            return await interaction.followup.send(
                embed=make_embed("Mốc không hợp lệ, thử lại.", color=ERROR_COLOR, guild=interaction.guild),
                ephemeral=True,
            )

        if not _is_likely_token(token):
            return await interaction.followup.send(
                embed=make_embed(
                    "Token trông không đúng format Discord.",
                    color=ERROR_COLOR, guild=interaction.guild,
                ),
                ephemeral=True,
            )

        if fingerprint and not _is_likely_fingerprint(fingerprint):
            return await interaction.followup.send(
                embed=make_embed(
                    "Fingerprint trông không đúng (phải là base64, 100-300 ký tự).\n"
                    "Bỏ trống nếu không có.",
                    color=ERROR_COLOR, guild=interaction.guild,
                ),
                ephemeral=True,
            )

        remaining = self._cooldown_remaining(user_id)
        if remaining > 0:
            return await interaction.followup.send(
                embed=make_embed(f"Còn **{remaining}s** nữa mới được dùng lại.",
                                 color=ERROR_COLOR, guild=interaction.guild),
                ephemeral=True,
            )

        if user_id in active_tasks:
            return await interaction.followup.send(
                embed=make_embed("Cậu đang có phiên chạy rồi.", color=ERROR_COLOR, guild=interaction.guild),
                ephemeral=True,
            )

        global _running_count
        async with _running_lock:
            if _running_count >= MAX_CONCURRENT:
                return await interaction.followup.send(
                    embed=make_embed("Bot đang full slot.", color=ERROR_COLOR, guild=interaction.guild),
                    ephemeral=True,
                )
            _running_count += 1

        _last_used[user_id] = time.time()
        _save_usage()

        total_hours = float(time_cfg["hours"])
        games = int(game_cfg["games"])
        hours_per_game = total_hours / games

        progress = BadgeProgress(interaction, hours_per_game, games)
        await progress.start()

        task = asyncio.create_task(
            self._run(
                interaction, token=token, fingerprint=fingerprint,
                games=games, hours=hours_per_game,
                progress=progress,
            ),
            name=f"badge_{user_id}",
        )
        active_tasks[user_id] = task

    async def _run(self, interaction: discord.Interaction, *, token: str, fingerprint: str,
                   games: int, hours: float, progress: BadgeProgress):
        global _running_count
        proxy = random.choice(PROXIES) if PROXIES else None
        try:
            build = await get_build_number()

            hb, sig = str(uuid.uuid4()), str(uuid.uuid4())
            super_props = _build_super_props(build, sig, hb)

            progress.set("starting")
            await progress.update()
            analytics = await _fetch_analytics_token(token, super_props, proxy=proxy)

            all_games = await _load_games(proxy=proxy)
            if not all_games:
                raise RuntimeError("Không tải được games.json từ CDN")
            count = min(games, len(all_games), MAX_GAMES)
            progress.total = count
            selected = random.sample(all_games, count)

            duration_ms = int(hours * 3600 * 1000)
            progress.set("running")
            await progress.update()

            async with ScienceSpoofer(
                token=token, analytics_token=analytics, super_props=super_props,
                heartbeat_session=hb, launch_signature=sig,
                fingerprint=fingerprint, proxy=proxy,
            ) as spoofer:
                
                # --- ENDPOINT CHECK ---
                test_game = selected[0]
                test_events = spoofer.build_events(test_game, duration_ms)
                test_status = await spoofer.post(test_events)
                
                if test_status == 204:
                    progress.inc(1)
                    await progress.update()
                    remaining_games = selected[1:]
                elif test_status in (401, 403):
                    progress.set("error", f"Auth bị từ chối (mã {test_status}) — token sai hoặc hết hạn")
                    await progress.update()
                    return
                else:
                    progress.set("error", f"Endpoint check thất bại (mã {test_status})")
                    await progress.update()
                    return

                # --- GỬI CÁC GAME CÒN LẠI ---
                for start_idx in range(0, len(remaining_games), BATCH_SIZE):
                    chunk = remaining_games[start_idx:start_idx + BATCH_SIZE]
                    events: list[dict] = []
                    for g in chunk:
                        events.extend(spoofer.build_events(g, duration_ms))
                    status = await spoofer.post(events)
                    
                    if status == 204:
                        progress.inc(len(chunk))
                        await progress.update()
                    elif status in (401, 403):
                        progress.set("error", f"Auth bị từ chối (mã {status})")
                        await progress.update()
                        return
                    else:
                        log(f"[Badge] Batch lỗi HTTP {status}", "warn")
                        await asyncio.sleep(2)
                    
                    await asyncio.sleep(BATCH_DELAY)

            progress.set("done")
            await progress.update()
            log(f"[Badge] {interaction.user.name}: xong {progress.done}/{progress.total} game", "ok")

        except asyncio.CancelledError:
            progress.set("error", "Đã huỷ bởi người dùng")
            await progress.update()
            raise
        except Exception as e:
            progress.set("error", str(e))
            await progress.update()
            log(f"[Badge] Lỗi: {type(e).__name__}: {e}", "error")
        finally:
            active_tasks.pop(interaction.user.id, None)
            async with _running_lock:
                _running_count = max(0, _running_count - 1)


async def setup(bot: commands.Bot):
    await bot.add_cog(BadgeSpooferCog(bot))