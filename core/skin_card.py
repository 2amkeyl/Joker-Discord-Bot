from __future__ import annotations

import asyncio
import hashlib
import io
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from core.common import ROOT_DIR, log

FONT_DIR = os.path.join(ROOT_DIR, "assets", "fonts")
CACHE_DIR = os.path.join(ROOT_DIR, "skin_cache")
VN_TZ = timezone(timedelta(hours=7))
_LANCZOS = getattr(Image, "Resampling", Image).LANCZOS

FALLBACK_AVATAR_URL = "https://cdn.pfps.gg/pfps/1759-joker-persona-5-profile-icon.png"

TIER_RANK = {"select": 1, "deluxe": 2, "premium": 3, "exclusive": 4, "ultra": 5}
TIER_DEFAULT_COLOR = {"select": 0x5A9FE2, "deluxe": 0x009587, "premium": 0xD1548D, "exclusive": 0xF5955B, "ultra": 0xFAD663}
TIER_ORDER = ["ultra", "exclusive", "premium", "deluxe", "select"]
FILTER_MIN_RANK = {"all": 0, "premium": 3, "exclusive": 4, "ultra": 5}


def tier_key(name: str) -> str:
    n = (name or "").lower()
    for k in TIER_RANK:
        if n.startswith(k):
            return k
    return ""


@dataclass
class CardSkin:
    name: str
    tier: str
    color: int
    melee: bool = False
    icon: Optional[Image.Image] = None


@dataclass
class CardData:
    name: str
    tag: str
    region: str
    updated: datetime
    skins: list
    tier_counts: dict
    rank_name: str = "UNRANKED"
    rank_icon: Optional[Image.Image] = None
    avatar: Optional[Image.Image] = None
    level: Optional[int] = None
    vp: Optional[int] = None
    rp: Optional[int] = None
    kc: Optional[int] = None
    show_wallet: bool = True
    show_names: bool = False


def _make_thumb(raw: bytes, max_w: int, max_h: int) -> Image.Image:
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    box = im.getchannel("A").getbbox()
    if box:
        im = im.crop(box)
    im.thumbnail((max_w, max_h), _LANCZOS)
    return im


class ImageCache:
    def __init__(self, session_factory, folder: str = CACHE_DIR, concurrency: int = 8):
        self._session_factory = session_factory
        self.folder = folder
        self._sem = asyncio.Semaphore(concurrency)
        os.makedirs(folder, exist_ok=True)

    def _path(self, url: str, kind: str) -> str:
        return os.path.join(self.folder, f"{kind}_{hashlib.sha1(url.encode()).hexdigest()[:20]}.png")

    async def get(self, url: Optional[str], *, kind: str = "skin", max_w: int = 420, max_h: int = 300) -> Optional[Image.Image]:
        if not url:
            return None
        path = self._path(url, kind)
        try:
            if os.path.exists(path):
                return await asyncio.to_thread(lambda: Image.open(path).convert("RGBA"))
            async with self._sem:
                async with self._session_factory().get(url) as r:
                    if r.status != 200:
                        return None
                    raw = await r.read()
            im = await asyncio.to_thread(_make_thumb, raw, max_w, max_h)
            await asyncio.to_thread(im.save, path, "PNG")
            return im
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
            log(f"Không tải được ảnh skin: {type(e).__name__}", "warn")
            return None


async def build_card_data(service, acct: dict, inv: dict, assets, *, tier_filter: str = "all",
                          show_wallet: bool = True, show_names: bool = False,
                          avatar_url: Optional[str] = None) -> CardData:
    owned, seen, picked = inv["owned"], set(), []
    counts = {k: 0 for k in TIER_ORDER}
    colors = {k: TIER_DEFAULT_COLOR[k] for k in TIER_ORDER}
    for uuid in owned:
        info = assets.skins.get(uuid)
        if info is None or not info.tier or id(info) in seen:
            continue
        tier = assets.tiers.get(info.tier)
        key = tier_key(tier.name) if tier else ""
        if not key:
            continue
        seen.add(id(info))
        color = tier.color if tier.color is not None else TIER_DEFAULT_COLOR[key]
        colors[key] = color
        counts[key] += 1
        picked.append((key, color, info))

    min_rank = FILTER_MIN_RANK.get(tier_filter, 0)
    shown = [(k, c, i) for (k, c, i) in picked if TIER_RANK[k] >= min_rank]
    shown.sort(key=lambda t: (-TIER_RANK[t[0]], not t[2].melee, t[2].name.lower()))

    cache = ImageCache(service._plain_http)
    rank_name, rank_url = await service.rank_info(inv.get("rank_tier"))
    icons, rank_icon, avatar = await asyncio.gather(
        asyncio.gather(*(cache.get(i.icon) for _, _, i in shown)),
        cache.get(rank_url, kind="rank", max_w=128, max_h=128),
        cache.get(avatar_url, kind="avatar", max_w=256, max_h=256),
    )
    if avatar is None:
        avatar = await cache.get(FALLBACK_AVATAR_URL, kind="avatar", max_w=256, max_h=256)
    name, _, tag = (acct.get("name") or "Người chơi Valorant").partition("#")
    return CardData(
        name=name, tag=tag, region=(acct.get("shard") or "").upper(), updated=datetime.now(VN_TZ),
        skins=[CardSkin(i.name, k, c, i.melee, ic) for (k, c, i), ic in zip(shown, icons)],
        tier_counts={k: (counts[k], colors[k]) for k in TIER_ORDER},
        rank_name=rank_name, rank_icon=rank_icon, avatar=avatar,
        level=inv.get("level"), vp=inv.get("vp"), rp=inv.get("rp"), kc=inv.get("kc"),
        show_wallet=show_wallet, show_names=show_names,
    )


BASE = (11, 11, 13)
BASE2 = (8, 8, 10)
M, G, MIN_W = 44, 14, 1600
PROFILE_H, STATS_H = 84, 104
HEAD_H = PROFILE_H + 26 + STATS_H + 36
FOOT_H = 70

_FONT_FILES = {"r": "BeVietnamPro-Regular.ttf", "s": "BeVietnamPro-SemiBold.ttf",
               "b": "BeVietnamPro-Bold.ttf", "x": "BeVietnamPro-ExtraBold.ttf"}


@lru_cache(maxsize=64)
def _font(weight: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, _FONT_FILES[weight]), size)
    except OSError:
        log("Thiếu font trong assets/fonts, ảnh sẽ dùng font mặc định (mất dấu tiếng Việt).", "warn")
        try:
            return ImageFont.load_default(size)
        except TypeError:
            return ImageFont.load_default()


def _rgb(c: int) -> tuple:
    return (c >> 16) & 255, (c >> 8) & 255, c & 255


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


_masks: dict = {}


def _mask(w: int, h: int, r: int, ss: int = 4) -> Image.Image:
    key = (w, h, r)
    m = _masks.get(key)
    if m is None:
        big = Image.new("L", (w * ss, h * ss), 0)
        ImageDraw.Draw(big).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=r * ss, fill=255)
        m = big.resize((w, h), _LANCZOS)
        if len(_masks) > 64:
            _masks.clear()
        _masks[key] = m
    return m


def _vgrad(w: int, h: int, top: tuple, bottom: tuple) -> Image.Image:
    g = Image.linear_gradient("L").resize((w, h))
    return Image.composite(Image.new("RGB", (w, h), bottom), Image.new("RGB", (w, h), top), g)


def _panel(canvas: Image.Image, x: int, y: int, w: int, h: int, r: int, fill, outline=None, ow: int = 1):
    def put(f, xx, yy, ww, hh, rr):
        m = _mask(ww, hh, max(rr, 1))
        if isinstance(f, tuple):
            canvas.paste(f, (xx, yy, xx + ww, yy + hh), m)
        else:
            canvas.paste(f.resize((ww, hh)) if f.size != (ww, hh) else f, (xx, yy), m)
    if outline is not None:
        put(outline, x, y, w, h, r)
        put(fill, x + ow, y + ow, w - 2 * ow, h - 2 * ow, r - ow)
    else:
        put(fill, x, y, w, h, r)


def _circle(canvas: Image.Image, cx: int, cy: int, d: int, fill, outline=None, ow: int = 2):
    ss = 4
    big = Image.new("L", (d * ss, d * ss), 0)
    ImageDraw.Draw(big).ellipse((0, 0, d * ss - 1, d * ss - 1), fill=255)
    m = big.resize((d, d), _LANCZOS)
    x, y = cx - d // 2, cy - d // 2
    if outline is not None:
        canvas.paste(outline, (x, y, x + d, y + d), m)
        inner = d - 2 * ow
        big2 = Image.new("L", (inner * ss, inner * ss), 0)
        ImageDraw.Draw(big2).ellipse((0, 0, inner * ss - 1, inner * ss - 1), fill=255)
        canvas.paste(fill, (x + ow, y + ow, x + ow + inner, y + ow + inner), big2.resize((inner, inner), _LANCZOS))
    else:
        canvas.paste(fill, (x, y, x + d, y + d), m)


def _tw(font, s: str) -> float:
    return font.getlength(s)


def _spaced_w(font, s: str, sp: float) -> float:
    return sum(_tw(font, ch) for ch in s) + sp * max(len(s) - 1, 0)


def _spaced(d: ImageDraw.ImageDraw, x: float, y: float, s: str, font, fill, sp: float, anchor: str = "lm") -> float:
    for ch in s:
        d.text((x, y), ch, font=font, fill=fill, anchor=anchor)
        x += _tw(font, ch) + sp
    return x


def _fit(font, s: str, maxw: float) -> str:
    if _tw(font, s) <= maxw:
        return s
    while s and _tw(font, s + "…") > maxw:
        s = s[:-1]
    return s.rstrip() + "…"


def _num(n: Optional[int]) -> str:
    return "—" if n is None else f"{n:,}".replace(",", ".")


def _dims(n_cells: int, cols: int):
    cw = max(210, (MIN_W - 2 * M - (cols - 1) * G) // cols)
    ch = int(cw * 0.72)
    rows = math.ceil(n_cells / cols)
    W = 2 * M + cols * cw + (cols - 1) * G
    H = M + HEAD_H + rows * ch + (rows - 1) * G + FOOT_H
    return W, H, cw, ch, rows


def auto_columns(n_cells: int, ratio: float = 4 / 5) -> int:
    best, best_diff = 3, 1e9
    for c in range(3, 11):
        W, H, *_ = _dims(n_cells, c)
        diff = abs(W / H - ratio)
        if diff < best_diff:
            best, best_diff = c, diff
    return best


def _logo(canvas, x, y, size, r=14):
    tile = _vgrad(size, size, (230, 0, 18), (110, 0, 10))
    _panel(canvas, x, y, size, size, r, tile, outline=(255, 90, 90), ow=1)
    ImageDraw.Draw(canvas).text((x + size / 2, y + size / 2 + 1), "J", font=_font("x", int(size * 0.58)),
                                fill=(255, 255, 255), anchor="mm")


def _profile(canvas, x, y, data: CardData, content_w: int):
    size = PROFILE_H
    if data.avatar is not None:
        av = data.avatar.copy()
        sc_ = max(size / av.width, size / av.height)
        av = av.resize((max(size, int(av.width * sc_)), max(size, int(av.height * sc_))), _LANCZOS)
        left, top = (av.width - size) // 2, (av.height - size) // 2
        av = av.crop((left, top, left + size, top + size))
        tile = Image.new("RGB", (size, size), (32, 10, 12))
        tile.paste(av, (0, 0), av)
        _panel(canvas, x, y, size, size, 18, tile, outline=(200, 20, 30), ow=2)
    else:
        _logo(canvas, x, y, size, r=18)
    d = ImageDraw.Draw(canvas)
    tx = x + size + 26
    nf, tf = _font("x", 42), _font("b", 27)
    maxw = content_w - size - 40
    name = _fit(nf, data.name, maxw - (_tw(tf, f" #{data.tag}") if data.tag else 0))
    cy = y + size // 2
    d.text((tx, cy), name, font=nf, fill=(255, 255, 255), anchor="lm")
    if data.tag:
        d.text((tx + _tw(nf, name) + 8, cy + 5), f"#{data.tag}", font=tf, fill=(110, 110, 120), anchor="lm")


def _icon_circle(canvas, cx, cy, text, color, d=40):
    _circle(canvas, cx, cy, d, (26, 26, 30), outline=(52, 52, 60), ow=2)
    ImageDraw.Draw(canvas).text((cx, cy), text, font=_font("x", 13 if len(text) > 1 else 16), fill=color, anchor="mm")


def _stat_box(canvas, x, y, h, label, value, *, icon=None, glyph=None, glyph_color=(200, 200, 210),
              value_color=(255, 255, 255), draw=True) -> int:
    lf, vf = _font("s", 12), _font("b", 20)
    text_w = max(_spaced_w(lf, label, 2), _tw(vf, value))
    pad, ic = 16, 40
    w = int(pad + ic + 12 + text_w + pad)
    if not draw:
        return w
    _panel(canvas, x, y, w, h, 20, (20, 20, 23), outline=(36, 36, 42))
    cx, cy = x + pad + ic // 2, y + h // 2
    if icon is not None:
        im = icon.copy()
        im.thumbnail((ic, ic), _LANCZOS)
        canvas.paste(im, (cx - im.width // 2, cy - im.height // 2), im)
    elif glyph is not None:
        _icon_circle(canvas, cx, cy, glyph, glyph_color, ic)
    d = ImageDraw.Draw(canvas)
    tx = x + pad + ic + 12
    _spaced(d, tx, y + h // 2 - 13, label, lf, (125, 125, 135), 2)
    d.text((tx, y + h // 2 + 11), value, font=vf, fill=value_color, anchor="lm")
    return w


def _tier_pill(canvas, x, y, h, color, count, draw=True) -> int:
    f = _font("b", 19)
    s = str(count)
    w = int(14 + 18 + 8 + _tw(f, s) + 14)
    if not draw:
        return w
    c = _rgb(color)
    _panel(canvas, x, y, w, h, h // 2, _mix(BASE, c, 0.14), outline=_mix(BASE, c, 0.5))
    d = ImageDraw.Draw(canvas)
    cx, cy, r = x + 14 + 9, y + h // 2, 8
    d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], outline=c, width=2)
    d.polygon([(cx, cy - 3), (cx + 3, cy), (cx, cy + 3), (cx - 3, cy)], fill=c)
    d.text((x + 14 + 18 + 8, cy), s, font=f, fill=c, anchor="lm")
    return w


def _stats_bar(canvas, x, y, w, h, data: CardData, n_shown: int):
    _panel(canvas, x, y, w, h, 30, (13, 13, 15), outline=(34, 34, 40), ow=1)
    bh = 64
    by = y + (h - bh) // 2
    cx = x + 22
    cx += _stat_box(canvas, cx, by, bh, "RANK", data.rank_name, icon=data.rank_icon, glyph="R", draw=True) + 14
    lf, vf = _font("s", 12), _font("b", 20)
    value = f"{n_shown} Skins"
    pills_w = sum(_tier_pill(canvas, 0, 0, 38, col, cnt, draw=False) + 8 for cnt, col in data.tier_counts.values()) - 8
    head_w = 16 + 40 + 12 + int(max(_spaced_w(lf, "SKINS", 2), _tw(vf, value))) + 16
    box_w = head_w + 1 + 18 + pills_w + 18
    _panel(canvas, cx, by, box_w, bh, 20, (20, 20, 23), outline=(36, 36, 42))
    d = ImageDraw.Draw(canvas)
    tcx, tcy = cx + 16 + 20, by + bh // 2
    _circle(canvas, tcx, tcy, 40, (26, 26, 30), outline=(52, 52, 60), ow=2)
    d.polygon([(tcx - 10, tcy + 8), (tcx, tcy - 10), (tcx + 10, tcy + 8), (tcx + 4, tcy + 8), (tcx, tcy - 1), (tcx - 4, tcy + 8)], fill=(255, 86, 96))
    _spaced(d, cx + 16 + 40 + 12, by + bh // 2 - 13, "SKINS", lf, (125, 125, 135), 2)
    d.text((cx + 16 + 40 + 12, by + bh // 2 + 11), value, font=vf, fill=(255, 255, 255), anchor="lm")
    d.line([(cx + head_w, by + 14), (cx + head_w, by + bh - 14)], fill=(48, 48, 56), width=1)
    px = cx + head_w + 18
    for cnt, col in data.tier_counts.values():
        px += _tier_pill(canvas, px, by + (bh - 38) // 2, 38, col, cnt) + 8
    left_end = cx + box_w

    if not data.show_wallet:
        return
    items = [("LEVEL", _num(data.level), "LV", (200, 200, 210), (255, 255, 255)),
             ("VP", _num(data.vp), "VP", (45, 224, 160), (45, 224, 160)),
             ("RP", _num(data.rp), "RP", (200, 200, 210), (255, 255, 255)),
             ("KC", _num(data.kc), "KC", (200, 200, 210), (255, 255, 255))]
    widths = [_stat_box(canvas, 0, 0, bh, lab, val, glyph=g, draw=False) for lab, val, g, _, _ in items]
    total = sum(widths) + 12 * (len(items) - 1)
    rx = x + w - 22 - total
    if rx < left_end + 14:
        items, widths = items[:-1], widths[:-1]
        total = sum(widths) + 12 * (len(items) - 1)
        rx = x + w - 22 - total
    for (lab, val, g, gc, vc), wd in zip(items, widths):
        _stat_box(canvas, rx, by, bh, lab, val, glyph=g, glyph_color=gc, value_color=vc)
        rx += wd + 12


def _skin_cell(canvas, x, y, w, h, skin: CardSkin, show_name: bool):
    col = _rgb(skin.color)
    bg = _vgrad(w - 2, h - 2, _mix(BASE, col, 0.16), (9, 9, 11))
    _panel(canvas, x, y, w, h, 14, bg, outline=_mix((20, 20, 22), col, 0.38), ow=1)
    d = ImageDraw.Draw(canvas)
    d.rectangle((x + 14, y, x + w - 14, y + 2), fill=col)
    tf = _font("x", 11)
    tw_ = int(_spaced_w(tf, "JOKER", 1.6) + 20)
    _panel(canvas, x + w - tw_ - 12, y + 3, tw_, 20, 8, (10, 10, 12), outline=_mix((20, 20, 22), col, 0.3))
    _spaced(d, x + w - tw_ - 12 + 10, y + 13, "JOKER", tf, col, 1.6)

    name_h = 30 if show_name else 0
    aw, ah = w - 28, h - 42 - name_h
    if skin.icon is not None:
        s = min(aw / skin.icon.width, ah / skin.icon.height, 1.6)
        iw, ih = max(1, int(skin.icon.width * s)), max(1, int(skin.icon.height * s))
        ic = skin.icon.resize((iw, ih), _LANCZOS)
        canvas.paste(ic, (x + (w - iw) // 2, y + 26 + (ah - ih) // 2), ic)
    else:
        d.text((x + w // 2, y + 26 + ah // 2), "?", font=_font("x", 34), fill=_mix(BASE, col, 0.5), anchor="mm")
    if show_name:
        nf = _font("s", max(12, int(w * 0.06)))
        d.text((x + w // 2, y + h - 18), _fit(nf, skin.name, w - 20), font=nf, fill=(205, 205, 212), anchor="mm")


def render_card(data: CardData, columns: Optional[int] = None, max_bytes: int = 9_500_000) -> bytes:
    n_cells = max(1, len(data.skins))
    cols = auto_columns(n_cells) if columns is None else max(3, min(10, int(columns)))
    W, H, cw, ch, rows = _dims(n_cells, cols)
    canvas = _vgrad(W, H, BASE, BASE2)

    content_w = W - 2 * M
    _profile(canvas, M, M, data, content_w)
    sy = M + PROFILE_H + 26
    _stats_bar(canvas, M, sy, content_w, STATS_H, data, len(data.skins))

    gy = M + HEAD_H
    for i, skin in enumerate(data.skins):
        r_, c_ = divmod(i, cols)
        _skin_cell(canvas, M + c_ * (cw + G), gy + r_ * (ch + G), cw, ch, skin, data.show_names)

    d = ImageDraw.Draw(canvas)
    ff = _font("s", 15)
    _spaced(d, W / 2 - _spaced_w(ff, "JOKER  •  DÙNG /skins ĐỂ XUẤT ẢNH KHO SKIN CỦA CẬU", 2) / 2, H - FOOT_H // 2 - 4,
            "JOKER  •  DÙNG /skins ĐỂ XUẤT ẢNH KHO SKIN CỦA CẬU", ff, (90, 90, 100), 2)

    img = canvas
    for _ in range(4):
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=6)
        if buf.tell() <= max_bytes:
            return buf.getvalue()
        img = img.resize((int(img.width * 0.8), int(img.height * 0.8)), _LANCZOS)
    return buf.getvalue()
