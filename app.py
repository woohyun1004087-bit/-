from __future__ import annotations

import asyncio
import json
import os

from dotenv import load_dotenv
load_dotenv()
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

# -----------------------------
# Basic configuration
# -----------------------------

KST = ZoneInfo("Asia/Seoul")
DATA_FILE = Path("data.json")
TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID")

RANKS = ["천민", "상민", "중인", "양반", "영의정", "왕"]
PLAYABLE_RANKS = ["천민", "상민", "중인", "양반", "영의정"]

# Daily work pay by rank
WORK_PAY: dict[str, int] = {
    "상민": 12_000,
    "중인": 20_000,
    "양반": 35_000,
    "영의정": 0,  # cannot work
    "천민": 0,
    "왕": 0,
}

# Daily living cost by rank
LIVING_COST: dict[str, int] = {
    "천민": 1_000,
    "상민": 2_000,
    "중인": 4_000,
    "양반": 7_000,
    "영의정": 12_000,
    "왕": 0,
}

# Rank shop prices
BUY_PRICE: dict[str, int] = {
    "상민": 50_000,
    "중인": 200_000,
    "양반": 800_000,
    "영의정": 0,  # handled separately by exam/king control
}

SELL_PRICE: dict[str, int] = {
    "상민": 25_000,
    "중인": 100_000,
    "양반": 400_000,
    "영의정": 0,
}

# Exam fee by current rank
EXAM_FEE: dict[str, int] = {
    "천민": 10_000,
    "상민": 20_000,
    "중인": 40_000,
    "양반": 80_000,
    "영의정": 0,
    "왕": 0,
}

# Promotion chance by current rank when taking the exam
EXAM_SUCCESS_RATE: dict[str, float] = {
    "천민": 0.45,
    "상민": 0.40,
    "중인": 0.35,
    "양반": 0.25,
}

DEFAULT_TAX_RATE = 10  # percent
DEFAULT_BALANCE = 0
DEFAULT_RANK = "상민"

# Discord role names to sync if present in the server
ROLE_NAMES = {rank: rank for rank in RANKS}


# -----------------------------
# Storage
# -----------------------------


def now_kst() -> datetime:
    return datetime.now(KST)


def today_key(dt: datetime | None = None) -> str:
    dt = dt or now_kst()
    return dt.strftime("%Y-%m-%d")


def current_day_index(dt: datetime | None = None) -> int:
    dt = dt or now_kst()
    return dt.toordinal()


def clamp_money(value: int) -> int:
    return max(0, int(value))


@dataclass
class MemberData:
    rank: str = DEFAULT_RANK
    balance: int = DEFAULT_BALANCE
    last_work_day: str = ""
    last_living_day: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MemberData":
        return cls(
            rank=raw.get("rank", DEFAULT_RANK),
            balance=int(raw.get("balance", DEFAULT_BALANCE)),
            last_work_day=raw.get("last_work_day", ""),
            last_living_day=raw.get("last_living_day", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "balance": self.balance,
            "last_work_day": self.last_work_day,
            "last_living_day": self.last_living_day,
        }


class EconomyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.data: dict[str, Any] = {
            "guilds": {},
        }
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
    return RANKS.index(rank) if rank in RANKS else 0


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


def living_cost_for(rank: str) -> int:
    return LIVING_COST.get(rank, 0)


def work_pay_for(rank: str) -> int:
    return WORK_PAY.get(rank, 0)


def exam_fee_for(rank: str) -> int:
    return EXAM_FEE.get(rank, 0)


def success_rate_for(rank: str) -> float:
    return EXAM_SUCCESS_RATE.get(rank, 0.0)


def buy_price_for(rank: str) -> int:
    return BUY_PRICE.get(rank, 0)


def sell_price_for(rank: str) -> int:
    return SELL_PRICE.get(rank, 0)


async def sync_discord_rank_role(member: discord.Member, new_rank: str) -> None:
    """Synchronize actual Discord roles if the server has same-named roles.

    This is optional: the bot will still work if the roles do not exist.
    """
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


async def ensure_registered(guild_id: int, user_id: int) -> MemberData:
    member = store.get_member(guild_id, user_id)
    if member.rank not in RANKS:
        member.rank = DEFAULT_RANK
    member.balance = clamp_money(member.balance)
    store.set_member(guild_id, user_id, member)
    await store.save()
    return member


async def apply_daily_living_cost(guild: discord.Guild, member: discord.Member) -> tuple[bool, int, int]:
    """Apply one day's living cost once per KST day.

    Returns: (applied, deducted_amount, remaining_balance)
    """
    if guild is None:
        return False, 0, 0

    data = store.get_member(guild.id, member.id)
    today = today_key()
    if data.last_living_day == today:
        return False, 0, data.balance

    cost = living_cost_for(data.rank)
    deducted = min(max(data.balance, 0), max(cost, 0))
    data.balance = clamp_money(data.balance - deducted)
    data.last_living_day = today
    store.set_member(guild.id, member.id, data)
    await store.save()
    return True, deducted, data.balance


async def apply_daily_living_cost_to_guild(guild: discord.Guild) -> int:
    changed = 0
    guild_data = store.ensure_guild(guild.id)
    for user_id_str in list(guild_data["members"].keys()):
        user_id = int(user_id_str)
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        applied, _, _ = await apply_daily_living_cost(guild, member)
        if applied:
            changed += 1
    return changed


# -----------------------------
# Discord bot
# -----------------------------

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if not daily_living_loop.is_running():
        daily_living_loop.start()

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


@tasks.loop(hours=1)
async def daily_living_loop() -> None:
    """Backstop for daily living cost.

    If the bot stayed online, this ensures everyone gets charged once a day.
    """
    for guild in bot.guilds:
        try:
            await apply_daily_living_cost_to_guild(guild)
        except Exception as exc:
            print(f"Daily living cost loop error in guild {guild.id}: {exc}")


async def get_interaction_member_data(interaction: discord.Interaction) -> MemberData:
    if interaction.guild is None or interaction.user is None:
        raise ValueError("Guild only")
    return await ensure_registered(interaction.guild.id, interaction.user.id)


async def require_guild_and_member(interaction: discord.Interaction) -> tuple[discord.Guild, discord.Member, MemberData]:
    if interaction.guild is None:
        raise app_commands.CheckFailure("이 명령어는 서버에서만 사용할 수 있습니다.")
    if not isinstance(interaction.user, discord.Member):
        raise app_commands.CheckFailure("서버 멤버만 사용할 수 있습니다.")
    data = await ensure_registered(interaction.guild.id, interaction.user.id)
    return interaction.guild, interaction.user, data


async def king_only(interaction: discord.Interaction) -> tuple[discord.Guild, discord.Member, MemberData]:
    guild, member, data = await require_guild_and_member(interaction)
    if not is_king(data):
        raise app_commands.CheckFailure("왕만 사용할 수 있습니다.")
    return guild, member, data


# -----------------------------
# Commands
# -----------------------------


@bot.tree.command(name="내정보", description="내 신분과 자산을 확인합니다.")
@app_commands.describe(target="확인할 사용자")
async def myinfo(interaction: discord.Interaction, target: discord.Member | None = None) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return

    target = target or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.response.send_message("서버 멤버만 확인할 수 있습니다.", ephemeral=True)
        return

    await apply_daily_living_cost(interaction.guild, target)
    data = await ensure_registered(interaction.guild.id, target.id)
    tax_rate = store.get_tax_rate(interaction.guild.id)
    treasury = store.get_treasury(interaction.guild.id)
    msg = (
        f"**{target.display_name}**\n"
        f"신분: **{data.rank}**\n"
        f"잔액: **{data.balance:,}원**\n"
        f"오늘 일하기 가능 여부: **{'가능' if can_work(data.rank) else '불가'}**\n"
        f"서버 세율: **{tax_rate}%**\n"
        f"국고: **{treasury:,}원**"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="일하기", description="하루에 한 번 일해서 돈을 벌 수 있습니다.")
async def work(interaction: discord.Interaction) -> None:
    guild, member, data = await require_guild_and_member(interaction)
    await apply_daily_living_cost(guild, member)
    data = store.get_member(guild.id, member.id)

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


@bot.tree.command(name="세금설정", description="왕이 세율을 설정합니다.")
@app_commands.describe(rate="0~100 사이의 세율(%)")
async def tax_set(interaction: discord.Interaction, rate: app_commands.Range[int, 0, 100]) -> None:
    guild, _, _ = await king_only(interaction)
    store.set_tax_rate(guild.id, int(rate))
    await store.save()
    await interaction.response.send_message(f"세율이 **{rate}%**로 설정되었습니다.", ephemeral=False)


@bot.tree.command(name="국고보기", description="국고를 확인합니다.")
async def treasury_view(interaction: discord.Interaction) -> None:
    _, _, data = await require_guild_and_member(interaction)
    if data.rank not in {"왕", "영의정"}:
        await interaction.response.send_message("국고는 왕 또는 영의정만 확인할 수 있습니다.", ephemeral=True)
        return
    treasury = store.get_treasury(interaction.guild.id)
    tax_rate = store.get_tax_rate(interaction.guild.id)
    await interaction.response.send_message(
        f"국고: **{treasury:,}원**\n세율: **{tax_rate}%**",
        ephemeral=True,
    )


@bot.tree.command(name="돈관리", description="왕이 특정 사용자의 돈을 직접 조정합니다.")
@app_commands.describe(
    member="대상 사용자",
    amount="금액",
    mode="지급이면 대상에게 주고, 차감이면 대상에서 빼서 국고로 보냅니다.",
    reason="사유",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="지급", value="give"),
        app_commands.Choice(name="차감", value="take"),
    ]
)
async def money_manage(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: app_commands.Range[int, 1, 10_000_000_000],
    mode: app_commands.Choice[str],
    reason: str,
) -> None:
    guild, _, _ = await king_only(interaction)
    target = await ensure_registered(guild.id, member.id)

    if mode.value == "give":
        if not store.remove_treasury(guild.id, int(amount)):
            await interaction.response.send_message("국고 잔액이 부족합니다.", ephemeral=True)
            return
        target.balance += int(amount)
        action_text = "지급"
    else:
        deducted = min(target.balance, int(amount))
        target.balance -= deducted
        store.add_treasury(guild.id, deducted)
        action_text = "차감"

    store.set_member(guild.id, member.id, target)
    await store.save()
    await interaction.response.send_message(
        f"{member.mention}의 돈을 **{action_text}**했습니다.\n"
        f"금액: **{amount:,}원**\n사유: {reason}",
        ephemeral=False,
    )
    
# -----------------------------
# Error handling
# -----------------------------


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    message = ""
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
        await ensure_registered(member.guild.id, member.id)


@bot.event
async def on_message(message: discord.Message) -> None:
    # Allow command processing if prefix commands are added later.
    await bot.process_commands(message)


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN 환경변수가 설정되지 않았습니다.")

    await warm_up_existing_guilds()
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
