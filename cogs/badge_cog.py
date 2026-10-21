import discord
from discord.ext import commands
from discord import app_commands
from discord.app_commands import Choice

from core.common import EMBED_COLOR, ERROR_COLOR, make_embed, log, ROOT_DIR

import asyncio
import random
import os
import aiohttp
import re
import time
from datetime import datetime, timezone
from typing import Optional, Tuple, Any
import json
import base64
import traceback

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

active_quest_tasks: dict[int, asyncio.Task] = {}

FALLBACK_BUILD = 504649
BUILD_TTL = 6 * 3600
BUILD_RE = re.compile(r'(?:buildNumber|build_number|BUILD_NUMBER)["\s:=]+["\s]*(\d{5,7})')
_build_cache: dict = {"value": FALLBACK_BUILD, "at": 0.0}

MAX_FAILS = 5
MAX_ATTEMPTS = 3


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
    log(f"[Quest] Build number hiện tại: {value}", "info")
    return value


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
        for attempt in range(3):
            try:
                async with self.session.request(method, url, json=payload, proxy=self.proxy) as r:
                    try:
                        body = await r.json(content_type=None)
                    except Exception:
                        body = await r.text()

                    if r.status == 429:
                        retry_after = body.get("retry_after", 5) if isinstance(body, dict) else 5
                        await asyncio.sleep(float(retry_after) + (1.5 ** attempt) + random.uniform(0, 1))
                        continue
                    return r.status, body
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_err = e
                await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
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
    cfg = quest.get("config") or {}
    msgs = cfg.get("messages") or {}
    name = _get(msgs, "questName", "quest_name") or _get(msgs, "gameTitle", "game_title") or (cfg.get("application") or {}).get("name")
    return name.strip() if name else f"Quest#{quest.get('id', '?')}"

def is_completable(quest: dict) -> bool:
    expires = _get(quest.get("config") or {}, "expiresAt", "expires_at")
    if expires:
        try:
            if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc): return False
        except Exception: pass
    tc = get_task_config(quest)
    tasks = tc.get("tasks") if isinstance(tc, dict) else None
    return any(tasks.get(t) is not None for t in SUPPORTED_TASKS) if isinstance(tasks, dict) else False

def is_enrolled(quest: dict) -> bool: return bool(_get(_user_status(quest), "enrolledAt", "enrolled_at"))
def is_completed(quest: dict) -> bool: return bool(_get(_user_status(quest), "completedAt", "completed_at"))

def get_task_type(quest: dict) -> Optional[str]:
    tc = get_task_config(quest)
    tasks = tc.get("tasks") if isinstance(tc, dict) else None
    if not isinstance(tasks, dict): return None
    for t in SUPPORTED_TASKS:
        if tasks.get(t) is not None: return t
    return None

def get_seconds_needed(quest: dict) -> int:
    t = get_task_type(quest)
    return (get_task_config(quest)["tasks"][t] or {}).get("target", 0) if t else 0

def get_seconds_done(quest: dict) -> float:
    t = get_task_type(quest)
    if not t: return 0
    progress = _user_status(quest).get("progress") or {}
    entry = progress.get(t) or {}
    return entry.get("value", 0) or 0


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
            log(f"[Quest] Lấy danh sách quest lỗi: HTTP {status} {str(data)[:150]}", "warn")
            return []
        if isinstance(data, dict):
            return data.get("quests") or []
        return data if isinstance(data, list) else []

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
        sec_done = get_seconds_done(quest)

        enrolled_at_str = _get(_user_status(quest), "enrolledAt", "enrolled_at")
        enrolled_ts = datetime.fromisoformat(enrolled_at_str.replace("Z", "+00:00")).timestamp() if enrolled_at_str else time.time()

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
        return True

    async def complete_heartbeat(self, quest: dict, task_type: str) -> bool:
        qid, name = quest["id"], get_quest_name(quest)
        sec_needed = get_seconds_needed(quest)
        sec_done = get_seconds_done(quest)

        stream_key = "call:0:1" if task_type == "PLAY_ACTIVITY" else f"call:0:{random.randint(1000, 30000)}"
        deadline = time.time() + max(0, sec_needed - sec_done) * 1.5 + 300
        fails = 0

        while sec_done < sec_needed:
            if time.time() > deadline:
                raise QuestFail("quá thời gian chờ mà tiến độ không tăng")
            status, body = await self.api.post(f"/quests/{qid}/heartbeat", {"stream_key": stream_key, "terminal": False})
            if status == 200 and isinstance(body, dict):
                fails = 0
                pdata = body.get("progress") or {}
                if task_type in pdata:
                    sec_done = (pdata[task_type] or {}).get("value", sec_done)
                    self.live.set_progress(sec_done)
                if body.get("completed_at") or sec_done >= sec_needed:
                    break
            else:
                fails = self._fail(name, status, body, fails)
            await asyncio.sleep(HEARTBEAT_INTERVAL)

        await self.api.post(f"/quests/{qid}/heartbeat", {"stream_key": stream_key, "terminal": True})
        return True

    async def process_quest(self, quest: dict) -> bool:
        task_type = get_task_type(quest)
        if not task_type:
            return False
        log(f"[Quest] {self.user_name} bắt đầu cày: {get_quest_name(quest)} ({task_type})", "info")
        self.live.begin_quest(get_quest_name(quest), task_type, get_seconds_needed(quest), get_seconds_done(quest))
        try:
            if task_type in ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE"):
                return await self.complete_video(quest)
            return await self.complete_heartbeat(quest, task_type)
        finally:
            self.live.end_quest()


async def background_quest_worker(bot: commands.Bot, user: discord.abc.User, api: AsyncDiscordAPI, live: QuestLive):
    completer = AsyncQuestAutocompleter(api, user.name, live)
    attempts: dict = {}
    updater = asyncio.create_task(live_updater(live))

    try:
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

                    live.error = False
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

            except (QuestAbort, asyncio.CancelledError):
                raise
            except Exception as e:
                log(f"[Quest] Lỗi vòng quét (sẽ thử lại): {e}\n{traceback.format_exc()}", "error")
                await asyncio.sleep(5)

    except asyncio.CancelledError:
        live.phase, live.stop_reason, live.error = "stopped", "Cậu đã dừng cày quest ngang chừng.", False
        log(f"[Quest] Đã dừng tiến trình của {user.name}.", "warn")
    except QuestAbort as e:
        live.phase, live.stop_reason, live.error = "stopped", f"Dừng vì lỗi: {e}", True
        log(f"[Quest] Dừng của {user.name}: {e}", "error")
        await live.ping("Hệ thống đã dừng", f"Joker phải dừng cày quest: {e}", error=True)
    finally:
        updater.cancel()
        await live.push(final=True)
        await api.close()
        active_quest_tasks.pop(user.id, None)


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


class QuestTokenModal(discord.ui.Modal, title="Nhập Token Cày Quest"):
    token_input = discord.ui.TextInput(
        label="Token của cậu",
        placeholder="Joker sẽ dùng Token này để cày sạch sành sanh quest...",
        style=discord.TextStyle.short,
        required=True
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        token = self.token_input.value.strip()
        user_id = interaction.user.id

        if user_id in active_quest_tasks:
            return await interaction.response.send_message(embed=local_make_embed("Cậu đang cày quest rồi mà!", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        api = AsyncDiscordAPI(token, await get_build_number(), proxy=random.choice(PROXIES) if PROXIES else None)
        if not await api.validate_token():
            await api.close()
            return await interaction.followup.send(embed=local_make_embed("Token không hợp lệ hoặc đã hết hạn, cậu kiểm tra lại nhé!", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)

        channel = interaction.channel
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(interaction.channel_id)
            except discord.HTTPException:
                channel = None
        if channel is None:
            await api.close()
            return await interaction.followup.send(embed=local_make_embed("Joker không xác định được kênh này, cậu thử lại ở một kênh text bình thường nhé!", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)
        live = QuestLive(interaction.user, channel)
        try:
            live.message = await channel.send(embed=live.build_embed())
        except Exception as e:
            await api.close()
            log(f"[Quest] Không gửi được tin nhắn tiến độ: {e}", "error")
            return await interaction.followup.send(embed=local_make_embed("Joker không gửi được tin nhắn vào kênh này, kiểm tra quyền của Joker nhé!", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)

        active_quest_tasks[user_id] = asyncio.create_task(background_quest_worker(self.bot, interaction.user, api, live))

        msg = "Đã khởi động chế độ cày quest! Joker sẽ tự động ngắt khi xong việc, cậu xem tiến độ trực tiếp ở tin nhắn Joker vừa gửi trong kênh."
        await interaction.followup.send(embed=local_make_embed(msg, title="Cày Quest Thành Công!", gif=True, guild=interaction.guild), ephemeral=True)

class QuestToSView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="Bật Cày Quest", style=discord.ButtonStyle.green, custom_id="accept_btn")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in active_quest_tasks:
            return await interaction.response.send_message(embed=local_make_embed("Cậu đang nằm trong hệ thống làm quest rồi mà, khi nào muốn dừng ngang thì bấm nút đỏ nhé!", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)
        await interaction.response.send_modal(QuestTokenModal(self.bot))

    @discord.ui.button(label="Dừng Cày Ngang Chừng", style=discord.ButtonStyle.red, custom_id="stop_btn")
    async def stop_quest(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        task = active_quest_tasks.pop(user_id, None)
        if task:
            task.cancel()
            await interaction.response.send_message(embed=local_make_embed("Đã ngắt chế độ cày quest cho cậu.", guild=interaction.guild), ephemeral=True)
        else:
            await interaction.response.send_message(embed=local_make_embed("Cậu có đang cày đâu mà đòi dừng!", color=ERROR_COLOR, guild=interaction.guild), ephemeral=True)

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

class BadgeCog(commands.Cog, name="Độc Quyền"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await get_build_number()
        log("Đã khởi động module Badge/Quest ngầm!", "ok")

    async def cog_unload(self):
        for task in active_quest_tasks.values():
            task.cancel()
        active_quest_tasks.clear()

    @app_commands.command(name="quest", description="Nhờ Joker cày quest giúp cậu")
    async def quest_command(self, interaction: discord.Interaction):
        desc = "Phi vụ mới à?\n\nNhập token xong là Joker sẽ nhận việc cày hết quest cho cậu.\nXong việc Joker sẽ báo và tự rút lui, không lo treo kênh."
        await interaction.response.send_message(embed=local_make_embed(desc, title="Hệ Thống Tự Động Cày Quest", gif=True, guild=interaction.guild), view=QuestToSView(self.bot), ephemeral=True)

    @app_commands.command(name="hypesquad", description="Gán, đổi hoặc xoá huy hiệu HypeSquad")
    @app_commands.describe(house="Chọn nhà HypeSquad bạn muốn nhận")
    @app_commands.choices(house=[Choice(name="Bravery (Dũng Cảm)", value="bravery"), Choice(name="Brilliance (Kiệt Xuất)", value="brilliance"), Choice(name="Balance (Cân Bằng)", value="balance"), Choice(name="Gỡ HypeSquad", value="leave")])
    async def hypesquad_command(self, interaction: discord.Interaction, house: Choice[str]):
        desc = f"Cậu muốn nhận nhà **HypeSquad {house.name}** đúng không?\n\nBấm nút bên dưới để mở bảng nhập Token."
        await interaction.response.send_message(embed=local_make_embed(desc, title="Đổi Nhà HypeSquad", gif=True, guild=interaction.guild), view=HypeSquadView(house.value), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(BadgeCog(bot))
