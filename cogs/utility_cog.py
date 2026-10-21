import discord
from discord.ext import commands
from discord import app_commands

from core.common import make_embed


class UtilityCog(commands.Cog, name="Tiện Ích"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Xem danh sách toàn bộ lệnh của Joker")
    async def help_cmd(self, interaction: discord.Interaction):
        cmds = [c for c in self.bot.tree.get_commands() if isinstance(c, app_commands.Command)]
        lines = [f"**/{c.qualified_name}** - {c.description}" for c in cmds]
        embed = make_embed(
            "\n".join(lines)[:4000],
            title="Danh sách lệnh của Joker",
            guild=interaction.guild,
        )
        embed.set_footer(text=f"{embed.footer.text} • {len(cmds)} lệnh")
        await interaction.response.send_message(embed=embed)
