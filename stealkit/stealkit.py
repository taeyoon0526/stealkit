from __future__ import annotations

import asyncio
import io
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

import aiohttp
import discord
from discord import ui
from discord.errors import Forbidden, HTTPException
from redbot.core import Config, commands
from redbot.core.bot import Red


CUSTOM_EMOJI_RE = re.compile(r"^<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{1,32}):(?P<id>\d{1,25})>$")
ID_RE = re.compile(r"^\d{1,25}$")

NOTICE_TEXT = "복사된 이모지/스티커의 사용 책임 및 저작권 책임은 명령어 실행자에게 있습니다."
MAX_EMOJI_ITEMS = 10
NAME_ATTEMPTS = 100
STICKER_WAIT_SECONDS = 60

COLOR_SUCCESS = 0x2ECC71
COLOR_INFO = 0x3498DB
COLOR_WARNING = 0xF39C12
COLOR_ERROR = 0xE74C3C
COLOR_PARTIAL = 0xF1C40F


@dataclass(frozen=True)
class ParsedEmoji:
    raw: str
    emoji_id: int
    name: str
    animated: bool
    id_only: bool = False


@dataclass(frozen=True)
class PrecheckResult:
    allowed: bool
    static_limit: int
    animated_limit: int
    static_used: int
    animated_used: int
    requested_static: int
    requested_animated: int

    @property
    def static_remaining(self) -> int:
        return max(0, self.static_limit - self.static_used)

    @property
    def animated_remaining(self) -> int:
        return max(0, self.animated_limit - self.animated_used)


@dataclass(frozen=True)
class StickerSourceInfo:
    sticker_id: int
    name: str
    url: str
    format_name: str
    filename: str
    emoji: str
    description: str


@dataclass(frozen=True)
class SessionInfo:
    guild_id: int
    channel_id: int
    user_id: int
    started_at: float


class DownloadError(RuntimeError):
    pass


class UnsupportedStickerFormat(RuntimeError):
    pass


def _sanitize_asset_name(name: str, *, kind: str) -> str:
    if kind == "emoji":
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)
        cleaned = re.sub(r"_+", "_", cleaned).strip("_")
        if not cleaned:
            cleaned = "emoji"
        if len(cleaned) < 2:
            cleaned = f"{cleaned}_"
        return cleaned[:32]

    cleaned = re.sub(r"[\r\n\t]", " ", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "sticker"
    if len(cleaned) < 2:
        cleaned = f"{cleaned}_"
    return cleaned[:30]


def parse_custom_emoji_input(token: str) -> Optional[ParsedEmoji]:
    value = token.strip()
    if not value:
        return None

    match = CUSTOM_EMOJI_RE.match(value)
    if match:
        name = _sanitize_asset_name(match.group("name"), kind="emoji")
        return ParsedEmoji(
            raw=value,
            emoji_id=int(match.group("id")),
            name=name,
            animated=bool(match.group("animated")),
        )

    if ID_RE.match(value):
        return ParsedEmoji(
            raw=value,
            emoji_id=int(value),
            name=f"emoji_{value}",
            animated=False,
            id_only=True,
        )

    return None


def build_emoji_cdn_url(parsed: ParsedEmoji) -> str:
    extension = "gif" if parsed.animated else "png"
    return f"https://cdn.discordapp.com/emojis/{parsed.emoji_id}.{extension}?quality=lossless"


async def download_bytes(url: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=20)
    last_error = "알 수 없는 다운로드 오류"

    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if 200 <= response.status < 300:
                        data = await response.read()
                        if not data:
                            raise DownloadError("빈 파일을 받았습니다.")
                        return data
                    last_error = f"HTTP {response.status}"
        except (aiohttp.ClientError, asyncio.TimeoutError, DownloadError) as exc:
            last_error = str(exc) or exc.__class__.__name__

        if attempt == 0:
            await asyncio.sleep(0.75)

    raise DownloadError(last_error)


def resolve_available_name(
    guild: discord.Guild,
    base_name: str,
    kind: str = "emoji",
    *,
    reserved_names: Optional[Set[str]] = None,
) -> str:
    normalized_base = _sanitize_asset_name(base_name, kind=kind)
    existing = guild.emojis if kind == "emoji" else guild.stickers
    taken = {asset.name.lower() for asset in existing}
    if reserved_names:
        taken.update(name.lower() for name in reserved_names)

    for index in range(NAME_ATTEMPTS):
        if index == 0:
            candidate = normalized_base
        else:
            suffix = f"_{index}"
            limit = 32 if kind == "emoji" else 30
            candidate = f"{normalized_base[:limit - len(suffix)]}{suffix}"

        if candidate.lower() not in taken:
            return candidate

    raise ValueError("이름 충돌을 100회 안에 해결하지 못했습니다.")


def build_v2_message(
    kind: str,
    title: str,
    success_items: Sequence[str],
    fail_items: Sequence[str],
    notice_text: str,
    accent_color: int,
    *,
    info_items: Optional[Sequence[str]] = None,
) -> ui.LayoutView:
    view = ui.LayoutView(timeout=60)
    container = ui.Container(accent_color=accent_color)
    container.add_item(ui.TextDisplay(f"## {title}"))
    container.add_item(ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))

    if info_items:
        container.add_item(ui.TextDisplay("\n".join(info_items)))

    if success_items:
        success_text = "\n".join(f"- {item}" for item in success_items)
        container.add_item(ui.TextDisplay(f"**성공**\n{success_text}"))

    if fail_items:
        fail_text = "\n".join(f"- {item}" for item in fail_items)
        container.add_item(ui.TextDisplay(f"**실패**\n{fail_text}"))

    if not success_items and not fail_items and not info_items:
        container.add_item(ui.TextDisplay("처리할 항목이 없습니다."))

    container.add_item(ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))
    container.add_item(ui.TextDisplay(f"*{notice_text}*"))
    view.add_item(container)
    return view


def process_sticker_from_message(message: discord.Message) -> StickerSourceInfo:
    if not message.stickers:
        raise ValueError("메시지에 스티커가 없습니다.")

    sticker = message.stickers[0]
    format_name = sticker.format.name.lower()
    if sticker.format == discord.StickerFormatType.lottie:
        raise UnsupportedStickerFormat("Lottie 스티커는 새 서버 스티커로 업로드할 수 없습니다.")
    if sticker.format not in {
        discord.StickerFormatType.png,
        discord.StickerFormatType.apng,
        discord.StickerFormatType.gif,
    }:
        raise UnsupportedStickerFormat(f"지원하지 않는 스티커 포맷입니다: {format_name}")

    extension = "gif" if sticker.format == discord.StickerFormatType.gif else "png"
    return StickerSourceInfo(
        sticker_id=sticker.id,
        name=_sanitize_asset_name(sticker.name, kind="sticker"),
        url=str(sticker.url),
        format_name=format_name,
        filename=f"sticker_{sticker.id}.{extension}",
        emoji="🙂",
        description="Copied by StealKit from Discord CDN.",
    )


def precheck_emoji_slots(
    guild: discord.Guild,
    requested_static_count: int,
    requested_animated_count: int,
) -> PrecheckResult:
    static_used = sum(1 for emoji in guild.emojis if not emoji.animated)
    animated_used = sum(1 for emoji in guild.emojis if emoji.animated)
    limit = int(guild.emoji_limit)
    result = PrecheckResult(
        allowed=True,
        static_limit=limit,
        animated_limit=limit,
        static_used=static_used,
        animated_used=animated_used,
        requested_static=requested_static_count,
        requested_animated=requested_animated_count,
    )
    allowed = (
        requested_static_count <= result.static_remaining
        and requested_animated_count <= result.animated_remaining
    )
    return PrecheckResult(
        allowed=allowed,
        static_limit=result.static_limit,
        animated_limit=result.animated_limit,
        static_used=result.static_used,
        animated_used=result.animated_used,
        requested_static=result.requested_static,
        requested_animated=result.requested_animated,
    )


class StealKit(commands.Cog):
    """CDN에서 파일을 받아 현재 서버에 새 이모지/스티커를 생성합니다."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=7246602401, force_registration=True)
        self.config.register_guild(
            logging_enabled=False,
            log_channel_id=None,
            enable_debug_logging=False,
        )
        self.pending_sticker_sessions: Dict[Tuple[int, int], SessionInfo] = {}
        self.active_locks: Dict[int, asyncio.Lock] = {}

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self.active_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self.active_locks[guild_id] = lock
        return lock

    async def _send_v2(
        self,
        ctx: commands.Context,
        *,
        title: str,
        accent_color: int,
        success_items: Optional[Sequence[str]] = None,
        fail_items: Optional[Sequence[str]] = None,
        info_items: Optional[Sequence[str]] = None,
        kind: str = "notice",
    ) -> None:
        view = build_v2_message(
            kind,
            title,
            success_items or [],
            fail_items or [],
            NOTICE_TEXT,
            accent_color,
            info_items=info_items,
        )
        await ctx.send(view=view)

    async def _ensure_guild_and_permissions(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await self._send_v2(
                ctx,
                title="서버 전용 명령어",
                accent_color=COLOR_ERROR,
                fail_items=["이 명령어는 서버 채널에서만 사용할 수 있습니다."],
            )
            return False

        author_permissions = ctx.channel.permissions_for(ctx.author)
        if not self._has_manage_expressions(author_permissions):
            await self._send_v2(
                ctx,
                title="권한 부족",
                accent_color=COLOR_ERROR,
                fail_items=["명령어 실행자에게 `Manage Emojis and Stickers` 권한이 필요합니다."],
            )
            return False

        me = ctx.guild.me or ctx.guild.get_member(self.bot.user.id)
        if me is None:
            await self._send_v2(
                ctx,
                title="봇 상태 확인 실패",
                accent_color=COLOR_ERROR,
                fail_items=["현재 서버에서 봇 멤버 정보를 확인하지 못했습니다."],
            )
            return False

        bot_permissions = ctx.channel.permissions_for(me)
        if not self._has_manage_expressions(bot_permissions):
            await self._send_v2(
                ctx,
                title="봇 권한 부족",
                accent_color=COLOR_ERROR,
                fail_items=["봇에게 `Manage Emojis and Stickers` 권한이 필요합니다."],
            )
            return False

        if not bot_permissions.send_messages:
            return False

        return True

    @staticmethod
    def _has_manage_expressions(permissions: discord.Permissions) -> bool:
        return bool(
            getattr(permissions, "manage_expressions", False)
            or getattr(permissions, "manage_emojis_and_stickers", False)
            or getattr(permissions, "manage_emojis", False)
        )

    def _resolve_id_only_emoji(self, parsed: ParsedEmoji) -> ParsedEmoji:
        if not parsed.id_only:
            return parsed

        cached = self.bot.get_emoji(parsed.emoji_id)
        if cached is not None:
            return ParsedEmoji(
                raw=parsed.raw,
                emoji_id=parsed.emoji_id,
                name=_sanitize_asset_name(cached.name, kind="emoji"),
                animated=cached.animated,
                id_only=True,
            )

        # ID-only 입력은 원본 animated 여부를 안정적으로 알 수 없다.
        # 메타데이터 resolve 실패 시 스펙대로 기본 이름과 정적 PNG 경로를 사용한다.
        return parsed

    @commands.group(name="steal", invoke_without_command=True)
    async def steal_group(self, ctx: commands.Context) -> None:
        """이모지/스티커를 현재 서버에 새 자산으로 복사합니다."""
        if not await self._ensure_guild_and_permissions(ctx):
            return
        await self._send_v2(
            ctx,
            title="StealKit 사용법",
            accent_color=COLOR_INFO,
            info_items=[
                "`[p]steal emoji <이모지|ID...>`",
                "`[p]steal sticker`",
                "원본 자산은 수정하지 않고 CDN 파일만 받아 새 자산으로 생성합니다.",
            ],
        )

    @steal_group.command(name="emoji")
    async def steal_emoji(self, ctx: commands.Context, *items: str) -> None:
        """커스텀 이모지를 현재 서버에 새 이모지로 복사합니다."""
        if not await self._ensure_guild_and_permissions(ctx):
            return
        guild = ctx.guild
        assert guild is not None

        if not items:
            await self._send_v2(
                ctx,
                title="입력 형식 오류",
                accent_color=COLOR_WARNING,
                fail_items=["복사할 커스텀 이모지 문자열 또는 이모지 ID를 입력하세요."],
                info_items=["예: `[p]steal emoji <:blobsmile:123456789012345678>`"],
            )
            return

        if len(items) > MAX_EMOJI_ITEMS:
            await self._send_v2(
                ctx,
                title="요청 수 초과",
                accent_color=COLOR_ERROR,
                fail_items=[f"이모지는 한 번에 최대 {MAX_EMOJI_ITEMS}개까지만 복사할 수 있습니다."],
            )
            return

        parsed_items: List[ParsedEmoji] = []
        fail_items: List[str] = []
        for token in items:
            parsed = parse_custom_emoji_input(token)
            if parsed is None:
                fail_items.append(f"`{token}`: 커스텀 이모지 문자열 또는 숫자 ID가 아닙니다.")
                continue
            parsed_items.append(self._resolve_id_only_emoji(parsed))

        if not parsed_items:
            await self._send_v2(
                ctx,
                title="처리 가능한 항목 없음",
                accent_color=COLOR_ERROR,
                fail_items=fail_items,
            )
            return

        lock = self._guild_lock(guild.id)
        async with lock:
            requested_static = sum(1 for item in parsed_items if not item.animated)
            requested_animated = sum(1 for item in parsed_items if item.animated)
            precheck = precheck_emoji_slots(guild, requested_static, requested_animated)
            if not precheck.allowed:
                slot_failures = list(fail_items)
                if requested_static > precheck.static_remaining:
                    slot_failures.append(
                        f"정적 이모지 슬롯 부족: 요청 {requested_static}개 / 남은 슬롯 {precheck.static_remaining}개"
                    )
                if requested_animated > precheck.animated_remaining:
                    slot_failures.append(
                        f"애니메이션 이모지 슬롯 부족: 요청 {requested_animated}개 / 남은 슬롯 {precheck.animated_remaining}개"
                    )
                await self._send_v2(
                    ctx,
                    title="이모지 슬롯 부족",
                    accent_color=COLOR_ERROR,
                    fail_items=slot_failures,
                    info_items=["슬롯이 부족한 경우 생성 전에 전체 작업을 중단합니다."],
                )
                return

            success_items: List[str] = []
            reserved_names: Set[str] = set()
            for parsed in parsed_items:
                try:
                    url = build_emoji_cdn_url(parsed)
                    image = await download_bytes(url)
                    final_name = resolve_available_name(
                        guild,
                        parsed.name,
                        "emoji",
                        reserved_names=reserved_names,
                    )
                    # 업로드 실패는 중복 생성 위험 때문에 재시도하지 않는다.
                    created = await guild.create_custom_emoji(
                        name=final_name,
                        image=image,
                        reason=self._audit_reason(ctx),
                    )
                    reserved_names.add(created.name)
                    success_items.append(f"`{parsed.raw}` → `:{created.name}:`")
                except ValueError as exc:
                    fail_items.append(f"`{parsed.raw}`: {exc}")
                except DownloadError as exc:
                    fail_items.append(f"`{parsed.raw}`: CDN 다운로드 실패 ({exc})")
                except Forbidden:
                    fail_items.append(f"`{parsed.raw}`: 봇 권한 부족으로 생성하지 못했습니다.")
                except HTTPException as exc:
                    fail_items.append(f"`{parsed.raw}`: Discord 업로드 실패 ({exc.status})")

        await self._send_v2(
            ctx,
            title="이모지 복사 결과",
            accent_color=self._result_color(success_items, fail_items),
            success_items=success_items,
            fail_items=fail_items,
        )

    @steal_group.command(name="sticker")
    async def steal_sticker(self, ctx: commands.Context) -> None:
        """다음 스티커 메시지의 첫 번째 스티커를 현재 서버에 복사합니다."""
        if not await self._ensure_guild_and_permissions(ctx):
            return
        guild = ctx.guild
        assert guild is not None

        session_key = (guild.id, ctx.author.id)
        if session_key in self.pending_sticker_sessions:
            await self._send_v2(
                ctx,
                title="대기 세션 중복",
                accent_color=COLOR_WARNING,
                fail_items=["이미 이 서버에서 같은 사용자의 스티커 대기 세션이 진행 중입니다."],
            )
            return

        self.pending_sticker_sessions[session_key] = SessionInfo(
            guild_id=guild.id,
            channel_id=ctx.channel.id,
            user_id=ctx.author.id,
            started_at=time.monotonic(),
        )

        try:
            await self._send_v2(
                ctx,
                title="스티커 대기 중",
                accent_color=COLOR_INFO,
                info_items=[
                    "60초 안에 이 채널에 스티커가 포함된 메시지를 보내세요.",
                    "일반 텍스트 메시지는 무시하고 계속 대기합니다.",
                ],
            )

            def check(message: discord.Message) -> bool:
                return (
                    message.guild is not None
                    and message.guild.id == guild.id
                    and message.channel.id == ctx.channel.id
                    and message.author.id == ctx.author.id
                    and bool(message.stickers)
                )

            try:
                message = await self.bot.wait_for("message", timeout=STICKER_WAIT_SECONDS, check=check)
            except asyncio.TimeoutError:
                await self._send_v2(
                    ctx,
                    title="스티커 대기 시간 초과",
                    accent_color=COLOR_WARNING,
                    fail_items=["60초 안에 스티커 메시지를 받지 못했습니다."],
                )
                return

            source = process_sticker_from_message(message)
            lock = self._guild_lock(guild.id)
            async with lock:
                if len(guild.stickers) >= guild.sticker_limit:
                    await self._send_v2(
                        ctx,
                        title="스티커 슬롯 부족",
                        accent_color=COLOR_ERROR,
                        fail_items=[
                            f"현재 서버의 스티커 슬롯이 가득 찼습니다. 사용 {len(guild.stickers)}개 / 제한 {guild.sticker_limit}개"
                        ],
                    )
                    return

                success_items: List[str] = []
                fail_items: List[str] = []
                try:
                    data = await download_bytes(source.url)
                    final_name = resolve_available_name(guild, source.name, "sticker")
                    file = discord.File(io.BytesIO(data), filename=source.filename)
                    created = await guild.create_sticker(
                        name=final_name,
                        description=source.description,
                        emoji=source.emoji,
                        file=file,
                        reason=self._audit_reason(ctx),
                    )
                    success_items.append(f"`{source.name}` → `{created.name}`")
                except ValueError as exc:
                    fail_items.append(f"`{source.name}`: {exc}")
                except DownloadError as exc:
                    fail_items.append(f"`{source.name}`: CDN 다운로드 실패 ({exc})")
                except Forbidden:
                    fail_items.append(f"`{source.name}`: 봇 권한 부족으로 생성하지 못했습니다.")
                except HTTPException as exc:
                    fail_items.append(f"`{source.name}`: Discord 업로드 실패 ({exc.status})")

            await self._send_v2(
                ctx,
                title="스티커 복사 결과",
                accent_color=self._result_color(success_items, fail_items),
                success_items=success_items,
                fail_items=fail_items,
                info_items=[f"포맷: `{source.format_name}`"],
            )
        except UnsupportedStickerFormat as exc:
            await self._send_v2(
                ctx,
                title="지원하지 않는 스티커",
                accent_color=COLOR_ERROR,
                fail_items=[str(exc)],
            )
        finally:
            self.pending_sticker_sessions.pop(session_key, None)

    @staticmethod
    def _audit_reason(ctx: commands.Context) -> str:
        return f"StealKit copy requested by {ctx.author} ({ctx.author.id})"

    @staticmethod
    def _result_color(success_items: Sequence[str], fail_items: Sequence[str]) -> int:
        if success_items and fail_items:
            return COLOR_PARTIAL
        if success_items:
            return COLOR_SUCCESS
        return COLOR_ERROR
