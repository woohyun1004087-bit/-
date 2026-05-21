from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

load_dotenv()

# -----------------------------
# Basic configuration
# -----------------------------

KST = ZoneInfo("Asia/Seoul")
DATA_FILE = Path("data.json")
TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID")

RANKS = ["천민", "상민", "중인", "양반", "영의정", "왕"]
PLAYABLE_RANKS = ["천민", "상민", "중인", "양반", "영의정"]

WORK_PAY: dict[str, int] = {
    "상민": 12_000,
    "중인": 20_000,
    "양반": 35_000,
    "영의정": 0,
    "천민": 0,
    "왕": 0,
}

DEFAULT_TAX_RATE = 10
DEFAULT_BALANCE = 0
DEFAULT_RANK = "상민"

ROLE_NAMES = {rank: rank for rank in RANKS}
BLOCKED_EARN_ROLE = "유람객"

# -----------------------------
# 박타기 설정
# -----------------------------

MAX_BAX_STAGE = 10
BASE_BAX_SUCCESS = 80
BAX_SUCCESS_DROP = 8

# stage 0 = 시작 직후
# stage 1 = 1단계 성공 후
# stage 10 = 최종 성공 후
BAX_STAGE_MULTIPLIERS: dict[int, float] = {
    0: 1.0,
    1: 1.2,
    2: 1.5,
    3: 1.9,
    4: 2.5,
    5: 3.2,
    6: 4.2,
    7: 5.6,
    8: 7.5,
    9: 10.0,
    10: 15.0,
}

# -----------------------------
# 노가다 설정
# -----------------------------

NOGADA_COOLDOWN_SECONDS = 10
nogada_last_used: dict[tuple[int, int], float] = {}

# -----------------------------
# Storage
# -----------------------------

def now_kst() -> datetime:
    return datetime.now(KST)


def today_key(dt: datetime | None = None) -> str:
    dt = dt or now_kst()
    return dt.strftime("%Y-%m-%d")


def clamp_money(value: int) -> int:
    return max(0, int(value))


@dataclass
class MemberData:
    rank: str = DEFAULT_RANK
    balance: int = DEFAULT_BALANCE
    last_work_day: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MemberData":
        return cls(
            rank=raw.get("rank", DEFAULT_RANK),
            balance=int(raw.get("balance", DEFAULT_BALANCE)),
            last_work_day=raw.get("last_work_day", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "balance": self.balance,
            "last_work_day": self.last_work_day,
        }


class EconomyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.data: dict[str, Any] = {"guilds": {}}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {"guilds": {}}
        self.data.setdefault("guilds", {})

    async def save(self) -> None:
        async with self.lock:
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def ensure_guild(self, guild_id: int) -> dict[str, Any]:
        guilds = self.data.setdefault("guilds", {})
        gid = str(guild_id)
        if gid not in guilds:
            guilds[gid] = {
                "tax_rate": DEFAULT_TAX_RATE,
                "treasury": 0,
                "members": {},
            }
        guilds[gid].setdefault("tax_rate", DEFAULT_TAX_RATE)
        guilds[gid].setdefault("treasury", 0)
        guilds[gid].setdefault("members", {})
        return guilds[gid]

    def get_member(self, guild_id: int, user_id: int) -> MemberData:
        guild = self.ensure_guild(guild_id)
        members = guild["members"]
        uid = str(user_id)
        if uid not in members:
            members[uid] = MemberData().to_dict()
        return MemberData.from_dict(members[uid])

    def set_member(self, guild_id: int, user_id: int, member: MemberData) -> None:
        guild = self.ensure_guild(guild_id)
        guild["members"][str(user_id)] = member.to_dict()

    def get_tax_rate(self, guild_id: int) -> int:
        return int(self.ensure_guild(guild_id)["tax_rate"])

    def set_tax_rate(self, guild_id: int, rate: int) -> None:
        self.ensure_guild(guild_id)["tax_rate"] = int(rate)

    def get_treasury(self, guild_id: int) -> int:
        return int(self.ensure_guild(guild_id)["treasury"])

    def add_treasury(self, guild_id: int, amount: int) -> None:
        guild = self.ensure_guild(guild_id)
        guild["treasury"] = int(guild.get("treasury", 0)) + int(amount)

    def remove_treasury(self, guild_id: int, amount: int) -> bool:
        guild = self.ensure_guild(guild_id)
        treasury = int(guild.get("treasury", 0))
        if treasury < amount:
            return False
        guild["treasury"] = treasury - int(amount)
        return True


store = EconomyStore(DATA_FILE)

# -----------------------------
# Helpers
# -----------------------------

def is_king(member_data: MemberData) -> bool:
    return member_data.rank == "왕"


def rank_index(rank: str) -> int:
    return RANKS.index(rank) if rank in RANKS else -1


def infer_rank_from_member(member: discord.Member) -> tuple[str, bool]:
    matched = [role.name for role in member.roles if role.name in RANKS]
    if not matched:
        return DEFAULT_RANK, False
    return max(matched, key=rank_index), True


def next_higher_rank(rank: str) -> str | None:
    if rank not in PLAYABLE_RANKS:
        return None
    i = PLAYABLE_RANKS.index(rank)
    if i + 1 >= len(PLAYABLE_RANKS):
        return None
    return PLAYABLE_RANKS[i + 1]


def next_lower_rank(rank: str) -> str | None:
    if rank not in PLAYABLE_RANKS:
        return None
    i = PLAYABLE_RANKS.index(rank)
    if i - 1 < 0:
        return None
    return PLAYABLE_RANKS[i - 1]


def can_work(rank: str) -> bool:
    return rank in {"상민", "중인", "양반", "영의정"}


def work_pay_for(rank: str) -> int:
    return WORK_PAY.get(rank, 0)


def has_role_name(member: discord.Member, role_name: str) -> bool:
    return any(role.name == role_name for role in member.roles)


def can_receive_money(member: discord.Member) -> bool:
    return not has_role_name(member, BLOCKED_EARN_ROLE)


def bax_success_rate(stage: int) -> int:
    return max(5, BASE_BAX_SUCCESS - (stage - 1) * BAX_SUCCESS_DROP)


def bax_multiplier(stage: int) -> float:
    return BAX_STAGE_MULTIPLIERS.get(stage, 1.0)


def bax_payout_for_stage(committed: int, stage: int) -> int:
    return int(committed * bax_multiplier(stage))


def bax_settlement_for_stage(committed: int, stage: int, tax_rate: int) -> tuple[int, int, int]:
    gross = bax_payout_for_stage(committed, stage)
    tax = gross * tax_rate // 100
    net = gross - tax
    return gross, tax, net


async def sync_discord_rank_role(member: discord.Member, new_rank: str) -> None:
    """Synchronize actual Discord roles if the server has same-named roles."""
    guild = member.guild
    rank_roles = [role for role in guild.roles if role.name in ROLE_NAMES.values()]
    target_role = next((role for role in rank_roles if role.name == new_rank), None)

    try:
        if rank_roles:
            await member.remove_roles(*rank_roles, reason="경제 봇 신분 동기화")
        if target_role is not None:
            await member.add_roles(target_role, reason="경제 봇 신분 동기화")
    except discord.Forbidden:
        pass
    except discord.HTTPException:
        pass


async def ensure_registered(
    guild_id: int,
    user_id: int,
    member: discord.Member | None = None,
    *,
    save: bool = True,
) -> MemberData:
    data = store.get_member(guild_id, user_id)

    if member is not None and member.guild.id == guild_id and member.id == user_id:
        inferred_rank, has_rank_role = infer_rank_from_member(member)
        if has_rank_role and data.rank != inferred_rank:
            data.rank = inferred_rank

    if data.rank not in RANKS:
        data.rank = DEFAULT_RANK

    data.balance = clamp_money(data.balance)
    store.set_member(guild_id, user_id, data)

    if save:
        await store.save()

    return data


async def require_guild_and_member(
    interaction: discord.Interaction,
) -> tuple[discord.Guild, discord.Member, MemberData]:
    if interaction.guild is None:
        raise app_commands.CheckFailure("이 명령어는 서버에서만 사용할 수 있습니다.")
    if not isinstance(interaction.user, discord.Member):
        raise app_commands.CheckFailure("서버 멤버만 사용할 수 있습니다.")
    data = await ensure_registered(interaction.guild.id, interaction.user.id, interaction.user)
    return interaction.guild, interaction.user, data


async def king_only(
    interaction: discord.Interaction,
) -> tuple[discord.Guild, discord.Member, MemberData]:
    guild, member, data = await require_guild_and_member(interaction)
    if not is_king(data):
        raise app_commands.CheckFailure("왕만 사용할 수 있습니다.")
    return guild, member, data


# -----------------------------
# 박타기 세션
# -----------------------------

@dataclass
class BaxSession:
    guild_id: int
    user_id: int
    bet: int
    committed: int
    stage: int = 0
    active: bool = True

    def next_stage(self) -> int:
        return self.stage + 1

    def next_rate(self) -> int:
        return bax_success_rate(self.next_stage())


bax_sessions: dict[tuple[int, int], BaxSession] = {}


class BaxView(discord.ui.View):
    def __init__(self, session: BaxSession) -> None:
        super().__init__(timeout=300)
        self.session = session

    def _key(self) -> tuple[int, int]:
        return (self.session.guild_id, self.session.user_id)

    @discord.ui.button(label="강화", style=discord.ButtonStyle.primary)
    async def enhance(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return

        key = self._key()
        session = bax_sessions.get(key)
        if session is None or not session.active:
            await interaction.response.send_message("진행 중인 박타기 세션이 없습니다.", ephemeral=True)
            return

        if interaction.user.id != session.user_id:
            await interaction.response.send_message("이 세션은 본인만 조작할 수 있습니다.", ephemeral=True)
            return

        data = await ensure_registered(session.guild_id, session.user_id, interaction.user, save=False)

        next_stage = session.stage + 1
        rate = bax_success_rate(next_stage)
        roll = random.randint(1, 100)

        if roll > rate:
            session.active = False
            bax_sessions.pop(key, None)

            # 실패 시 베팅 금액 전액을 국고로
            store.add_treasury(session.guild_id, session.committed)

            store.set_member(session.guild_id, session.user_id, data)
            await store.save()

            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True

            await interaction.response.edit_message(
                content=(
                    f"**{next_stage}단계 실패!**\n"
                    f"성공 확률: **{rate}%**, 나온 수: **{roll}**\n"
                    f"베팅 금액 **{session.committed:,}원**이 국고로 들어갔습니다.\n"
                    f"박타기 결과는 실패로 종료되었습니다."
                ),
                view=self,
            )
            return

        session.stage = next_stage
        store.set_member(session.guild_id, session.user_id, data)
        await store.save()

        tax_rate = store.get_tax_rate(session.guild_id)
        current_gross, current_tax, current_net = bax_settlement_for_stage(session.committed, session.stage, tax_rate)

        if session.stage >= MAX_BAX_STAGE:
            session.active = False
            bax_sessions.pop(key, None)

            data.balance += current_net
            store.add_treasury(session.guild_id, current_tax)
            store.set_member(session.guild_id, session.user_id, data)
            await store.save()

            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True

            await interaction.response.edit_message(
                content=(
                    f"**10단계 성공!**\n"
                    f"성공 확률: **{rate}%**, 나온 수: **{roll}**\n"
                    f"현재 배율: **{bax_multiplier(session.stage):.1f}배**\n"
                    f"총 지급액: **{current_gross:,}원**\n"
                    f"세금: **{current_tax:,}원**\n"
                    f"실수령액: **{current_net:,}원**\n"
                    f"현재 잔액: **{data.balance:,}원**"
                ),
                view=self,
            )
            return

        _, _, next_net = bax_settlement_for_stage(
            session.committed,
            session.stage + 1,
            tax_rate,
        )

        await interaction.response.edit_message(
            content=(
                f"**{session.stage}단계 성공!**\n"
                f"성공 확률: **{rate}%**, 나온 수: **{roll}**\n"
                f"현재 배율: **{bax_multiplier(session.stage):.1f}배**\n"
                f"지금 그만두면: **{current_net:,}원**\n"
                f"다음 단계 성공 시 실수령액: **{next_net:,}원**\n"
                f"세율: **{tax_rate}%**\n"
                f"원하면 아래 버튼으로 계속하거나 멈출 수 있습니다."
            ),
            view=self,
        )

    @discord.ui.button(label="그만하기", style=discord.ButtonStyle.danger)
    async def stop(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return

        key = self._key()
        session = bax_sessions.get(key)
        if session is None or not session.active:
            await interaction.response.send_message("진행 중인 박타기 세션이 없습니다.", ephemeral=True)
            return

        if interaction.user.id != session.user_id:
            await interaction.response.send_message("이 세션은 본인만 조작할 수 있습니다.", ephemeral=True)
            return

        data = await ensure_registered(session.guild_id, session.user_id, interaction.user, save=False)
        tax_rate = store.get_tax_rate(session.guild_id)
        gross, tax, net = bax_settlement_for_stage(session.committed, session.stage, tax_rate)

        data.balance += net
        store.add_treasury(session.guild_id, tax)

        session.active = False
        bax_sessions.pop(key, None)
        store.set_member(session.guild_id, session.user_id, data)
        await store.save()

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        await interaction.response.edit_message(
            content=(
                f"정산 완료.\n"
                f"현재 단계: **{session.stage}단계**\n"
                f"배율: **{bax_multiplier(session.stage):.1f}배**\n"
                f"총 지급액: **{gross:,}원**\n"
                f"세금: **{tax:,}원**\n"
                f"실수령액: **{net:,}원**\n"
                f"현재 잔액: **{data.balance:,}원**"
            ),
            view=self,
        )

    async def on_timeout(self) -> None:
        key = self._key()
        session = bax_sessions.get(key)
        if session is not None:
            session.active = False
            bax_sessions.pop(key, None)

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


# -----------------------------
# Discord bot
# -----------------------------

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    try:
        if TEST_GUILD_ID:
            guild_obj = discord.Object(id=int(TEST_GUILD_ID))
            bot.tree.copy_global_to(guild=guild_obj)
            await bot.tree.sync(guild=guild_obj)
            print(f"Synced commands to test guild {TEST_GUILD_ID}")
        else:
            await bot.tree.sync()
            print("Synced global commands")
    except Exception as exc:
        print(f"Command sync failed: {exc}")


# -----------------------------
# Commands
# -----------------------------

@bot.tree.command(name="내정보", description="내 신분과 자산을 확인합니다.")
@app_commands.describe(target="확인할 사용자")
async def myinfo(
    interaction: discord.Interaction,
    target: discord.Member | None = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return

    target = target or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.response.send_message("서버 멤버만 확인할 수 있습니다.", ephemeral=True)
        return

    data = await ensure_registered(interaction.guild.id, target.id, target)

    today = today_key()
    if has_role_name(target, BLOCKED_EARN_ROLE):
        work_status = "유람객 불가"
    elif data.last_work_day == today:
        work_status = "오늘 완료"
    else:
        work_status = "가능" if can_work(data.rank) else "불가"

    msg = (
        f"**{target.display_name}**\n"
        f"신분: **{data.rank}**\n"
        f"잔액: **{data.balance:,}원**\n"
        f"오늘 일하기 가능 여부: **{work_status}**\n"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="일하기", description="하루에 한 번 일해서 돈을 벌 수 있습니다.")
async def work(interaction: discord.Interaction) -> None:
    guild, member, data = await require_guild_and_member(interaction)

    if has_role_name(member, BLOCKED_EARN_ROLE):
        await interaction.response.send_message("유람객은 /일하기를 할 수 없습니다.", ephemeral=True)
        return

    today = today_key()
    if data.last_work_day == today:
        await interaction.response.send_message("오늘은 이미 /일하기를 했습니다.", ephemeral=True)
        return

    if data.rank == "왕":
        await interaction.response.send_message("왕은 /일하기를 할 수 없습니다.", ephemeral=True)
        return

    if data.rank == "천민":
        await interaction.response.send_message("천민은 /일하기를 할 수 없습니다.", ephemeral=True)
        return

    pay = work_pay_for(data.rank)
    if pay <= 0:
        await interaction.response.send_message("현재 신분으로는 일할 수 없습니다.", ephemeral=True)
        return

    tax_rate = store.get_tax_rate(guild.id)
    tax = (pay * tax_rate) // 100
    net = pay - tax

    data.balance += net
    data.last_work_day = today
    store.set_member(guild.id, member.id, data)
    store.add_treasury(guild.id, tax)
    await store.save()

    await interaction.response.send_message(
        f"{member.mention}이(가) 일해서 **{pay:,}원** 벌었습니다.\n"
        f"세금 **{tax:,}원**이 국고로 들어가고, 실제 수령액은 **{net:,}원**입니다.",
        ephemeral=False,
    )


@bot.tree.command(name="노가다", description="짧은 쿨타임으로 일하기의 10분의 1만큼 벌 수 있습니다.")
async def nogada(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return

    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("서버 멤버만 사용할 수 있습니다.", ephemeral=True)
        return

    member = interaction.user
    guild = interaction.guild
    key = (guild.id, member.id)

    if has_role_name(member, BLOCKED_EARN_ROLE):
        await interaction.response.send_message("유람객은 /노가다를 할 수 없습니다.", ephemeral=True)
        return

    data = await ensure_registered(guild.id, member.id, member)

    if data.rank == "왕":
        await interaction.response.send_message("왕은 /노가다를 할 수 없습니다.", ephemeral=True)
        return

    if data.rank == "천민":
        await interaction.response.send_message("천민은 /노가다를 할 수 없습니다.", ephemeral=True)
        return

    base_pay = work_pay_for(data.rank) // 10
    if base_pay <= 0:
        await interaction.response.send_message("현재 신분으로는 노가다를 할 수 없습니다.", ephemeral=True)
        return

    now = time.monotonic()
    last_used = nogada_last_used.get(key, 0.0)
    elapsed = now - last_used
    if elapsed < NOGADA_COOLDOWN_SECONDS:
        remaining = NOGADA_COOLDOWN_SECONDS - elapsed
        await interaction.response.send_message(
            f"/노가다는 아직 쿨타임입니다. **{remaining:.1f}초** 후에 다시 시도하세요.",
            ephemeral=True,
        )
        return

    nogada_last_used[key] = now

    tax_rate = store.get_tax_rate(guild.id)
    tax = (base_pay * tax_rate) // 100
    net = base_pay - tax

    data.balance += net
    store.set_member(guild.id, member.id, data)
    store.add_treasury(guild.id, tax)
    await store.save()

    await interaction.response.send_message(
        f"{member.mention}이(가) /노가다로 **{base_pay:,}원** 벌었습니다.\n"
        f"세금 **{tax:,}원**이 국고로 들어가고, 실제 수령액은 **{net:,}원**입니다.",
        ephemeral=False,
    )


@bot.tree.command(name="돈주기", description="다른 사용자에게 돈을 송금합니다.")
@app_commands.describe(
    target="받을 사용자",
    amount="송금할 금액",
    reason="사유",
)
async def money_give(
    interaction: discord.Interaction,
    target: discord.Member,
    amount: app_commands.Range[int, 1, 10_000_000_000],
    reason: str = "",
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return

    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("서버 멤버만 사용할 수 있습니다.", ephemeral=True)
        return

    sender = interaction.user

    if target.bot:
        await interaction.response.send_message("봇에게는 송금할 수 없습니다.", ephemeral=True)
        return

    if target.id == sender.id:
        await interaction.response.send_message("자기 자신에게는 송금할 수 없습니다.", ephemeral=True)
        return

    if has_role_name(target, BLOCKED_EARN_ROLE):
        await interaction.response.send_message("유람객은 돈을 받을 수 없습니다.", ephemeral=True)
        return

    sender_data = await ensure_registered(interaction.guild.id, sender.id, sender)
    target_data = await ensure_registered(interaction.guild.id, target.id, target)

    send_amount = int(amount)

    if sender_data.balance < send_amount:
        await interaction.response.send_message(
            f"잔액이 부족합니다.\n"
            f"현재 잔액: **{sender_data.balance:,}원**\n"
            f"필요 금액: **{send_amount:,}원**",
            ephemeral=True,
        )
        return

    sender_data.balance -= send_amount
    target_data.balance += send_amount

    store.set_member(interaction.guild.id, sender.id, sender_data)
    store.set_member(interaction.guild.id, target.id, target_data)
    await store.save()

    reason_text = f"\n사유: {reason}" if reason else ""

    await interaction.response.send_message(
        f"{sender.mention}이(가) {target.mention}에게 **{send_amount:,}원**을 송금했습니다.{reason_text}",
        ephemeral=False,
    )


@bot.tree.command(name="세금설정", description="왕이 세율을 설정합니다.")
@app_commands.describe(rate="0~100 사이의 세율(%)")
async def tax_set(
    interaction: discord.Interaction,
    rate: app_commands.Range[int, 0, 100],
) -> None:
    guild, _, _ = await king_only(interaction)
    store.set_tax_rate(guild.id, int(rate))
    await store.save()
    await interaction.response.send_message(f"세율이 **{rate}%**로 설정되었습니다.", ephemeral=False)


@bot.tree.command(name="국고보기", description="국고를 확인합니다.")
async def treasury_view(interaction: discord.Interaction) -> None:
    guild, _, data = await require_guild_and_member(interaction)
    if data.rank not in {"왕", "영의정"}:
        await interaction.response.send_message("국고는 왕 또는 영의정만 확인할 수 있습니다.", ephemeral=True)
        return

    treasury = store.get_treasury(guild.id)
    tax_rate = store.get_tax_rate(guild.id)
    await interaction.response.send_message(
        f"국고: **{treasury:,}원**\n세율: **{tax_rate}%**",
        ephemeral=True,
    )


@bot.tree.command(name="돈관리", description="왕이 특정 대상에게 돈을 지급하거나 차감합니다.")
@app_commands.describe(
    action="지급 또는 차감",
    target_type="사람, 역할, 전체 중 하나",
    amount="1인당 금액",
    member="대상 사용자",
    role="대상 역할",
    reason="사유",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="지급", value="give"),
        app_commands.Choice(name="차감", value="take"),
    ],
    target_type=[
        app_commands.Choice(name="사람", value="member"),
        app_commands.Choice(name="역할", value="role"),
        app_commands.Choice(name="전체", value="all"),
    ],
)
async def money_manage(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    target_type: app_commands.Choice[str],
    amount: app_commands.Range[int, 1, 10_000_000_000],
    member: discord.Member | None = None,
    role: discord.Role | None = None,
    reason: str = "",
) -> None:
    guild, _, _ = await king_only(interaction)

    targets: list[discord.Member] = []

    if target_type.value == "member":
        if member is None:
            await interaction.response.send_message("대상 사용자를 지정해야 합니다.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("봇은 대상이 될 수 없습니다.", ephemeral=True)
            return
        targets = [member]

    elif target_type.value == "role":
        if role is None:
            await interaction.response.send_message("대상 역할을 지정해야 합니다.", ephemeral=True)
            return

        if any(m.bot for m in role.members):
            await interaction.response.send_message(
                "해당 역할에 봇이 포함되어 있어서 지급할 수 없습니다.",
                ephemeral=True,
            )
            return

        targets = [m for m in role.members if not m.bot]
        if not targets:
            await interaction.response.send_message("해당 역할에 대상이 없습니다.", ephemeral=True)
            return

    elif target_type.value == "all":
        targets = [m for m in guild.members if not m.bot]
        if not targets:
            await interaction.response.send_message("대상 사용자가 없습니다.", ephemeral=True)
            return

    else:
        await interaction.response.send_message("잘못된 대상 종류입니다.", ephemeral=True)
        return

    count = len(targets)
    per_person = int(amount)

    if action.value == "give":
        payable_targets = [t for t in targets if can_receive_money(t)]
        blocked_count = count - len(payable_targets)

        if not payable_targets:
            await interaction.response.send_message("유람객만 있어서 지급할 수 없습니다.", ephemeral=True)
            return

        total_amount = per_person * len(payable_targets)

        if store.get_treasury(guild.id) < total_amount:
            await interaction.response.send_message(
                f"국고 잔액이 부족합니다.\n필요 금액: **{total_amount:,}원**",
                ephemeral=True,
            )
            return

        for target in payable_targets:
            data = await ensure_registered(guild.id, target.id, target, save=False)
            data.balance += per_person
            store.set_member(guild.id, target.id, data)

        store.remove_treasury(guild.id, total_amount)
        await store.save()

        if target_type.value == "member":
            title = f"{targets[0].mention}에게"
        elif target_type.value == "role":
            title = f"{role.mention} 역할의 **{len(payable_targets)}명**에게"
        else:
            title = f"서버의 **{len(payable_targets)}명**에게"

        blocked_text = f"\n유람객 **{blocked_count}명**은 제외했습니다." if blocked_count else ""

        await interaction.response.send_message(
            f"{title} 1인당 **{per_person:,}원**씩 지급했습니다.\n"
            f"총 지급액: **{total_amount:,}원**\n"
            f"사유: {reason}\n"
            f"남은 국고: **{store.get_treasury(guild.id):,}원**"
            f"{blocked_text}",
            ephemeral=False,
        )
        return

    total_taken = 0
    for target in targets:
        data = await ensure_registered(guild.id, target.id, target, save=False)
        taken = min(data.balance, per_person)
        data.balance -= taken
        total_taken += taken
        store.set_member(guild.id, target.id, data)

    store.add_treasury(guild.id, total_taken)
    await store.save()

    if target_type.value == "member":
        title = f"{targets[0].mention}의 돈을"
    elif target_type.value == "role":
        title = f"{role.mention} 역할의 **{count}명**의 돈을"
    else:
        title = f"서버의 **{count}명**의 돈을"

    await interaction.response.send_message(
        f"{title} 차감했습니다.\n"
        f"실제 차감액: **{total_taken:,}원**\n"
        f"사유: {reason}\n"
        f"남은 국고: **{store.get_treasury(guild.id):,}원**",
        ephemeral=False,
    )


@bot.tree.command(name="박타기", description="한 단계씩 강화하면서 중간에 멈출 수 있습니다.")
@app_commands.describe(
    bet="처음 걸 금액",
)
async def bax_taogi(
    interaction: discord.Interaction,
    bet: app_commands.Range[int, 1, 10_000_000_000],
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return

    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("서버 멤버만 사용할 수 있습니다.", ephemeral=True)
        return

    member = interaction.user

    if has_role_name(member, BLOCKED_EARN_ROLE):
        await interaction.response.send_message("유람객은 /박타기를 할 수 없습니다.", ephemeral=True)
        return

    key = (interaction.guild.id, member.id)
    if key in bax_sessions and bax_sessions[key].active:
        await interaction.response.send_message("이미 진행 중인 박타기 세션이 있습니다.", ephemeral=True)
        return

    data = await ensure_registered(interaction.guild.id, member.id, member)

    if data.balance < bet:
        await interaction.response.send_message(
            f"잔액이 부족합니다.\n"
            f"현재 잔액: **{data.balance:,}원**\n"
            f"필요 금액: **{bet:,}원**",
            ephemeral=True,
        )
        return

    data.balance -= bet
    store.set_member(interaction.guild.id, member.id, data)
    await store.save()

    session = BaxSession(
        guild_id=interaction.guild.id,
        user_id=member.id,
        bet=bet,
        committed=bet,
        stage=0,
        active=True,
    )
    bax_sessions[key] = session
    view = BaxView(session)

    tax_rate = store.get_tax_rate(interaction.guild.id)
    _, _, net = bax_settlement_for_stage(bet, 0, tax_rate)

    await interaction.response.send_message(
        f"/박타기 시작\n"
        f"베팅 금액: **{bet:,}원**\n"
        f"현재 단계: **0단계**\n"
        f"현재 배율: **{bax_multiplier(0):.1f}배**\n"
        f"지금 그만두면: **{net:,}원**\n"
        f"다음 성공 확률: **{session.next_rate()}%**\n"
        f"세율: **{tax_rate}%**\n"
        f"아래 버튼으로 한 단계씩 진행하거나 중간에 멈출 수 있습니다.",
        view=view,
        ephemeral=False,
    )


# -----------------------------
# Error handling
# -----------------------------

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.CheckFailure):
        message = str(error)
    else:
        message = f"오류가 발생했습니다: {error}"

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# -----------------------------
# Startup checks
# -----------------------------

async def warm_up_existing_guilds() -> None:
    for guild in bot.guilds:
        store.ensure_guild(guild.id)
    await store.save()


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    store.ensure_guild(guild.id)
    await store.save()


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if member.guild:
        await ensure_registered(member.guild.id, member.id, member)


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN 환경변수가 설정되지 않았습니다.")

    await warm_up_existing_guilds()
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
