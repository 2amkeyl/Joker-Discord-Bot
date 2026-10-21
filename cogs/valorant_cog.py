import asyncio
import io
import time
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.common import EMBED_COLOR, make_embed, log, err_embed
from core.skin_card import build_card_data, render_card
from core.riot import (
    MANUAL_LOGIN_URL, VP_CURRENCY, Assets, NotLoggedIn, RiotError, SessionExpired, SkinInfo, ValorantService,
    login_with_tokens, parse_pasted_tokens,
)
from stores.valorant_store import valorant_store

VP = "<:VP:1560259333269491752>"

TIER_EMOJI = {
    "Select": "<:SE:1560264300335988756>",
    "Deluxe": "<:DE:1560264355604602890>",
    "Premium": "<:PE:1560264388928077894>",
    "Exclusive": "<:EE:1560264482352275506>",
    "Ultra": "<:UE:1560264531501125692>",
}

@dataclass
class SkinOpts:
    cot: Optional[int] = None
    loc: str = "all"
    an_so_du: bool = False
    hien_ten: bool = False


LOGOUT_URL = "https://auth.riotgames.com/logout"
LOGIN_PROMPT_TTL = 300

TIER_COLOR: dict = {}


def _tier_emoji(tier_name: str) -> str:
    name = (tier_name or "").lower()
    for key, emoji in TIER_EMOJI.items():
        if emoji and name.startswith(key.lower()):
            return emoji
    return ""


def _vp(cost: dict) -> int:
    if VP_CURRENCY in cost:
        return int(cost[VP_CURRENCY])
    return int(next(iter(cost.values()), 0))


def _skin_embed(assets: Assets, item_id: str, price_text: str) -> discord.Embed:
    info = assets.skins.get(item_id) or SkinInfo(name="Skin chưa rõ tên", icon=None, tier=None)
    tier = assets.tiers.get(info.tier) if info.tier else None
    tier_name = tier.name if tier else ""
    color = TIER_COLOR.get(tier_name, tier.color if tier and tier.color is not None else EMBED_COLOR)
    embed = discord.Embed(description=price_text, color=color)
    emoji = _tier_emoji(tier_name)
    if emoji:
        embed.title = f"{emoji} {info.name}"
    else:
        embed.set_author(name=info.name, icon_url=tier.icon if tier and tier.icon else None)
    if info.icon:
        embed.set_thumbnail(url=info.icon)
    return embed


def build_daily(name: str, sf: dict, assets: Assets, guild) -> list[discord.Embed]:
    layout = sf.get("SkinsPanelLayout") or {}
    prices = {}
    for o in layout.get("SingleItemStoreOffers", []):
        price = _vp(o.get("Cost") or {})
        prices[o.get("OfferID")] = price
        for rw in o.get("Rewards", []):
            prices.setdefault(rw.get("ItemID"), price)
    ends = int(time.time()) + int(layout.get("SingleItemOffersRemainingDurationInSeconds", 0))
    embeds = [make_embed(f"Cửa hàng ngày hôm nay của **{name}**\n(làm mới <t:{ends}:R>)", guild=guild)]
    for item_id in layout.get("SingleItemOffers", []):
        price = prices.get(item_id)
        embeds.append(_skin_embed(assets, item_id, f"**{price}** {VP}" if price is not None else f"**?** {VP}"))
    return embeds


def build_night(name: str, sf: dict, assets: Assets, guild) -> Optional[list[discord.Embed]]:
    bonus = sf.get("BonusStore")
    if not bonus or not bonus.get("BonusStoreOffers"):
        return None
    ends = int(time.time()) + int(bonus.get("BonusStoreRemainingDurationInSeconds", 0))
    embeds = [make_embed(f"Night Market của **{name}**\n(kết thúc <t:{ends}:R>)", guild=guild)]
    for o in bonus["BonusStoreOffers"]:
        offer = o.get("Offer") or {}
        rewards = offer.get("Rewards") or [{}]
        item_id = rewards[0].get("ItemID", "")
        original = _vp(offer.get("Cost") or {})
        sale = _vp(o.get("DiscountCosts") or {})
        pct = int(o.get("DiscountPercent", 0))
        embeds.append(_skin_embed(assets, item_id, f"~~{original}~~ → **{sale}** {VP}  (-{pct}%)"))
    return embeds


class OwnerView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.message: Optional[discord.Message] = None
        self.interaction: Optional[discord.Interaction] = None

    async def dismiss(self):
        self.stop()
        await self._delete_ephemeral()

    async def _delete_ephemeral(self):
        if self.interaction is not None:
            try: await self.interaction.delete_original_response()
            except discord.HTTPException: pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=err_embed("Nút này của người khác rồi, cậu tự gõ lệnh nhé!", interaction.guild), ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.interaction is not None:
            return await self._delete_ephemeral()
        if self.message is not None:
            try: await self.message.edit(view=None)
            except discord.HTTPException: pass


class LoginView(OwnerView):
    def __init__(self, cog: "ValorantCog", kind: str, owner_id: int):
        super().__init__(owner_id, timeout=LOGIN_PROMPT_TTL)
        self.cog, self.kind = cog, kind
        self.add_item(discord.ui.Button(label="Đăng xuất Riot", style=discord.ButtonStyle.link, url=LOGOUT_URL))
        self.add_item(discord.ui.Button(label="Mở trang đăng nhập Riot", style=discord.ButtonStyle.link, url=MANUAL_LOGIN_URL))
        paste = discord.ui.Button(label="Dán link", style=discord.ButtonStyle.primary)
        paste.callback = self._paste
        self.add_item(paste)

    async def _paste(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LinkModal(self.cog, self.kind, self))


class AccountView(OwnerView):
    def __init__(self, cog: "ValorantCog", owner_id: int):
        super().__init__(owner_id)
        self.cog = cog

    @discord.ui.button(label="Đăng xuất", style=discord.ButtonStyle.danger)
    async def logout(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.logout(interaction)


class _SafeModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log(f"Lỗi modal Valorant: {type(error).__name__}", "error")
        embed = err_embed("Có trục trặc, thử lại sau nhé.", interaction.guild)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass


class LinkModal(_SafeModal, title="Dán link đăng nhập Riot"):
    text = discord.ui.TextInput(label="Link sau khi đăng nhập", style=discord.TextStyle.paragraph,
                                min_length=20, max_length=4000)

    def __init__(self, cog: "ValorantCog", kind: str, login_view: Optional[LoginView] = None):
        super().__init__()
        self.cog, self.kind, self.login_view = cog, kind, login_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.begin_link_login(interaction, self.text.value, self.kind, self.login_view)


class ValorantCog(commands.Cog, name="Valorant"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.service = ValorantService(valorant_store)
        self._skin_opts: dict[str, SkinOpts] = {}
        self._render_sem = asyncio.Semaphore(2)

    async def cog_unload(self):
        await self.service.close()

    @app_commands.command(name="shop", description="Xem cửa hàng skin Valorant hôm nay của cậu")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def shop(self, interaction: discord.Interaction):
        await self._run(interaction, "daily")

    @app_commands.command(name="nightmarket", description="Xem Night Market Valorant của cậu")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def nightmarket(self, interaction: discord.Interaction):
        await self._run(interaction, "night")

    @app_commands.command(name="skins", description="Xuất ảnh kho skin Valorant của cậu")
    @app_commands.describe(cot="Số cột (3-10), bỏ trống để Joker tự chọn cho dễ xem trên điện thoại",
                           loc="Chỉ hiện skin từ bậc nào trở lên",
                           an_so_du="Ẩn level, VP, RP, KC trong ảnh",
                           hien_ten="Hiện tên skin dưới mỗi ô")
    @app_commands.choices(loc=[app_commands.Choice(name="Tất cả", value="all"),
                               app_commands.Choice(name="Premium trở lên", value="premium"),
                               app_commands.Choice(name="Exclusive trở lên", value="exclusive"),
                               app_commands.Choice(name="Chỉ Ultra", value="ultra")])
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: i.user.id)
    async def skins(self, interaction: discord.Interaction, cot: Optional[app_commands.Range[int, 3, 10]] = None,
                    loc: Optional[app_commands.Choice[str]] = None, an_so_du: bool = False, hien_ten: bool = False):
        self._skin_opts[str(interaction.user.id)] = SkinOpts(cot, loc.value if loc else "all", an_so_du, hien_ten)
        await self._run(interaction, "skins")

    async def _run(self, interaction: discord.Interaction, kind: str):
        if not valorant_store.available():
            return await interaction.response.send_message(
                embed=err_embed("Tính năng này cần thư viện `cryptography` nhưng máy chủ của Joker chưa cài, nhờ chủ bot cài giúp nhé!",
                           interaction.guild), ephemeral=True)
        uid = str(interaction.user.id)
        if not valorant_store.has(uid):
            view = LoginView(self, kind, interaction.user.id)
            await interaction.response.send_message(embed=self._login_embed(interaction.guild), view=view, ephemeral=True)
            view.interaction = interaction
            return
        await interaction.response.defer(ephemeral=True)
        await self._deliver(interaction, kind)

    def _login_embed(self, guild, *, expired: bool = False) -> discord.Embed:
        head = ("Phiên đăng nhập Riot của cậu hết hạn rồi (link chỉ sống khoảng 1 giờ), cậu lấy link mới để Joker xem shop tiếp."
                if expired else "Để xem cửa hàng, Joker cần cậu đăng nhập Riot trước.")
        body = (
            f"{head}\n\n"
            "**1.** (Nếu trình duyệt đang đăng nhập nhầm acc) bấm **Đăng xuất Riot**, thấy báo \"Bạn đã được đăng xuất\" thì đóng tab đó.\n"
            "**2.** Bấm **Mở trang đăng nhập Riot** và đăng nhập như bình thường (đây là trang chính thức của Riot, Joker không thấy mật khẩu).\n"
            "**3.** Đăng nhập xong trình duyệt sẽ nhảy sang trang `playvalorant.com`, trang này báo lỗi hoặc trống là **bình thường**.\n"
            "**4.** Chạm vào thanh địa chỉ, **copy toàn bộ địa chỉ** (rất dài, có chữ `#access_token=`), rồi bấm **Dán link** ở đây.\n\n"
            "• Link cho phép xem tài khoản của cậu trong khoảng 1 giờ, **chỉ dán vào form của Joker**, đừng gửi cho ai khác. Joker chỉ **xem** cửa hàng, không mua gì hết.\n"
            "• Nếu địa chỉ mất phần `#access_token=`, thử mở trang đăng nhập bằng tab ẩn danh hoặc trình duyệt khác.\n"
            f"• Tin nhắn này tự xoá sau {LOGIN_PROMPT_TTL // 60} phút nếu cậu không làm gì."
        )
        return make_embed(body, title="Đăng nhập Riot", guild=guild)

    async def _ask_login(self, interaction: discord.Interaction, kind: str, e: Exception):
        uid = str(interaction.user.id)
        valorant_store.remove(uid)
        self.service.forget(uid)
        view = LoginView(self, kind, interaction.user.id)
        await interaction.followup.send(
            embed=self._login_embed(interaction.guild, expired=isinstance(e, SessionExpired)), view=view, ephemeral=True)
        view.interaction = interaction

    async def _deliver_skins(self, interaction: discord.Interaction):
        uid, guild = str(interaction.user.id), interaction.guild
        opts = self._skin_opts.pop(uid, None) or SkinOpts()
        try:
            acct, inv = await self.service.inventory(uid)
            assets = await self.service.assets()
            async with self._render_sem:
                card = await build_card_data(self.service, acct, inv, assets, tier_filter=opts.loc,
                                             show_wallet=not opts.an_so_du, show_names=opts.hien_ten,
                                             avatar_url=interaction.user.display_avatar.replace(size=256, format="png").url)
                png = await asyncio.to_thread(render_card, card, opts.cot)
        except (NotLoggedIn, SessionExpired) as e:
            return await self._ask_login(interaction, "skins", e)
        except RiotError as e:
            return await interaction.followup.send(embed=err_embed(e.user_message, guild), ephemeral=True)
        except Exception as e:
            log(f"Lỗi vẽ ảnh kho skin: {type(e).__name__}: {e}", "error")
            return await interaction.followup.send(embed=err_embed("Joker vẽ ảnh bị lỗi, thử lại sau nhé.", guild), ephemeral=True)
        if not card.skins:
            return await interaction.followup.send(
                embed=make_embed("Không có skin nào khớp bộ lọc này, thử chọn bậc thấp hơn nhé.", guild=guild), ephemeral=True)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        await interaction.followup.send(file=discord.File(io.BytesIO(png), filename="joker-skins.png"), ephemeral=False)

    async def _deliver(self, interaction: discord.Interaction, kind: str):
        if kind == "skins":
            return await self._deliver_skins(interaction)
        public = True
        uid = str(interaction.user.id)
        guild = interaction.guild
        try:
            acct, sf = await self.service.storefront(uid)
            assets = await self.service.assets()
        except (NotLoggedIn, SessionExpired) as e:
            return await self._ask_login(interaction, kind, e)
        except RiotError as e:
            return await interaction.followup.send(embed=err_embed(e.user_message, guild), ephemeral=True)

        if kind == "night":
            embeds = build_night(acct["name"], sf, assets, guild)
            if embeds is None:
                return await interaction.followup.send(
                    embed=make_embed("Night Market chưa mở, khi nào có Joker sẽ hiện ở đây.", guild=guild), ephemeral=True)
        else:
            embeds = build_daily(acct["name"], sf, assets, guild)

        view = AccountView(self, interaction.user.id)
        if public:
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
        view.message = await interaction.followup.send(embeds=embeds, view=view, ephemeral=not public)

    async def begin_link_login(self, interaction: discord.Interaction, text: str, kind: str,
                               login_view: Optional[LoginView] = None):
        uid = str(interaction.user.id)
        try:
            account, tokens = await login_with_tokens(parse_pasted_tokens(text))
        except RiotError as e:
            return await interaction.followup.send(embed=err_embed(e.user_message, interaction.guild), ephemeral=True)
        valorant_store.set(uid, account)
        self.service.prime(uid, tokens)
        if login_view is not None:
            await login_view.dismiss()
        await self._deliver(interaction, kind)

    async def logout(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        valorant_store.remove(uid)
        self.service.forget(uid)
        await interaction.response.send_message(
            embed=make_embed("Joker đã xoá phiên đăng nhập Riot của cậu. Muốn chắc ăn hơn thì cậu đăng xuất các thiết bị trong cài đặt tài khoản Riot nữa nhé.",
                             guild=interaction.guild), ephemeral=True)
