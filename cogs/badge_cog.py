import discord
from discord.ext import commands
from discord import app_commands
from discord.app_commands import Choice

from core.common import (
    EMBED_COLOR, ERROR_COLOR, make_embed, log, ROOT_DIR,
    read_json, write_json_atomic
)

import asyncio
import random
import os
import aiohttp
import re
import time
import uuid
import math
from datetime import datetime, timezone
from typing import Optional, Tuple, Any
import json
import base64
import traceback
from dataclasses import dataclass

API_BASE = "https://discord.com/api/v9"
HEARTBEAT_INTERVAL = 20

SUPPORTED_TASKS = [
    "WATCH_VIDEO",
    "PLAY_ON_DESKTOP",
    "STREAM_ON_DESKTOP",
    "PLAY_ACTIVITY",
    "WATCH_VIDEO_ON_MOBILE"
]

PROXIES = []
_PROXIES_PATH = os.path.join(ROOT_DIR, "proxies.txt")
if os.path.exists(_PROXIES_PATH):
    with open(_PROXIES_PATH, "r", encoding="utf-8") as f:
        PROXIES = [line.strip() for line in f if line.strip()]

@dataclass
class QuestSession:
    res_id: str
    task: Optional[asyncio.Task]
    is_initializing: bool = True

# active_quest_tasks lưu: user_id -> QuestSession
active_quest_tasks: dict[int, QuestSession] = {}
_cancel_requests: set[tuple[int, str]] = set()
_is_shutting_down = False
_cog_generation = 0

FALLBACK_BUILD = 504649
BUILD_TTL = 6 * 3600
BUILD_RE = re.compile(r'(?:buildNumber|build_number|BUILD_NUMBER)["\s:=]+["\s]*(\d{5,7})')
_build_cache: dict = {"value": FALLBACK_BUILD, "at": 0.0}

MAX_FAILS = 5
MAX_ATTEMPTS = 3

_running_lock = asyncio.Lock()


# ==========================================
# HELPER FUNCTIONS
# ==========================================

async def _cleanup_quest_reservation(user_id: int, res_id: str):
    global _running_count
    async with _running_lock:
        current = active_quest_tasks.get(user_id)
        if current and current.res_id == res_id:
            active_quest_tasks.pop(user_id, None)
            _running_count = max(0, _running_count - 1)
        _cancel_requests.discard((user_id, res_id))


def _task_done_callback(task: asyncio.Task, user_id: int, res_id: str, api: Any):
    # SỬA LỖI 1: Callback đảm bảo dọn dẹp nếu task bị hủy trước khi chạy vào try/finally
    if api and not api.session.closed:
        asyncio.create_task(api.close())
    # Gọi cleanup idempotent
    asyncio.create_task(_cleanup_quest_reservation(user_id, res_id))
    
    # Lấy lỗi nếu có để log (tránh warning Task exception was never retrieved)
    try:
        exc = task.exception()
        if exc and not isinstance(exc, asyncio.CancelledError):
            log(f"[Quest] Task {res_id} kết thúc với lỗi: {type(exc).__name__}: {exc}", "error")
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


def local_make_embed(desc: str, guild: Optional[discord.Guild] = None, **kwargs) -> discord.Embed:
    embed = make_embed(desc, guild=guild, **kwargs)
    if guild:
        icon_url = guild.icon.url if getattr(guild, "icon", None) else None
        embed.set_footer(text=guild.name, icon_url=icon_url)
    return embed


class QuestAbort(Exception):
    pass


class QuestFail(Exception):
    pass


async def fetch_latest_build_number() -> int:
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": ua}, timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get("https://discord.com/app") as r:
                if r.status != 200:
                    log(f"[Quest] Không lấy được trang Discord ({r.status}), dùng build dự phòng", "warn")
                    return FALLBACK_BUILD
                text = await r.text()

            m = BUILD_RE.search(text)
            if m:
                return int(m.group(1))

            assets = re.findall(r'/assets/([\w.\-]+?)\.js', text)
            for name in reversed(assets[-8:]):
                try:
                    async with session.get(f"https://discord.com/assets/{name}.js") as ar:
                        m = BUILD_RE.search(await ar.text())
                        if m:
                            return int(m.group(1))
                except Exception:
                    continue
    except Exception as e:
        log(f"[Quest] Lỗi lấy build number: {e}", "warn")
    log(f"[Quest] Không tìm thấy build number, dùng build dự phòng {FALLBACK_BUILD}", "warn")
    return FALLBACK_BUILD


async def get_build_number() -> int:
    now = time.time()
    if _build_cache["at"] and now - _build_cache["at"] < BUILD_TTL:
        return _build_cache["value"]
    value = await fetch_latest_build_number()
    _build_cache.update(value=value, at=now)
    log(f"[Quest/Badge] Build number hiện tại: {value}", "info")
    return value


# ==========================================
# QUEST LOGIC
# ==========================================

def make_super_properties(build_number: int) -> str:
    obj = {
        "os": "Windows", "browser": "Discord Client", "release_channel": "stable",
        "client_version": "1.0.9175", "os_version": "10.0.26100", "os_arch": "x64", "app_arch": "x64",
        "system_locale": "en-US",
        "browser_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 Electron/32.2.7 Safari/537.36",
        "browser_version": "32.2.7", "client_build_number": build_number, "native_build_number": 59498,
        "client_event_source": None,
    }
    return base64.b64encode(json.dumps(obj).encode()).decode()


class AsyncDiscordAPI:
    def __init__(self, token: str, build_number: int, proxy: Optional[str] = None):
        self.token = token
        self.proxy = proxy
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9175 Chrome/128.0.6613.186 Electron/32.2.7 Safari/537.36"
        headers = {
            "Authorization": token, "Content-Type": "application/json",
            "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": ua, "X-Super-Properties": make_super_properties(build_number),
            "X-Discord-Locale": "en-US", "X-Discord-Timezone": "Asia/Ho_Chi_Minh",
            "Origin": "https://discord.com", "Referer": "https://discord.com/channels/@me",
        }
        self.session = aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=20))

    async def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Tuple[int, Any]:
        url = f"{API_BASE}{path}"
        last_err = None
        last_status = 0
        
        for attempt in range(3):
            try:
                async with self.session.request(method, url, json=payload, proxy=self.proxy) as r:
                    try:
                        body = await r.json(content_type=None)
                    except Exception:
                        body = await r.text()

                    if r.status == 429:
                        last_status = 429
                        try:
                            retry_after = float(body.get("retry_after", 5)) if isinstance(body, dict) else 5.0
                        except (ValueError, TypeError):
                            retry_after = 5.0
                        
                        if attempt < 2:
                            wait = retry_after + random.uniform(0.5, 1.5)
                            log(f"[Quest] Rate limit 429, đợi {wait:.1f}s...", "warn")
                            await asyncio.sleep(wait)
                        continue
                    
                    if r.status in (500, 502, 503, 504):
                        if method == "POST":
                            log(f"[Quest] POST gặp lỗi {r.status}, không retry để tránh trùng lặp.", "warn")
                            return r.status, body
                        elif attempt < 2:
                            log(f"[Quest] GET lỗi {r.status}, thử lại sau 2s...", "warn")
                            await asyncio.sleep(2)
                            continue
                        
                    return r.status, body
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if method == "POST":
                    log(f"[Quest] POST {path} lỗi mạng không thể retry an toàn: {type(e).__name__}", "warn")
                    return 0, None
                
                last_err = e
                last_status = 0
                if attempt < 2:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
                
        if last_status == 429:
            log(f"[Quest] {method} {path} thất bại do rate limit 429 sau 3 lần thử.", "warn")
            return 429, None
            
        log(f"[Quest] {method} {path} lỗi kết nối: {last_err}", "warn")
        return 0, None

    async def get(self, path: str) -> Tuple[int, Any]: return await self._request("GET", path)
    async def post(self, path: str, payload: Optional[dict] = None) -> Tuple[int, Any]: return await self._request("POST", path, payload)

    async def validate_token(self) -> bool:
        status, _ = await self.get("/users/@me")
        return status == 200

    async def close(self):
        await self.session.close()


def _get(d, *keys):
    if not isinstance(d, dict): return None
    for k in keys:
        if k in d: return d[k]
    return None

def _user_status(quest: dict) -> dict:
    us = _get(quest, "userStatus", "user_status")
    return us if isinstance(us, dict) else {}

def get_task_config(quest: dict):
    return _get(quest.get("config") or {}, "taskConfig", "task_config", "taskConfigV2", "task_config_v2")

def get_quest_name(quest: dict) -> str:
    cfg = quest.get("config")
    if not isinstance(cfg, dict): cfg = {}
    
    msgs = cfg.get("messages")
    if not isinstance(msgs, dict): msgs = {}
    
    app = cfg.get("application")
    if not isinstance(app, dict): app = {}
    
    name = _get(msgs, "questName", "quest_name") or _get(msgs, "gameTitle", "game_title") or app.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return f"Quest#{quest.get('id', '?')}"

def is_valid_task_config(task_data: Any) -> bool:
    if not isinstance(task_data, dict): return False
    val = task_data.get("target")
    
    # SỬA LỖI 4: Chặn bool, yêu cầu số nguyên dương, bọc OverflowError
    if not isinstance(val, (int, float)) or isinstance(val, bool): return False
    try:
        if not math.isfinite(val) or val < 1: return False
    except OverflowError:
        return False
        
    return True

def is_completable(quest: dict) -> bool:
    expires = _get(quest.get("config") or {}, "expiresAt", "expires_at")
    if expires:
        try:
            if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc): 
                return False
        except (ValueError, TypeError, AttributeError): 
            log(f"[Quest] Lỗi parse expires_at: {expires}", "warn")
            return False
            
    tc = get_task_config(quest)
    tasks = tc.get("tasks") if isinstance(tc, dict) else None
    if not isinstance(tasks, dict): return False
    
    return any(is_valid_task_config(tasks.get(t)) for t in SUPPORTED_TASKS)

def is_enrolled(quest: dict) -> bool: return bool(_get(_user_status(quest), "enrolledAt", "enrolled_at"))
def is_completed(quest: dict) -> bool: return bool(_get(_user_status(quest), "completedAt", "completed_at"))

def get_task_type(quest: dict) -> Optional[str]:
    tc = get_task_config(quest)
    tasks = tc.get("tasks") if isinstance(tc, dict) else None
    if not isinstance(tasks, dict): return None
    for t in SUPPORTED_TASKS:
        if is_valid_task_config(tasks.get(t)): 
            return t
    return None

def get_seconds_needed(quest: dict) -> int:
    t = get_task_type(quest)
    if not t: return 0
    tc = get_task_config(quest)
    if not isinstance(tc, dict): return 0
    tasks = tc.get("tasks")
    if not isinstance(tasks, dict): return 0
    task_data = tasks.get(t)
    if isinstance(task_data, dict):
        val = task_data.get("target", 0)
        if is_valid_task_config(task_data):
            return int(val)
    return 0

def get_seconds_done(quest: dict, sec_needed: int = 99999999) -> float:
    t = get_task_type(quest)
    if not t: return 0.0
    
    progress = _user_status(quest).get("progress")
    if not isinstance(progress, dict): return 0.0
    
    entry = progress.get(t)
    if not isinstance(entry, dict): return 0.0
    
    val = entry.get("value", 0)
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        try:
            if math.isfinite(val) and val >= 0:
                # SỬA LỖI 5: Clamp không vượt quá sec_needed
                return min(float(val), float(sec_needed))
        except OverflowError:
            pass
            
    return 0.0


LIVE_MIN_GAP = 10
LIVE_TICK = 5


def progress_bar(pct: float, width: int = 12) -> str:
    filled = max(0, min(width, round(width * pct / 100)))
    return "▰" * filled + "▱" * (width - filled)


def fmt_dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}g {m:02d}p"
    if m:
        return f"{m}p {s:02d}s"
    return f"{s}s"


class QuestLive:
    PHASES = {
        "starting": "Đang khởi động",
        "scanning": "Đang quét quest",
        "enrolling": "Đang nhận quest",
        "farming": "Đang cày quest",
        "stopped": "Đã dừng",
    }

    def __init__(self, user: discord.abc.User, channel):
        self.user = user
        self.channel = channel
        self.message: Optional[discord.Message] = None
        self.started_at = time.time()
        self.phase = "starting"
        self.error = False
        self.stop_reason: Optional[str] = None
        self.quest_name: Optional[str] = None
        self.task_type: Optional[str] = None
        self.sec_needed = 0
        self.sec_done = 0.0
        self.quest_started_at = 0.0
        self.quest_start_done = 0.0
        self.completed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self._last_edit = 0.0
        self._last_sig = None
        self._lock = asyncio.Lock()

    def begin_quest(self, name: str, task_type: str, needed: int, done: float):
        self.phase = "farming"
        self.quest_name, self.task_type = name, task_type
        self.sec_needed, self.sec_done = needed, done
        self.quest_started_at, self.quest_start_done = time.time(), done

    def set_progress(self, done: float):
        self.sec_done = done

    def end_quest(self):
        self.quest_name = self.task_type = None
        self.sec_needed, self.sec_done = 0, 0.0

    def _eta(self) -> str:
        elapsed = time.time() - self.quest_started_at
        gained = self.sec_done - self.quest_start_done
        if elapsed < 5 or gained <= 0:
            return "đang tính..."
        rate = gained / elapsed
        return "~" + fmt_dur((self.sec_needed - self.sec_done) / rate)

    def signature(self):
        return (self.phase, self.quest_name, int(self.sec_done), len(self.completed), len(self.failed), self.stop_reason)

    def build_embed(self) -> discord.Embed:
        lines = []
        if self.phase == "farming" and self.quest_name:
            pct = min(100, self.sec_done / self.sec_needed * 100) if self.sec_needed else 0
            lines.append(f"**{self.quest_name}** · `{self.task_type}`")
            lines.append(f"{progress_bar(pct)} **{int(pct)}%** · {int(self.sec_done)}/{int(self.sec_needed)}s")
            lines.append(f"Còn lại: {self._eta()}")
        elif self.phase == "stopped":
            lines.append(self.stop_reason or "Đã dừng hệ thống cày quest.")
        else:
            lines.append("Đợi chút, Joker đang xử lý...")

        kwargs = {"color": ERROR_COLOR} if (self.phase == "stopped" and self.error) else {}
        embed = local_make_embed("\n".join(lines), title=f"Cày Quest • {self.PHASES.get(self.phase, self.phase)}", guild=self.channel.guild, **kwargs)

        if self.completed:
            shown = "\n".join(f"✅ {n}" for n in self.completed[-5:])
            embed.add_field(name=f"Đã hoàn thành ({len(self.completed)})", value=shown[:1000], inline=False)
        if self.failed:
            shown = "\n".join(f"⚠️ {n}: {r}" for n, r in self.failed[-3:])
            embed.add_field(name=f"Lỗi ({len(self.failed)})", value=shown[:1000], inline=False)
        embed.add_field(name="Bắt đầu", value=f"<t:{int(self.started_at)}:R>", inline=True)
        return embed

    async def push(self, force: bool = False, final: bool = False):
        if self.message is None:
            return
        async with self._lock:
            now = time.time()
            sig = self.signature()
            if not (force or final):
                if sig == self._last_sig or now - self._last_edit < LIVE_MIN_GAP:
                    return
            try:
                await self.message.edit(embed=self.build_embed(), view=None if final else discord.utils.MISSING)
            except discord.HTTPException as e:
                log(f"[Quest] Không sửa được tin nhắn tiến độ: {e}", "warn")
                return
            self._last_edit, self._last_sig = now, sig

    async def ping(self, title: str, desc: str, error: bool = False):
        kwargs = {"color": ERROR_COLOR} if error else {"gif": True}
        try:
            await self.channel.send(content=self.user.mention, embed=local_make_embed(desc, title=title, guild=self.channel.guild, **kwargs))
        except Exception:
            pass


async def live_updater(live: QuestLive):
    while True:
        await asyncio.sleep(LIVE_TICK)
        await live.push()


class AsyncQuestAutocompleter:
    def __init__(self, api: AsyncDiscordAPI, user_name: str, live: QuestLive):
        self.api = api
        self.user_name = user_name
        self.live = live

    def _fail(self, name: str, status: int, body: Any, fails: int) -> int:
        fails += 1
        log(f"[Quest] {name}: HTTP {status} {str(body)[:150]} ({fails}/{MAX_FAILS})", "warn")
        if status == 401:
            raise QuestAbort("Token hết hạn hoặc bị Discord từ chối (HTTP 401)")
        if fails >= MAX_FAILS:
            raise QuestFail(f"lỗi liên tục (HTTP {status}): {str(body)[:120]}")
        return fails

    async def fetch_quests(self) -> list:
        status, data = await self.api.get("/quests/@me")
        if status == 401:
            raise QuestAbort("Token hết hạn hoặc bị Discord từ chối (HTTP 401)")
        
        if status != 200:
            raise QuestFail(f"Lỗi API lấy danh sách quest (HTTP {status}). Đang thử lại...")
            
        if isinstance(data, dict):
            data = data.get("quests") or []
            
        valid_quests = []
        if isinstance(data, list):
            for q in data:
                if isinstance(q, dict) and "id" in q and not isinstance(q["id"], bool):
                    qid_str = str(q["id"]).strip()
                    if qid_str.isdigit():
                        q["id"] = qid_str
                        valid_quests.append(q)
                    else:
                        log(f"[Quest] Bỏ qua quest có ID sai format: {q.get('id')}", "warn")
                else:
                    log(f"[Quest] Bỏ qua quest sai schema: {q}", "warn")
                    
        return valid_quests

    async def enroll_quest(self, quest: dict) -> bool:
        qid = quest["id"]
        payload = {
            "location": 11, "is_targeted": False, "metadata_raw": None, "metadata_sealed": None,
            "traffic_metadata_raw": quest.get("traffic_metadata_raw"), "traffic_metadata_sealed": quest.get("traffic_metadata_sealed")
        }
        status, body = await self.api.post(f"/quests/{qid}/enroll", payload)
        if status not in (200, 201, 204):
            log(f"[Quest] Nhận \"{get_quest_name(quest)}\" thất bại: HTTP {status} {str(body)[:150]}", "warn")
        return status in (200, 201, 204)

    async def auto_accept(self, quests: list) -> list:
        pending = [q for q in quests if not is_enrolled(q) and not is_completed(q) and is_completable(q)]
        if not pending:
            return quests
        self.live.phase = "enrolling"
        for q in pending:
            await self.enroll_quest(q)
            await asyncio.sleep(3)
        await asyncio.sleep(2)
        self.live.phase = "scanning"
        return await self.fetch_quests()

    async def complete_video(self, quest: dict) -> bool:
        qid, name = quest["id"], get_quest_name(quest)
        sec_needed = get_seconds_needed(quest)
        sec_done = get_seconds_done(quest, sec_needed)

        enrolled_at_str = _get(_user_status(quest), "enrolledAt", "enrolled_at")
        enrolled_ts = None
        
        if enrolled_at_str:
            if not isinstance(enrolled_at_str, str):
                raise QuestFail("Dữ liệu enrolled_at không phải chuỗi thời gian.")
            try:
                dt = datetime.fromisoformat(enrolled_at_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                enrolled_ts = dt.timestamp()
            except (ValueError, TypeError, AttributeError):
                raise QuestFail("Dữ liệu enrolled_at không hợp lệ, không thể tính thời gian.")
        
        if enrolled_ts is None:
            enrolled_ts = time.time()

        speed, max_future, fails = 7, 10, 0
        while sec_done < sec_needed:
            max_allowed = (time.time() - enrolled_ts) + max_future
            diff = max_allowed - sec_done
            timestamp = sec_done + speed

            if diff >= speed:
                status, body = await self.api.post(f"/quests/{qid}/video-progress", {"timestamp": min(sec_needed, timestamp + random.random())})
                if status == 200 and isinstance(body, dict):
                    fails = 0
                    if body.get("completed_at"):
                        return True
                    sec_done = min(sec_needed, timestamp)
                    self.live.set_progress(sec_done)
                else:
                    fails = self._fail(name, status, body, fails)

            if timestamp >= sec_needed:
                break
            await asyncio.sleep(1)

        status, body = await self.api.post(f"/quests/{qid}/video-progress", {"timestamp": sec_needed})
        if status != 200:
            raise QuestFail(f"gửi tiến độ cuối thất bại (HTTP {status})")
            
        if isinstance(body, dict) and body.get("completed_at"):
            return True
            
        await asyncio.sleep(2)
        quests = await self.fetch_quests()
        updated = next((q for q in quests if q.get("id") == qid), None)
        if updated and is_completed(updated):
            return True
            
        raise QuestFail("Đã gửi tiến độ cuối nhưng Discord chưa báo hoàn thành.")

    async def complete_heartbeat(self, quest: dict, task_type: str) -> bool:
        qid, name = quest["id"], get_quest_name(quest)
        sec_needed = get_seconds_needed(quest)
        sec_done = get_seconds_done(quest, sec_needed)

        stream_key = "call:0:1" if task_type == "PLAY_ACTIVITY" else f"call:0:{random.randint(1000, 30000)}"
        deadline = time.time() + max(0, sec_needed - sec_done) * 1.5 + 300
        fails = 0

        while sec_done < sec_needed:
            if time.time() > deadline:
                raise QuestFail("quá thời gian chờ mà tiến độ không tăng")
            status, body = await self.api.post(f"/quests/{qid}/heartbeat", {"stream_key": stream_key, "terminal": False})
            if status == 200 and isinstance(body, dict):
                fails = 0
                
                pdata = body.get("progress")
                if isinstance(pdata, dict) and task_type in pdata:
                    entry = pdata[task_type]
                    if isinstance(entry, dict):
                        val = entry.get("value")
                        if isinstance(val, (int, float)) and not isinstance(val, bool):
                            try:
                                if math.isfinite(val) and val >= 0:
                                    sec_done = min(float(val), float(sec_needed))
                                    self.live.set_progress(sec_done)
                            except OverflowError:
                                pass
                            
                if body.get("completed_at") or sec_done >= sec_needed:
                    break
            else:
                fails = self._fail(name, status, body, fails)
            await asyncio.sleep(HEARTBEAT_INTERVAL)

        status, body = await self.api.post(f"/quests/{qid}/heartbeat", {"stream_key": stream_key, "terminal": True})
        if status != 200:
            raise QuestFail(f"Gửi heartbeat kết thúc thất bại (HTTP {status})")
            
        if isinstance(body, dict) and body.get("completed_at"):
            return True
            
        await asyncio.sleep(2)
        quests = await self.fetch_quests()
        updated = next((q for q in quests if q.get("id") == qid), None)
        if updated and is_completed(updated):
            return True
            
        raise QuestFail("Đã gửi heartbeat kết thúc nhưng Discord chưa báo hoàn thành.")

    async def process_quest(self, quest: dict) -> bool:
        task_type = get_task_type(quest)
        if not task_type:
            return False
        log(f"[Quest] {self.user_name} bắt đầu cày: {get_quest_name(quest)} ({task_type})", "info")
        self.live.begin_quest(get_quest_name(quest), task_type, get_seconds_needed(quest), get_seconds_done(quest, get_seconds_needed(quest)))
        try:
            if task_type in ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE"):
                return await self.complete_video(quest)
            return await self.complete_heartbeat(quest, task_type)
        finally:
            self.live.end_quest()


async def background_quest_worker(bot: commands.Bot, user: discord.abc.User, api: AsyncDiscordAPI, live: QuestLive, res_id: str):
    completer = AsyncQuestAutocompleter(api, user.name, live)
    attempts: dict = {}
    worker_fails = 0
    updater = None

    try:
        # SỬA LỖI 2: Khởi tạo updater bên trong try để đảm bảo finally được chạy
        updater = asyncio.create_task(live_updater(live))
        
        live.phase = "scanning"
        while True:
            try:
                quests = await completer.fetch_quests()
                quests = await completer.auto_accept(quests)

                actionable = [q for q in quests if is_enrolled(q) and not is_completed(q) and is_completable(q)]
                actionable = [q for q in actionable if attempts.get(q.get("id"), 0) < MAX_ATTEMPTS]

                if not actionable:
                    live.phase = "stopped"
                    if live.completed:
                        live.stop_reason = f"Đã cày xong {len(live.completed)} quest! Joker rút lui."
                        await live.ping("Phi vụ hoàn tất!", "Joker đã hoàn thành toàn bộ quest hiện có cho cậu. Khi nào có quest mới thì cứ gọi Joker.")
                    elif live.failed:
                        live.stop_reason = "Đã dừng do tất cả quest đều bị lỗi liên tục."
                    else:
                        live.stop_reason = "Không tìm thấy quest nào cần cày cả."
                        await live.ping("Không có quest!", "Hiện tại tài khoản của cậu không có quest nào cần cày.")

                    live.error = bool(live.failed)
                    break

                for q in actionable:
                    qid, name = q.get("id"), get_quest_name(q)
                    attempts[qid] = attempts.get(qid, 0) + 1
                    try:
                        if await completer.process_quest(q):
                            live.completed.append(name)
                            await live.push(force=True)
                    except QuestFail as e:
                        log(f"[Quest] {name} thất bại: {e}", "warn")
                        live.failed.append((name, str(e)))
                        await live.push(force=True)
                        await live.ping("Quest thất bại", f"Quest **{name}** bị lỗi: {e}", error=True)

                await asyncio.sleep(3)
                worker_fails = 0

            except (QuestAbort, asyncio.CancelledError):
                raise
            except Exception as e:
                worker_fails += 1
                log(f"[Quest] Lỗi vòng quét lần {worker_fails} (sẽ thử lại): {e}\n{traceback.format_exc()}", "error")
                if worker_fails >= MAX_FAILS:
                    live.phase, live.stop_reason, live.error = "stopped", "Dừng do lỗi API liên tục 5 lần.", True
                    await live.ping("Hệ thống đã dừng", "Joker không thể lấy danh sách quest do lỗi mạng liên tục.", error=True)
                    break
                await asyncio.sleep(5)

    except asyncio.CancelledError:
        live.phase, live.stop_reason, live.error = "stopped", "Cậu đã dừng cày quest ngang chừng.", False
        log(f"[Quest] Đã dừng tiến trình của {user.name}.", "warn")
    except QuestAbort as e:
        live.phase, live.stop_reason, live.error = "stopped", f"Dừng vì lỗi: {e}", True
        log(f"[Quest] Dừng của {user.name}: {e}", "error")
        await live.ping("Hệ thống đã dừng", f"Joker phải dừng cày quest: {e}", error=True)
    finally:
        # Cleanup an toàn
        if updater is not None:
            try:
                updater.cancel()
                await updater
            except (asyncio.CancelledError, Exception):
                pass
            
        try:
            await live.push(final=True)
        except Exception:
            pass
            
        try:
            await api.close()
        except Exception:
            pass
            
        await _cleanup_quest_reservation(user.id, res_id)


async def set_hypesquad_house(user_token: str, house_name: str) -> tuple[bool, str]:
    houses = {"bravery": 1, "brilliance": 2, "balance": 3, "leave": 0}
    key = house_name.lower()
    if key not in houses: return False, "Nhà HypeSquad không hợp lệ!"
    headers = {"Authorization": user_token, "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            if houses[key] == 0:
                async with session.post(f"{API_BASE}/hypesquad/online", json={}, headers=headers, timeout=10) as r:
                    ok = r.status in (200, 204)
                if not ok:
                    async with session.delete(f"{API_BASE}/hypesquad/online", headers=headers, timeout=10) as r_del:
                        ok = r_del.status in (200, 204)
            else:
                async with session.post(f"{API_BASE}/hypesquad/online", json={"house_id": houses[key]}, headers=headers, timeout=10) as r:
                    ok = r.status in (200, 204)

            if ok:
                msg = ("Đã gỡ huy hiệu **HypeSquad**." if houses[key] == 0
                       else f"Đã gán thành công huy hiệu **HypeSquad {house_name.capitalize()}** ")
                return True, msg
            return False, "Token lỗi hoặc hết hạn rồi nhé!"
    except Exception as e: return False, f"Lỗi kết nối: {str(e)}"


# ==========================================
# SPOOFER LOGIC
# ==========================================

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

USER_AGENT_BADGE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) discord/1.0.9253 Chrome/148.0.7778.280 "
    "Electron/42.7.1 Safari/537.36"
)

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

# active_tasks lưu: user_id -> QuestSession
active_tasks: dict[int, QuestSession] = {}
_running_count = 0
_last_used: dict[int, float] = {}
_games_cache: dict = {"data": None, "at": 0.0}

def _save_usage():
    try:
        write_json_atomic(USAGE_FILE, {str(k): v for k, v in _last_used.items()}, indent=2)
    except Exception as e:
        log(f"[Badge] Lỗi nghiêm trọng: Không thể lưu cooldown. {type(e).__name__}: {e}", "error")

def _cleanup_usage():
    now = time.time()
    to_delete = [uid for uid, ts in _last_used.items() if now - ts > 7 * 86400]
    if to_delete:
        for uid in to_delete:
            del _last_used[uid]
        _save_usage()

def _load_usage():
    data = read_json(USAGE_FILE, {})
    valid_data = {}
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                valid_data[int(k)] = float(v)
            except (ValueError, TypeError):
                log(f"[Badge] Bỏ qua bản ghi cooldown hỏng: key={k}, value={v}", "warn")
    return valid_data

_last_used = _load_usage()
_cleanup_usage()


def _is_likely_token(t: str) -> bool:
    return isinstance(t, str) and t.count(".") == 2 and 50 <= len(t) <= 200


def _is_likely_fingerprint(t: str) -> bool:
    if not t:
        return True
    t = t.strip()
    if not (100 <= len(t) <= 300):
        return False
    return bool(re.match(r"^[A-Za-z0-9+/=]+$", t))


@dataclass(frozen=True)
class SpooferGame:
    id: str
    name: str
    exe: str


def _win_exe(game: dict) -> str:
    for e in game.get("executables", []):
        if isinstance(e, dict) and e.get("os") == "win32" and isinstance(e.get("name"), str):
            return e["name"]
    return "game.exe"


def _parse_games(raw: str) -> list[SpooferGame]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    
    if not isinstance(data, list):
        return []

    games, seen = [], set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            name = "Unknown"

        gid = str(entry.get("id", ""))
        if not gid.isdigit() or gid in seen:
            continue
        
        executables = entry.get("executables", [])
        if not isinstance(executables, list):
            continue
            
        if not any(isinstance(e, dict) and e.get("os") == "win32" and e.get("name") for e in executables):
            continue
        
        seen.add(gid)
        games.append(SpooferGame(gid, name, _win_exe(entry)))
    return games


async def _load_games(proxy: Optional[str] = None) -> list[SpooferGame]:
    now = time.time()
    if _games_cache["data"] is not None and now - _games_cache["at"] < GAMES_TTL:
        return _games_cache["data"]

    cache_path = os.path.join(ROOT_DIR, "data", "games.json")
    
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if raw:
                games = _parse_games(raw)
                if games: 
                    _games_cache.update(data=games, at=now)
                    return games
        except OSError:
            pass
        
        try:
            os.remove(cache_path)
        except OSError:
            pass

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(GAMES_CDN_URL, proxy=proxy) as r:
                if r.status == 200:
                    raw = await r.text()
                    games = _parse_games(raw)
                    if games:
                        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                        tmp_path = f"{cache_path}.{uuid.uuid4().hex}.tmp"
                        try:
                            with open(tmp_path, "w", encoding="utf-8") as f:
                                f.write(raw)
                            os.replace(tmp_path, cache_path)
                        finally:
                            if os.path.exists(tmp_path):
                                try: os.remove(tmp_path)
                                except OSError: pass
                        
                        _games_cache.update(data=games, at=now)
                        return games
    except Exception as e:
        log(f"[Badge] Tải games.json lỗi: {type(e).__name__}", "warn")

    return []


def _build_super_props(build_number: int, launch_signature: str, heartbeat_session: str) -> str:
    obj = {
        "os": "Windows", "browser": "Discord Client", "release_channel": "stable",
        "client_version": "1.0.9253", "os_version": "10.0.26200", "os_arch": "x64", "app_arch": "x64",
        "system_locale": "vi-VN",
        "browser_user_agent": USER_AGENT_BADGE, "browser_version": "42.7.1",
        "client_build_number": build_number, "native_build_number": 88414,
        "client_event_source": None,
        "client_launch_id": str(uuid.uuid4()),
        "launch_signature": launch_signature,
        "client_heartbeat_session_id": heartbeat_session,
    }
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


async def _fetch_analytics_token(token: str, super_props: str, proxy: Optional[str] = None) -> str:
    headers = {"authorization": token, "user-agent": USER_AGENT_BADGE, "x-super-properties": super_props}
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
            "user-agent": USER_AGENT_BADGE,
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


class BadgeProgress:
    def __init__(self, interaction: discord.Interaction, hours_per_game: float, total: int):
        self.interaction = interaction
        self.hours = hours_per_game
        self.total = total
        self.done = 0
        self.failed = 0
        self.started_at = time.time()
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()
        self.phase = "starting"
        self.error: Optional[str] = None

    def set(self, phase: str, error: Optional[str] = None):
        self.phase, self.error = phase, error

    def inc(self, n: int):
        self.done += n

    def inc_fail(self, n: int):
        self.failed += n

    def _embed(self) -> discord.Embed:
        pct = int(self.done / self.total * 100) if self.total else 0
        filled = max(0, min(12, round(12 * pct / 100)))
        bar = "▰" * filled + "▱" * (12 - filled)
        
        if self.phase == "starting":
            desc = "Đang xác thực token và kết nối Discord..."
        elif self.phase == "running":
            total_claimed = self.done * self.hours
            desc = f"{bar} **{pct}%**\nĐã gửi: **{self.done}/{self.total}** game · **{total_claimed:,.0f}h**"
            if self.failed > 0:
                desc += f"\n⚠️ Đang lỗi: **{self.failed}** game"
        elif self.phase == "done":
            total_claimed = self.done * self.hours
            desc = (f"✅ **Hoàn tất** {self.done}/{self.total} game\n"
                    f"Đã buff **{total_claimed:,.0f} giờ** trong {int(time.time() - self.started_at)}s\n\n"
                    f"Badge sẽ cập nhật sau **1-2 ngày**.")
        elif self.phase == "error":
            total_claimed = self.done * self.hours
            desc = (f"⚠️ **Dừng giữa chừng**: {self.error}\n"
                    f"Thành công: {self.done}/{self.total} game (~{total_claimed:,.0f}h).\n"
                    f"Thất bại: {self.failed} game.")
        else:
            desc = "..."

        kwargs = {"color": ERROR_COLOR} if self.phase in ("error", "partial") else {}
        return make_embed(desc, title="Buff Huy Hiệu Discord", guild=self.interaction.guild, **kwargs)

    async def start(self):
        try:
            self.message = await self.interaction.followup.send(
                embed=self._embed(), ephemeral=True, wait=True,
            )
        except discord.HTTPException as e:
            log(f"[Badge] Không gửi được progress: {e}", "error")
            raise RuntimeError("Không thể gửi tin nhắn tiến trình, thử lại sau.") from e

    async def update(self):
        if self.message is None:
            return
        async with self._lock:
            try:
                await self.message.edit(embed=self._embed())
            except discord.HTTPException:
                pass


# ==========================================
# UI CLASSES (Modals & Views)
# ==========================================

class QuestTokenModal(discord.ui.Modal, title="Nhập Token Cày Quest"):
    token_input = discord.ui.TextInput(
        label="Token của cậu",
        placeholder="Joker sẽ dùng Token này để cày sạch sành sanh quest...",
        style=discord.TextStyle.short,
        required=True
    )

    def __init__(self, bot: commands.Bot, gen: int):
        super().__init__()
        self.bot = bot
        self.gen = gen

    async def on_submit(self, interaction: discord.Interaction):
        if self.gen != _cog_generation or _is_shutting_down:
            return await interaction.response.send_message(embed=local_make_embed("Hệ thống vừa khởi động lại, vui lòng thử lại.", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)
            
        token = self.token_input.value.strip()
        user_id = interaction.user.id

        global _running_count
        res_id = str(uuid.uuid4())
        err_msg = None
        
        current_task = asyncio.current_task()
        async with _running_lock:
            if self.gen != _cog_generation or _is_shutting_down:
                err_msg = "Hệ thống vừa khởi động lại, vui lòng thử lại."
            elif user_id in active_quest_tasks:
                err_msg = "Cậu đang cày quest rồi mà!"
            elif _running_count >= MAX_CONCURRENT:
                err_msg = "Bot đang quá tải, thử lại sau nhé!"
            else:
                active_quest_tasks[user_id] = QuestSession(res_id, current_task, is_initializing=True)
                _running_count += 1

        if err_msg:
            return await interaction.response.send_message(embed=local_make_embed(err_msg, color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)

        api = None
        task_created = False
        live = None
        
        try:
            await interaction.response.defer(ephemeral=True)
            api = AsyncDiscordAPI(token, await get_build_number(), proxy=random.choice(PROXIES) if PROXIES else None)
            
            if self.gen != _cog_generation or _is_shutting_down: 
                raise ValueError("Hệ thống vừa khởi động lại.")
            if (user_id, res_id) in _cancel_requests: 
                raise ValueError("Đã hủy theo yêu cầu của cậu.")

            if not await api.validate_token():
                raise ValueError("Token không hợp lệ hoặc đã hết hạn, cậu kiểm tra lại nhé!")

            channel = interaction.channel
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(interaction.channel_id)
                except discord.HTTPException:
                    channel = None
            if channel is None:
                raise ValueError("Joker không xác định được kênh này, cậu thử lại ở một kênh text bình thường nhé!")

            live = QuestLive(interaction.user, channel)
            try:
                live.message = await channel.send(embed=live.build_embed())
            except Exception:
                live = None
                raise ValueError("Joker không gửi được tin nhắn vào kênh này, kiểm tra quyền của Joker nhé!")

            err_to_delete = False
            async with _running_lock:
                if self.gen != _cog_generation or _is_shutting_down:
                    err_to_delete = True
                    err_msg = "Hệ thống vừa khởi động lại."
                elif (user_id, res_id) in _cancel_requests:
                    err_to_delete = True
                    err_msg = "Đã hủy theo yêu cầu của cậu."
                else:
                    task = asyncio.create_task(background_quest_worker(self.bot, interaction.user, api, live, res_id))
                    # SỬA LỖI 1: Gắn callback
                    task.add_done_callback(lambda t, uid=user_id, rid=res_id, a=api: _task_done_callback(t, uid, rid, a))
                    
                    session = active_quest_tasks.get(user_id)
                    if session and session.res_id == res_id:
                        session.task = task
                        session.is_initializing = False
            
            if err_to_delete:
                raise ValueError(err_msg)
            
            task_created = True
            api = None # Nhường quyền quản lý api cho task

            msg = "Đã khởi động chế độ cày quest! Joker sẽ tự động ngắt khi xong việc, cậu xem tiến độ trực tiếp ở tin nhắn Joker vừa gửi trong kênh."
            await interaction.followup.send(embed=local_make_embed(msg, title="Cày Quest Thành Công!", gif=True, guild=interaction.guild), ephemeral=True)
            
        except Exception as e:
            log(f"[Quest] Lỗi khởi tạo: {e}", "error")
            try:
                await interaction.followup.send(embed=local_make_embed(str(e), color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)
            except:
                pass
        finally:
            if not task_created:
                if live and getattr(live, "message", None):
                    try:
                        await live.message.delete()
                    except Exception:
                        try:
                            await live.message.edit(embed=local_make_embed("Khởi tạo tiến trình thất bại.", color=ERROR_COLOR, guild=interaction.guild))
                        except Exception:
                            pass
                await _cleanup_quest_reservation(user_id, res_id)
            if api is not None:
                await api.close()


class QuestToSView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=120)
        self.bot = bot
        self.gen = _cog_generation

    @discord.ui.button(label="Bật Cày Quest", style=discord.ButtonStyle.green, custom_id="accept_btn")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        err_msg = None
        async with _running_lock:
            if self.gen != _cog_generation or _is_shutting_down:
                err_msg = "Bot đang khởi động lại, thử lại sau nhé!"
            elif interaction.user.id in active_quest_tasks:
                err_msg = "Cậu đang nằm trong hệ thống làm quest rồi mà, khi nào muốn dừng ngang thì bấm nút đỏ nhé!"

        if err_msg:
            return await interaction.response.send_message(embed=local_make_embed(err_msg, color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)

        await interaction.response.send_modal(QuestTokenModal(self.bot, self.gen))

    @discord.ui.button(label="Dừng Cày Ngang Chừng", style=discord.ButtonStyle.red, custom_id="stop_btn")
    async def stop_quest(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        err_msg = None
        msg = None

        async with _running_lock:
            if self.gen != _cog_generation or _is_shutting_down:
                err_msg = "Hệ thống đã khởi động lại, nút này không còn hiệu lực."
            elif user_id not in active_quest_tasks:
                err_msg = "Cậu có đang cày đâu mà đòi dừng!"
            else:
                session = active_quest_tasks.get(user_id)
                if session.is_initializing:
                    _cancel_requests.add((user_id, session.res_id))
                    msg = "Đã gửi yêu cầu hủy, Joker đang ngắt tiến trình khởi tạo..."
                else:
                    msg = "Đã ngắt chế độ cày quest cho cậu."
                
                if session.task:
                    session.task.cancel()

        if err_msg:
            return await interaction.response.send_message(embed=local_make_embed(err_msg, color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)
        
        await interaction.response.send_message(embed=local_make_embed(msg, guild=interaction.guild), ephemeral=True)


class HypeSquadTokenModal(discord.ui.Modal, title="Nhập Token HypeSquad"):
    token_input = discord.ui.TextInput(
        label="Discord Token của cậu",
        placeholder="Nhập token để đổi nhà HypeSquad...",
        style=discord.TextStyle.short,
        required=True
    )

    def __init__(self, house_choice: str):
        super().__init__()
        self.house_choice = house_choice

    async def on_submit(self, interaction: discord.Interaction):
        token = self.token_input.value.strip()
        await interaction.response.defer(ephemeral=True)

        success, msg = await set_hypesquad_house(token, self.house_choice)
        await interaction.followup.send(embed=local_make_embed(msg, title="Nhận Huy Hiệu Thành Công!" if success else "Thao tác thất bại", color=EMBED_COLOR if success else ERROR_COLOR, gif=success, guild=interaction.guild), ephemeral=True)


class HypeSquadView(discord.ui.View):
    def __init__(self, house_choice: str):
        super().__init__(timeout=120)
        self.house_choice = house_choice

    @discord.ui.button(label="Xác nhận nhận HypeSquad", style=discord.ButtonStyle.blurple, custom_id="hypesquad_btn")
    async def confirm_hypesquad(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(HypeSquadTokenModal(self.house_choice))
        for child in self.children: child.disabled = True
        try: await interaction.message.edit(view=self)
        except: pass

    @discord.ui.button(label="Hủy bỏ", style=discord.ButtonStyle.gray, custom_id="cancel_hypesquad_btn")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(embed=local_make_embed("Đã hủy thao tác đổi nhà HypeSquad.", title="Đã hủy", guild=interaction.guild), view=self)


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

    def __init__(self, cog: "BadgeCog", time_key: str, game_key: str, gen: int):
        super().__init__()
        self.cog = cog
        self.time_key = time_key
        self.game_key = game_key
        self.gen = gen

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.begin(
            interaction,
            token=self.token_input.value.strip(),
            fingerprint=self.fingerprint_input.value.strip(),
            time_key=self.time_key,
            game_key=self.game_key,
            gen=self.gen
        )


class MilestoneView(discord.ui.View):
    def __init__(self, cog: "BadgeCog"):
        super().__init__(timeout=300)
        self.cog = cog
        self.gen = _cog_generation
        self.selected_time = None
        self.selected_games = None
        
        time_opts = [
            discord.SelectOption(
                label=f"Mốc {k}: {v['name']} ({v['hours']} giờ)",
                value=k, emoji="🗡️"
            ) for k, v in TIME_MILESTONES.items()
        ]
        self.time_select = discord.ui.Select(placeholder="Chọn mốc Game Time...", options=time_opts, custom_id="time_select")
        self.time_select.callback = self.on_time_select
        self.add_item(self.time_select)
        
        game_opts = [
            discord.SelectOption(
                label=f"Mốc {k}: {v['name']} ({v['games']} game)",
                value=k, emoji="🎮"
            ) for k, v in GAME_MILESTONES.items()
        ]
        self.game_select = discord.ui.Select(placeholder="Chọn mốc Game Variety...", options=game_opts, custom_id="game_select")
        self.game_select.callback = self.on_game_select
        self.add_item(self.game_select)
        
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
        if self.gen != _cog_generation:
            return await interaction.response.send_message(embed=make_embed("Bot đang khởi động lại, thử lại sau nhé!", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)
        self.selected_time = interaction.data["values"][0]
        self.start_btn.disabled = not (self.selected_time and self.selected_games)
        await interaction.response.edit_message(embed=self._build_embed(interaction), view=self)
        
    async def on_game_select(self, interaction: discord.Interaction):
        if self.gen != _cog_generation:
            return await interaction.response.send_message(embed=make_embed("Bot đang khởi động lại, thử lại sau nhé!", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)
        self.selected_games = interaction.data["values"][0]
        self.start_btn.disabled = not (self.selected_time and self.selected_games)
        await interaction.response.edit_message(embed=self._build_embed(interaction), view=self)
        
    async def on_start(self, interaction: discord.Interaction):
        if self.gen != _cog_generation:
            return await interaction.response.send_message(embed=make_embed("Bot đang khởi động lại, thử lại sau nhé!", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)
        await interaction.response.send_modal(BadgeModal(self.cog, self.selected_time, self.selected_games, self.gen))


# ==========================================
# MAIN COG CLASS
# ==========================================

class BadgeCog(commands.Cog, name="Độc Quyền"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        global _is_shutting_down, _running_count, _cog_generation
        _is_shutting_down = False
        _running_count = 0
        _cog_generation += 1
        
        active_quest_tasks.clear()
        active_tasks.clear()
        _cancel_requests.clear()
        
        await get_build_number()
        log(f"Đã khởi động module Badge/Quest ngầm (Gen {_cog_generation})!", "ok")

    async def cog_unload(self):
        global _is_shutting_down
        _is_shutting_down = True
        
        tasks = []
        for session in list(active_quest_tasks.values()) + list(active_tasks.values()):
            if session.task is not None:
                session.task.cancel()
                tasks.append(session.task)
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            
        active_quest_tasks.clear()
        active_tasks.clear()
        _cancel_requests.clear()

    # --- Lệnh /quest ---
    @app_commands.command(name="quest", description="Nhờ Joker cày quest giúp cậu")
    async def quest_command(self, interaction: discord.Interaction):
        desc = "Phi vụ mới à?\n\nNhập token xong là Joker sẽ nhận việc cày hết quest cho cậu.\nXong việc Joker sẽ báo và tự rút lui, không lo treo kênh."
        await interaction.response.send_message(embed=local_make_embed(desc, title="Hệ Thống Tự Động Cày Quest", gif=True, guild=interaction.guild), view=QuestToSView(self.bot), ephemeral=True)

    # --- Lệnh /hypesquad ---
    @app_commands.command(name="hypesquad", description="Gán, đổi hoặc xoá huy hiệu HypeSquad")
    @app_commands.describe(house="Chọn nhà HypeSquad bạn muốn nhận")
    @app_commands.choices(house=[Choice(name="Bravery (Dũng Cảm)", value="bravery"), Choice(name="Brilliance (Kiệt Xuất)", value="brilliance"), Choice(name="Balance (Cân Bằng)", value="balance"), Choice(name="Gỡ HypeSquad", value="leave")])
    async def hypesquad_command(self, interaction: discord.Interaction, house: Choice[str]):
        desc = f"Cậu muốn nhận nhà **HypeSquad {house.name}** đúng không?\n\nBấm nút bên dưới để mở bảng nhập Token."
        await interaction.response.send_message(embed=local_make_embed(desc, title="Đổi Nhà HypeSquad", gif=True, guild=interaction.guild), view=HypeSquadView(house.value), ephemeral=True)

    # --- Lệnh /badge ---
    @app_commands.command(name="badge", description="Buff thời gian chơi và số game đã chơi lên Discord")
    async def badge_cmd(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        err_msg = None

        async with _running_lock:
            if _is_shutting_down:
                err_msg = "Bot đang khởi động lại, thử lại sau nhé!"
            elif user_id in active_tasks:
                err_msg = "Cậu đang có 1 phiên chạy rồi, đợi xong đã."
            elif _running_count >= MAX_CONCURRENT:
                err_msg = f"Bot đang chạy tối đa ({MAX_CONCURRENT} người). Thử lại sau vài phút."

        if err_msg:
            return await interaction.response.send_message(embed=make_embed(err_msg, color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)

        view = MilestoneView(self)
        embed = view._build_embed(interaction)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @staticmethod
    def _cooldown_remaining(user_id: int) -> int:
        last = _last_used.get(user_id, 0)
        elapsed = time.time() - last
        return max(0, int(COOLDOWN_SECONDS - elapsed)) if elapsed < COOLDOWN_SECONDS else 0

    async def begin(self, interaction: discord.Interaction, token: str, fingerprint: str, time_key: str, game_key: str, gen: int):
        user_id = interaction.user.id
        time_cfg = TIME_MILESTONES.get(time_key)
        game_cfg = GAME_MILESTONES.get(game_key)
        
        if not time_cfg or not game_cfg:
            return await interaction.followup.send(embed=make_embed("Mốc không hợp lệ, thử lại.", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)
        if not _is_likely_token(token):
            return await interaction.followup.send(embed=make_embed("Token trông không đúng format Discord.", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)
        if fingerprint and not _is_likely_fingerprint(fingerprint):
            return await interaction.followup.send(embed=make_embed("Fingerprint trông không đúng (phải là base64, 100-300 ký tự).\nBỏ trống nếu không có.", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)

        global _running_count
        res_id = str(uuid.uuid4())
        err_msg = None
        current_task = asyncio.current_task()

        async with _running_lock:
            if gen != _cog_generation or _is_shutting_down:
                err_msg = "Bot đang khởi động lại."
            else:
                remaining = self._cooldown_remaining(user_id)
                if remaining > 0:
                    err_msg = f"Còn **{remaining}s** nữa mới được dùng lại."
                elif user_id in active_tasks:
                    err_msg = "Cậu đang có phiên chạy rồi."
                elif _running_count >= MAX_CONCURRENT:
                    err_msg = "Bot đang full slot."
                else:
                    _running_count += 1
                    # SỬA LỖI 3: is_initializing=True khi bắt đầu
                    active_tasks[user_id] = QuestSession(res_id, current_task, is_initializing=True)

        if err_msg:
            return await interaction.followup.send(embed=make_embed(err_msg, color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)

        task_created = False
        try:
            total_hours = float(time_cfg["hours"])
            games = int(game_cfg["games"])
            hours_per_game = total_hours / games

            progress = BadgeProgress(interaction, hours_per_game, games)
            await progress.start()

            async with _running_lock:
                if gen != _cog_generation or _is_shutting_down:
                    raise RuntimeError("Bot đang khởi động lại.")
                
                task = asyncio.create_task(
                    self._run(interaction, token=token, fingerprint=fingerprint, games=games, hours=hours_per_game, progress=progress, res_id=res_id),
                    name=f"badge_{user_id}",
                )
                session = active_tasks.get(user_id)
                if session and session.res_id == res_id:
                    session.task = task
                    session.is_initializing = False # SỬA LỖI 3: Đổi thành False khi task được tạo
                task_created = True
                
        except Exception as e:
            log(f"[Badge] Lỗi khởi tạo tiến trình: {e}", "error")
            try:
                await interaction.followup.send(embed=make_embed("Không thể khởi tạo tiến trình, thử lại sau.", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)
            except: pass
        finally:
            if not task_created:
                async with _running_lock:
                    current = active_tasks.get(user_id)
                    if current and current.res_id == res_id:
                        active_tasks.pop(user_id, None)
                        _running_count = max(0, _running_count - 1)

    async def _run(self, interaction: discord.Interaction, *, token: str, fingerprint: str,
                   games: int, hours: float, progress: BadgeProgress, res_id: str):
        global _running_count
        user_id = interaction.user.id
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
                
                test_game = selected[0]
                test_events = spoofer.build_events(test_game, duration_ms)
                test_status = await spoofer.post(test_events)
                
                if test_status == 204:
                    progress.inc(1)
                    await progress.update()
                    
                    _last_used[user_id] = time.time()
                    _save_usage()
                    
                    remaining_games = selected[1:]
                elif test_status in (401, 403):
                    progress.set("error", f"Auth bị từ chối (mã {test_status}) — token sai hoặc hết hạn")
                    await progress.update()
                    return
                else:
                    progress.set("error", f"Endpoint check thất bại (mã {test_status})")
                    await progress.update()
                    return

                for start_idx in range(0, len(remaining_games), BATCH_SIZE):
                    chunk = remaining_games[start_idx:start_idx + BATCH_SIZE]
                    events: list[dict] = []
                    for g in chunk:
                        events.extend(spoofer.build_events(g, duration_ms))
                    
                    success = False
                    for attempt in range(3):
                        status = await spoofer.post(events)
                        
                        if status == 204:
                            success = True
                            break
                        elif status in (401, 403):
                            progress.set("error", f"Auth bị từ chối giữa chừng (mã {status})")
                            await progress.update()
                            return
                        else:
                            log(f"[Badge] Batch {start_idx//BATCH_SIZE + 1} lỗi HTTP {status}, thử lại lần {attempt+1}/3...", "warn")
                            await asyncio.sleep(3)
                    
                    if success:
                        progress.inc(len(chunk))
                    else:
                        progress.inc_fail(len(chunk))
                        log(f"[Badge] Batch {start_idx//BATCH_SIZE + 1} thất bại hoàn toàn.", "error")
                    
                    await progress.update()
                    await asyncio.sleep(BATCH_DELAY)

            if progress.failed > 0:
                progress.set("error", f"Có {progress.failed} game không thể gửi (lỗi Discord).")
            else:
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
            async with _running_lock:
                current = active_tasks.get(user_id)
                if current and current.res_id == res_id:
                    active_tasks.pop(user_id, None)
                    _running_count = max(0, _running_count - 1)


async def setup(bot: commands.Bot):
    await bot.add_cog(BadgeCog(bot))