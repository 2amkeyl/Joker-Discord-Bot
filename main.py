import asyncio
import os

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

from core.common import log, set_footer_guild, err_embed

from cogs.badge_cog import BadgeCog
from cogs.badge_spoofer_cog import BadgeSpooferCog
from cogs.utility_cog import UtilityCog
from cogs.valorant_cog import ValorantCog

load_dotenv()

async def _cooldown_countdown_warning(interaction: discord.Interaction, seconds: int):
    def _text(n: int) -> str:
        return f"{interaction.user.mention} bình tĩnh nào, đợi thêm **{n} giây** nữa rồi hãy dùng lại lệnh này."
    try:
        await interaction.response.send_message(_text(seconds))
    except discord.HTTPException: return

    remaining = seconds
    while remaining > 1:
        await asyncio.sleep(1)
        remaining -= 1
        try: await interaction.edit_original_response(content=_text(remaining))
        except discord.HTTPException: return
    await asyncio.sleep(1)
    try: await interaction.delete_original_response()
    except discord.HTTPException: pass

class JokerCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        set_footer_guild(interaction.guild)
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            seconds = max(1, round(error.retry_after))
            asyncio.create_task(_cooldown_countdown_warning(interaction, seconds))
            return

        cmd_name = interaction.command.qualified_name if interaction.command else "?"

        original = getattr(error, "original", error)
        if isinstance(original, discord.NotFound) and original.code == 10062:
            log(f"Interaction của lệnh '/{cmd_name}' đã hết hạn trước khi Joker kịp phản hồi (mạng chậm hoặc bot bị treo tạm), bỏ qua.", "warn")
            return

        log(f"Lỗi không xử lý được ở lệnh '/{cmd_name}': {error}", "error")

        embed = err_embed(
            "Kế hoạch gặp trục trặc, lệnh này bị lỗi. Thử lại sau nhé.",
            interaction.guild,
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, tree_cls=JokerCommandTree)

    async def setup_hook(self):
        await self.add_cog(BadgeCog(self))
        await self.add_cog(BadgeSpooferCog(self))
        await self.add_cog(UtilityCog(self))
        await self.add_cog(ValorantCog(self))

        await self.tree.sync()
        log("Đã đồng bộ toàn bộ lệnh của Joker!", "ok")

bot = MyBot()

@bot.event
async def on_ready():
    activity = discord.Activity(type=discord.ActivityType.listening, name="/help | Phantom Thieves of Hearts")
    await bot.change_presence(status=discord.Status.online, activity=activity)
    log(f"Joker vào cuộc! Bot {bot.user} sẵn sàng ra tay!", "ok")

async def run_bot():
    token = os.getenv('BOT_TOKEN')
    if not token:
        return log("BOT_TOKEN is missing", "error")
    try:
        await bot.start(token)
    except Exception as e:
        log(f"Discord error: {e}", "error")

if __name__ == "__main__":
    if os.name == 'nt': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_bot())