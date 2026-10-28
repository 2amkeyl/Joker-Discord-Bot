import asyncio
import hashlib
import json
import os
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from core.common import (
    ROOT_DIR, aread_json, awrite_json_atomic, err_embed, log, log_exc, setup_logging,
)

# Mỗi cog là một extension có hàm `async def setup(bot)`.
EXTENSIONS: tuple[str, ...] = ("cogs.badge_cog",)

SYNC_STATE_PATH = os.path.join(ROOT_DIR, "data", ".command_sync.json")


async def _respond(interaction: discord.Interaction, **kwargs) -> None:
    """Trả lời interaction, tự chọn response hoặc followup tuỳ trạng thái, nuốt lỗi HTTP."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)
    except discord.HTTPException:
        pass


class JokerCommandTree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            # <t:...:R> để Discord tự đếm ngược phía client, không cần sửa tin nhắn mỗi giây.
            until = int(time.time()) + max(1, round(error.retry_after))
            await _respond(
                interaction,
                content=f"Bình tĩnh nào, dùng lại lệnh này <t:{until}:R> nhé.",
                ephemeral=True,
            )
            return

        cmd_name = interaction.command.qualified_name if interaction.command else "?"
        original = getattr(error, "original", error)

        if isinstance(original, discord.NotFound) and original.code == 10062:
            log(f"Interaction của lệnh '/{cmd_name}' đã hết hạn trước khi Joker kịp phản hồi "
                f"(mạng chậm hoặc bot bị treo tạm), bỏ qua.", "warn")
            return

        if isinstance(error, app_commands.CheckFailure):
            text = "Cậu không dùng được lệnh này ở đây."
        else:
            log_exc(f"Lỗi không xử lý được ở lệnh '/{cmd_name}'", original)
            text = "Kế hoạch gặp trục trặc, lệnh này bị lỗi. Thử lại sau nhé."

        await _respond(interaction, embed=err_embed(text, interaction.guild), ephemeral=True)


class JokerBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            help_command=None,  # bot chỉ dùng slash, tắt help mặc định của prefix command
            intents=discord.Intents.default(),
            tree_cls=JokerCommandTree,
            activity=discord.Activity(type=discord.ActivityType.listening,
                                      name="Phantom Thieves of Hearts"),
            status=discord.Status.online,
        )
        self._ready_logged = False

    async def setup_hook(self):
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        await self._sync_if_changed()

    async def on_ready(self):
        if self._ready_logged:  # on_ready có thể chạy lại mỗi lần reconnect
            return
        self._ready_logged = True
        log(f"Joker vào cuộc! Bot {self.user} sẵn sàng ra tay!", "ok")

    def _commands_signature(self) -> str:
        payload = [c.to_dict(self.tree) for c in self.tree.get_commands()]
        raw = json.dumps([self.application_id, payload], sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    async def _sync_if_changed(self):
        # Chỉ sync khi danh sách lệnh đổi, tránh gọi API mỗi lần khởi động (đặt SYNC_COMMANDS=1 để ép sync).
        sig = self._commands_signature()
        state = await aread_json(SYNC_STATE_PATH, {}, label="trạng thái đồng bộ lệnh")
        old = state.get("signature") if isinstance(state, dict) else None
        if old == sig and os.getenv("SYNC_COMMANDS") != "1":
            log("Lệnh không đổi, bỏ qua bước đồng bộ.", "info")
            return

        try:
            await self.tree.sync()
        except discord.HTTPException as e:
            # Sync lỗi (mạng, rate limit...) không nên làm bot chết; lần khởi động sau sẽ thử lại.
            log(f"Không đồng bộ được lệnh, bot vẫn chạy với lệnh cũ trên Discord: {e}", "warn")
            return

        try:
            await awrite_json_atomic(SYNC_STATE_PATH, {"signature": sig})
        except OSError as e:
            log(f"Không lưu được trạng thái đồng bộ lệnh: {e}", "warn")
        log("Đã đồng bộ toàn bộ lệnh của Joker!", "ok")


async def run_bot() -> int:
    token = os.getenv("BOT_TOKEN")
    if not token:
        log("Thiếu BOT_TOKEN, hãy kiểm tra file .env.", "error")
        return 1

    bot = JokerBot()
    try:
        async with bot:  # đảm bảo bot.close() chạy khi thoát để các cog dọn session
            await bot.start(token)
    except discord.LoginFailure:
        log("BOT_TOKEN không hợp lệ, kiểm tra lại file .env.", "error")
        return 1
    except Exception as e:
        log_exc("Lỗi Discord", e)
        return 1
    return 0


def main() -> int:
    load_dotenv()
    setup_logging()
    try:
        return asyncio.run(run_bot())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
