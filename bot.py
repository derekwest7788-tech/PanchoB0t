import os, sqlite3, re
import secrets
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    import psycopg
except ImportError:
    psycopg = None

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "1327705127582171240"))
WINNING_SLIPS_CHANNEL_ID = int(os.getenv("WINNING_SLIPS_CHANNEL_ID", "1327718864888533064"))
COMMUNITY_SLIPS_CHANNEL_ID = int(os.getenv("COMMUNITY_SLIPS_CHANNEL_ID", "1554981496853635162"))
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TIMEZONE = ZoneInfo(os.getenv("BOT_TIMEZONE", "America/Chicago"))

# Natural-language VIP management. If VIP_ROLE_ID is left at 0, the bot finds
# the role by the exact VIP_ROLE_NAME (case-insensitive).
VIP_ROLE_ID = int(os.getenv("VIP_ROLE_ID", "0") or "0")
VIP_ROLE_NAME = (os.getenv("VIP_ROLE_NAME", "VIP") or "VIP").strip()

# Natural-language "gulag" timeout command. Change this Railway variable to alter the default.
GULAG_TIMEOUT_MINUTES = int(os.getenv("GULAG_TIMEOUT_MINUTES", "10"))
DISCORD_MAX_TIMEOUT = timedelta(days=28)

WIN_EMOJI = "✅"
LOSS_EMOJI = "❌"
IMAGE_EXTENSIONS = (".png",".jpg",".jpeg",".gif",".webp",".bmp",".heic",".heif")


def local_now():
    return datetime.now(BOT_TIMEZONE)

def to_utc(dt):
    return dt.astimezone(timezone.utc)

def is_image_message(message):
    return any(
        (a.content_type or "").lower().startswith("image/")
        or a.filename.lower().endswith(IMAGE_EXTENSIONS)
        for a in message.attachments
    )


def is_image_attachment(attachment: discord.Attachment) -> bool:
    """Return True when an uploaded attachment is an image we allow in Community Slips."""
    content_type = (attachment.content_type or "").lower()
    filename = attachment.filename.lower()
    return content_type.startswith("image/") or filename.endswith(IMAGE_EXTENSIONS)


def community_slips_staff_exempt(message: discord.Message) -> bool:
    """Allow staff to post instructions or moderation messages in the image-only channel."""
    author = message.author
    return isinstance(author, discord.Member) and (
        author.guild_permissions.manage_messages
        or author.guild_permissions.administrator
    )


async def enforce_community_slips(message: discord.Message) -> bool:
    """
    Keep Community Slips image-only. Multiple images and an optional caption are allowed.
    Returns True when the message may remain, False when it was removed.
    """
    if message.channel.id != COMMUNITY_SLIPS_CHANNEL_ID:
        return True

    if community_slips_staff_exempt(message):
        return True

    # Members must attach at least one file, and every attached file must be an image.
    valid = bool(message.attachments) and all(
        is_image_attachment(attachment) for attachment in message.attachments
    )
    if valid:
        return True

    try:
        await message.delete()
    except (discord.Forbidden, discord.HTTPException):
        return False

    try:
        warning = await message.channel.send(
            f"{message.author.mention} 📸 **Community Slips is image-only.** "
            "Post one or more slip images; a caption is okay.",
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False
            ),
        )
        await warning.delete(delay=5)
    except (discord.Forbidden, discord.HTTPException):
        pass

    return False


def detect_pick_result(content: str):
    w = WIN_EMOJI in content
    l = LOSS_EMOJI in content
    if w and not l:
        return "W"
    if l and not w:
        return "L"
    return None

def day_range(days_ago=0):
    """Return a full Central Time calendar day as a UTC [start, end) range."""
    target = local_now().date() - timedelta(days=days_ago)
    next_day = target + timedelta(days=1)

    start_local = datetime(
        target.year, target.month, target.day,
        tzinfo=BOT_TIMEZONE,
    )
    end_local = datetime(
        next_day.year, next_day.month, next_day.day,
        tzinfo=BOT_TIMEZONE,
    )

    return to_utc(start_local), to_utc(end_local)


def today_range():
    return day_range(0)


def yesterday_range():
    return day_range(1)

def current_week_range():
    now = local_now()
    s = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return to_utc(s), to_utc(s + timedelta(days=7))

def last_week_range():
    cur, _ = current_week_range()
    s = cur.astimezone(BOT_TIMEZONE) - timedelta(days=7)
    return to_utc(s), cur

def month_range(year, month):
    s = datetime(year, month, 1, tzinfo=BOT_TIMEZONE)
    e = datetime(year + 1, 1, 1, tzinfo=BOT_TIMEZONE) if month == 12 else datetime(year, month + 1, 1, tzinfo=BOT_TIMEZONE)
    return to_utc(s), to_utc(e)

def current_month_range():
    n = local_now()
    return month_range(n.year, n.month)

def previous_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)

def last_month_range():
    n = local_now()
    y, m = previous_month(n.year, n.month)
    return month_range(y, m)

def year_range(year):
    return to_utc(datetime(year,1,1,tzinfo=BOT_TIMEZONE)), to_utc(datetime(year+1,1,1,tzinfo=BOT_TIMEZONE))

def period_range(period):
    return {
        "today": today_range,
        "yesterday": yesterday_range,
        "weekly": current_week_range,
        "last_week": last_week_range,
        "monthly": current_month_range,
        "last_month": last_month_range,
    }.get(period, lambda: (None, None))()

def period_name(period):
    return {
        "today":"Today",
        "yesterday":"Yesterday",
        "weekly":"This Week",
        "last_week":"Last Week",
        "monthly":"This Month",
        "last_month":"Last Month",
        "alltime":"All Time",
    }.get(period, period)

def month_label(year, month):
    return datetime(year, month, 1, tzinfo=BOT_TIMEZONE).strftime("%B %Y")


DURATION_RE = re.compile(r"(\d+)\s*(mo|[smhdwy])", re.IGNORECASE)
DURATION_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
    # Month/year are fixed-length subscription helpers, not calendar arithmetic.
    "mo": 30 * 24 * 60 * 60,
    "y": 365 * 24 * 60 * 60,
}


def parse_duration(value: str) -> timedelta:
    """Parse values like 30d, 1mo, 1w2d, 12h30m, or 1y."""
    compact = re.sub(r"\s+", "", value.lower())
    if not compact:
        raise ValueError("Duration is empty.")

    pos = 0
    total = 0
    for match in DURATION_RE.finditer(compact):
        if match.start() != pos:
            raise ValueError("Invalid duration format.")
        amount = int(match.group(1))
        unit = match.group(2).lower()
        total += amount * DURATION_SECONDS[unit]
        pos = match.end()

    if pos != len(compact) or total <= 0:
        raise ValueError("Invalid duration format.")

    # Keep accidental timers from spanning absurd lengths.
    if total > 5 * 365 * 24 * 60 * 60:
        raise ValueError("Duration cannot exceed 5 years.")

    return timedelta(seconds=total)


def parse_db_datetime(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def human_remaining(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours and len(parts) < 2:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def human_duration_full(delta: timedelta) -> str:
    """Readable original membership length such as 7 days or 30 days."""
    seconds = max(0, int(round(delta.total_seconds())))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes and not days:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts[:2]) if parts else "under 1 minute"


def duration_matches(actual: timedelta, wanted: timedelta, tolerance_seconds: int = 300) -> bool:
    """Match reconstructed Carl-bot grants while allowing a tiny logging delay."""
    return abs(actual.total_seconds() - wanted.total_seconds()) <= tolerance_seconds


def carl_log_text(message: discord.Message) -> str:
    """Flatten a Carl-bot log message/embeds into searchable text."""
    parts = [message.content or ""]
    for embed in message.embeds:
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        if embed.author and embed.author.name:
            parts.append(embed.author.name)
        if embed.footer and embed.footer.text:
            parts.append(embed.footer.text)
        for field in embed.fields:
            if field.name:
                parts.append(field.name)
            if field.value:
                parts.append(field.value)
    return "\n".join(parts)


def duration_from_log_text(text: str) -> Optional[timedelta]:
    """Extract a human/shorthand duration from a Carl-bot log line."""
    lowered = text.lower()

    # Common shorthand: 30d, 1mo, 1w2d, 12h30m, etc.
    shorthand = re.compile(r"(?<![a-z0-9])((?:\d+\s*(?:mo|[smhdwy])\s*)+)(?![a-z])", re.I)
    candidates = []
    for match in shorthand.finditer(lowered):
        raw = match.group(1)
        try:
            candidates.append(parse_duration(raw))
        except ValueError:
            pass

    # Natural wording: 30 days, 2 weeks, 1 month, 6 hours, etc.
    word_units = {
        "second": 1, "seconds": 1, "sec": 1, "secs": 1,
        "minute": 60, "minutes": 60, "min": 60, "mins": 60,
        "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
        "day": 86400, "days": 86400,
        "week": 604800, "weeks": 604800,
        "month": 2592000, "months": 2592000,
        "year": 31536000, "years": 31536000,
    }
    natural_matches = list(re.finditer(
        r"(?<!\d)(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)(?![a-z])",
        lowered,
        re.I,
    ))
    if natural_matches:
        total = 0
        for match in natural_matches:
            amount = int(match.group(1))
            unit = match.group(2).lower()
            total += amount * word_units[unit]
        if total > 0:
            candidates.append(timedelta(seconds=total))

    if not candidates:
        return None

    # Usually there is only one. Prefer the largest sensible duration when a log
    # contains both a compact and a verbose representation of the same timer.
    return max(candidates, key=lambda d: d.total_seconds())


def expiry_from_carl_log(message: discord.Message, text: str) -> Optional[datetime]:
    """Reconstruct expiration from an exact Discord timestamp or log time + duration."""
    assigned_at = message.created_at.astimezone(timezone.utc)

    # If Carl logged an explicit Discord timestamp, that is more accurate than
    # recomputing from a duration. Prefer a future timestamp relative to assignment.
    timestamps = []
    for raw in re.findall(r"<t:(\d{9,12})(?::[A-Za-z])?>", text):
        try:
            dt = datetime.fromtimestamp(int(raw), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            continue
        if dt > assigned_at:
            timestamps.append(dt)
    if timestamps:
        return max(timestamps)

    duration = duration_from_log_text(text)
    if duration is None:
        return None
    return assigned_at + duration


def member_id_from_carl_log(text: str) -> Optional[int]:
    """Extract the target member ID from a Carl-bot log message."""
    # Prefer a mention next to an explicit Member/User label so a moderator
    # mention elsewhere in the embed is not mistaken for the temp-role target.
    labelled_mention = re.search(
        r"(?:member|user)[^\n]{0,100}<@!?(\d{15,22})>",
        text,
        re.I,
    )
    if labelled_mention:
        return int(labelled_mention.group(1))

    labelled_id = re.search(
        r"(?:member|user)(?:\s+id)?\s*[:#-]?\s*`?(\d{15,22})`?",
        text,
        re.I,
    )
    if labelled_id:
        return int(labelled_id.group(1))

    mention = re.search(r"<@!?(\d{15,22})>", text)
    if mention:
        return int(mention.group(1))
    return None


class Database:
    def __init__(self):
        self.pg = bool(DATABASE_URL)
        if self.pg and psycopg is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed.")
        self.sqlite_path = os.getenv("SQLITE_PATH", "winning_slips.db")

    def connect(self):
        return psycopg.connect(DATABASE_URL) if self.pg else sqlite3.connect(self.sqlite_path)

    def ph(self):
        return "%s" if self.pg else "?"

    def ts(self, dt):
        return dt if self.pg else dt.isoformat()

    def initialize(self):
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                if self.pg:
                    cur.execute("""CREATE TABLE IF NOT EXISTS slip_posts(
                        message_id BIGINT PRIMARY KEY,guild_id BIGINT NOT NULL,channel_id BIGINT NOT NULL,
                        user_id BIGINT NOT NULL,posted_at TIMESTAMPTZ NOT NULL)""")
                    cur.execute("""CREATE TABLE IF NOT EXISTS pick_results(
                        message_id BIGINT PRIMARY KEY,guild_id BIGINT NOT NULL,channel_id BIGINT NOT NULL,
                        user_id BIGINT NOT NULL,result TEXT NOT NULL,posted_at TIMESTAMPTZ NOT NULL)""")
                    cur.execute("""CREATE TABLE IF NOT EXISTS temp_roles(
                        guild_id BIGINT NOT NULL,user_id BIGINT NOT NULL,role_id BIGINT NOT NULL,
                        assigned_at TIMESTAMPTZ NOT NULL,expires_at TIMESTAMPTZ NOT NULL,
                        assigned_by BIGINT NOT NULL,source TEXT NOT NULL DEFAULT 'self',
                        PRIMARY KEY(guild_id,user_id,role_id))""")
                    cur.execute("""CREATE TABLE IF NOT EXISTS temp_role_history(
                        event_key TEXT PRIMARY KEY,guild_id BIGINT NOT NULL,user_id BIGINT NOT NULL,
                        role_id BIGINT NOT NULL,assigned_at TIMESTAMPTZ NOT NULL,expires_at TIMESTAMPTZ NOT NULL,
                        assigned_by BIGINT NOT NULL,source TEXT NOT NULL DEFAULT 'self',
                        log_channel_id BIGINT,log_message_id BIGINT)""")
                else:
                    cur.execute("""CREATE TABLE IF NOT EXISTS slip_posts(
                        message_id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,posted_at TEXT NOT NULL)""")
                    cur.execute("""CREATE TABLE IF NOT EXISTS pick_results(
                        message_id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,result TEXT NOT NULL,posted_at TEXT NOT NULL)""")
                    cur.execute("""CREATE TABLE IF NOT EXISTS temp_roles(
                        guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,role_id INTEGER NOT NULL,
                        assigned_at TEXT NOT NULL,expires_at TEXT NOT NULL,
                        assigned_by INTEGER NOT NULL,source TEXT NOT NULL DEFAULT 'self',
                        PRIMARY KEY(guild_id,user_id,role_id))""")
                    cur.execute("""CREATE TABLE IF NOT EXISTS temp_role_history(
                        event_key TEXT PRIMARY KEY,guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
                        role_id INTEGER NOT NULL,assigned_at TEXT NOT NULL,expires_at TEXT NOT NULL,
                        assigned_by INTEGER NOT NULL,source TEXT NOT NULL DEFAULT 'self',
                        log_channel_id INTEGER,log_message_id INTEGER)""")
            conn.commit()

    def add_slip(self, message):
        dt = message.created_at.astimezone(timezone.utc)
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                if self.pg:
                    cur.execute("""INSERT INTO slip_posts VALUES(%s,%s,%s,%s,%s)
                                   ON CONFLICT(message_id) DO NOTHING""",
                                (message.id,message.guild.id,message.channel.id,message.author.id,dt))
                else:
                    cur.execute("""INSERT OR IGNORE INTO slip_posts VALUES(?,?,?,?,?)""",
                                (message.id,message.guild.id,message.channel.id,message.author.id,dt.isoformat()))
                ok = cur.rowcount > 0
            conn.commit()
        return ok

    def remove_slip(self, message_id):
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(f"DELETE FROM slip_posts WHERE message_id={self.ph()}", (message_id,))
            conn.commit()

    def slip_counts(self, start=None, end=None, user_id=None, limit=None):
        p = self.ph()
        filters = [f"guild_id={p}", f"channel_id={p}"]
        vals = [GUILD_ID, WINNING_SLIPS_CHANNEL_ID]
        if user_id is not None:
            filters.append(f"user_id={p}"); vals.append(user_id)
        if start is not None:
            filters.append(f"posted_at>={p}"); vals.append(self.ts(start))
        if end is not None:
            filters.append(f"posted_at<{p}"); vals.append(self.ts(end))
        sql = f"""SELECT user_id,COUNT(*) total FROM slip_posts
                  WHERE {' AND '.join(filters)}
                  GROUP BY user_id ORDER BY total DESC,user_id ASC"""
        if limit is not None:
            sql += f" LIMIT {p}"; vals.append(limit)
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(sql, tuple(vals))
                return cur.fetchall()

    def first_slip(self):
        p = self.ph()
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(f"""SELECT MIN(posted_at) FROM slip_posts
                                WHERE guild_id={p} AND channel_id={p}""",
                            (GUILD_ID, WINNING_SLIPS_CHANNEL_ID))
                v = cur.fetchone()[0]
        if v is None:
            return None
        return v.astimezone(timezone.utc) if isinstance(v, datetime) else datetime.fromisoformat(v).astimezone(timezone.utc)

    def upsert_result(self, message, result):
        dt = message.created_at.astimezone(timezone.utc)
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                if self.pg:
                    cur.execute("""INSERT INTO pick_results(message_id,guild_id,channel_id,user_id,result,posted_at)
                                   VALUES(%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT(message_id) DO UPDATE SET
                                   guild_id=EXCLUDED.guild_id,channel_id=EXCLUDED.channel_id,
                                   user_id=EXCLUDED.user_id,result=EXCLUDED.result,posted_at=EXCLUDED.posted_at""",
                                (message.id,message.guild.id,message.channel.id,message.author.id,result,dt))
                else:
                    cur.execute("""INSERT INTO pick_results(message_id,guild_id,channel_id,user_id,result,posted_at)
                                   VALUES(?,?,?,?,?,?)
                                   ON CONFLICT(message_id) DO UPDATE SET
                                   guild_id=excluded.guild_id,channel_id=excluded.channel_id,
                                   user_id=excluded.user_id,result=excluded.result,posted_at=excluded.posted_at""",
                                (message.id,message.guild.id,message.channel.id,message.author.id,result,dt.isoformat()))
            conn.commit()

    def remove_result(self, message_id):
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(f"DELETE FROM pick_results WHERE message_id={self.ph()}", (message_id,))
            conn.commit()

    def record(self, user_id, channel_id=None, start=None, end=None):
        p = self.ph()
        filters = [f"guild_id={p}", f"user_id={p}"]
        vals = [GUILD_ID, user_id]
        if channel_id is not None:
            filters.append(f"channel_id={p}"); vals.append(channel_id)
        if start is not None:
            filters.append(f"posted_at>={p}"); vals.append(self.ts(start))
        if end is not None:
            filters.append(f"posted_at<{p}"); vals.append(self.ts(end))
        sql = f"""SELECT
                    SUM(CASE WHEN result='W' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN result='L' THEN 1 ELSE 0 END)
                  FROM pick_results WHERE {' AND '.join(filters)}"""
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(sql, tuple(vals))
                w,l = cur.fetchone()
        return int(w or 0), int(l or 0)

    def user_results(self, user_id, channel_id=None, start=None, end=None):
        p = self.ph()
        filters = [f"guild_id={p}", f"user_id={p}"]
        vals = [GUILD_ID, user_id]
        if channel_id is not None:
            filters.append(f"channel_id={p}"); vals.append(channel_id)
        if start is not None:
            filters.append(f"posted_at>={p}"); vals.append(self.ts(start))
        if end is not None:
            filters.append(f"posted_at<{p}"); vals.append(self.ts(end))
        sql = f"SELECT result,posted_at FROM pick_results WHERE {' AND '.join(filters)} ORDER BY posted_at ASC"
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(sql, tuple(vals))
                return cur.fetchall()

    def primary_capper(self, channel_id, start=None, end=None):
        p = self.ph()
        filters = [f"guild_id={p}", f"channel_id={p}"]
        vals = [GUILD_ID, channel_id]
        if start is not None:
            filters.append(f"posted_at>={p}"); vals.append(self.ts(start))
        if end is not None:
            filters.append(f"posted_at<{p}"); vals.append(self.ts(end))
        sql = f"""SELECT user_id,COUNT(*) c FROM pick_results
                  WHERE {' AND '.join(filters)}
                  GROUP BY user_id ORDER BY c DESC,user_id ASC LIMIT 1"""
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(sql, tuple(vals))
                row = cur.fetchone()
        return int(row[0]) if row else None

    def record_leaderboard(self, channel_id=None, start=None, end=None, minimum_picks=3, limit=10):
        p = self.ph()
        filters = [f"guild_id={p}"]
        vals = [GUILD_ID]
        if channel_id is not None:
            filters.append(f"channel_id={p}"); vals.append(channel_id)
        if start is not None:
            filters.append(f"posted_at>={p}"); vals.append(self.ts(start))
        if end is not None:
            filters.append(f"posted_at<{p}"); vals.append(self.ts(end))
        sql = f"""SELECT user_id,
                    SUM(CASE WHEN result='W' THEN 1 ELSE 0 END) wins,
                    SUM(CASE WHEN result='L' THEN 1 ELSE 0 END) losses,
                    COUNT(*) picks
                  FROM pick_results
                  WHERE {' AND '.join(filters)}
                  GROUP BY user_id
                  HAVING COUNT(*) >= {p}
                  ORDER BY
                    (1.0*SUM(CASE WHEN result='W' THEN 1 ELSE 0 END)/COUNT(*)) DESC,
                    COUNT(*) DESC,wins DESC,user_id ASC
                  LIMIT {p}"""
        vals.extend([minimum_picks, limit])
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(sql, tuple(vals))
                return cur.fetchall()


    def upsert_temp_role(self, guild_id, user_id, role_id, assigned_at, expires_at, assigned_by, source="self"):
        assigned_at = assigned_at.astimezone(timezone.utc)
        expires_at = expires_at.astimezone(timezone.utc)
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                if self.pg:
                    cur.execute("""INSERT INTO temp_roles(
                                   guild_id,user_id,role_id,assigned_at,expires_at,assigned_by,source)
                                   VALUES(%s,%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT(guild_id,user_id,role_id) DO UPDATE SET
                                   assigned_at=EXCLUDED.assigned_at,expires_at=EXCLUDED.expires_at,
                                   assigned_by=EXCLUDED.assigned_by,source=EXCLUDED.source""",
                                (guild_id,user_id,role_id,assigned_at,expires_at,assigned_by,source))
                else:
                    cur.execute("""INSERT INTO temp_roles(
                                   guild_id,user_id,role_id,assigned_at,expires_at,assigned_by,source)
                                   VALUES(?,?,?,?,?,?,?)
                                   ON CONFLICT(guild_id,user_id,role_id) DO UPDATE SET
                                   assigned_at=excluded.assigned_at,expires_at=excluded.expires_at,
                                   assigned_by=excluded.assigned_by,source=excluded.source""",
                                (guild_id,user_id,role_id,assigned_at.isoformat(),expires_at.isoformat(),assigned_by,source))
            conn.commit()

    def add_temp_role_history(
        self, event_key, guild_id, user_id, role_id, assigned_at, expires_at,
        assigned_by, source="self", log_channel_id=None, log_message_id=None
    ):
        """Persist one temp-role grant forever. Safe to call repeatedly for backfills."""
        assigned_at = assigned_at.astimezone(timezone.utc)
        expires_at = expires_at.astimezone(timezone.utc)
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                if self.pg:
                    cur.execute(
                        """INSERT INTO temp_role_history(
                           event_key,guild_id,user_id,role_id,assigned_at,expires_at,
                           assigned_by,source,log_channel_id,log_message_id)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(event_key) DO UPDATE SET
                           guild_id=EXCLUDED.guild_id,user_id=EXCLUDED.user_id,role_id=EXCLUDED.role_id,
                           assigned_at=EXCLUDED.assigned_at,expires_at=EXCLUDED.expires_at,
                           assigned_by=EXCLUDED.assigned_by,source=EXCLUDED.source,
                           log_channel_id=EXCLUDED.log_channel_id,log_message_id=EXCLUDED.log_message_id""",
                        (event_key,guild_id,user_id,role_id,assigned_at,expires_at,assigned_by,
                         source,log_channel_id,log_message_id),
                    )
                else:
                    cur.execute(
                        """INSERT INTO temp_role_history(
                           event_key,guild_id,user_id,role_id,assigned_at,expires_at,
                           assigned_by,source,log_channel_id,log_message_id)
                           VALUES(?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(event_key) DO UPDATE SET
                           guild_id=excluded.guild_id,user_id=excluded.user_id,role_id=excluded.role_id,
                           assigned_at=excluded.assigned_at,expires_at=excluded.expires_at,
                           assigned_by=excluded.assigned_by,source=excluded.source,
                           log_channel_id=excluded.log_channel_id,log_message_id=excluded.log_message_id""",
                        (event_key,guild_id,user_id,role_id,assigned_at.isoformat(),expires_at.isoformat(),
                         assigned_by,source,log_channel_id,log_message_id),
                    )
            conn.commit()

    def temp_role_history(self, guild_id, user_id=None, role_id=None, limit=1000):
        p = self.ph()
        filters = [f"guild_id={p}"]
        vals = [guild_id]
        if user_id is not None:
            filters.append(f"user_id={p}")
            vals.append(user_id)
        if role_id is not None:
            filters.append(f"role_id={p}")
            vals.append(role_id)
        sql = f"""SELECT event_key,user_id,role_id,assigned_at,expires_at,assigned_by,source,
                         log_channel_id,log_message_id
                  FROM temp_role_history WHERE {' AND '.join(filters)}
                  ORDER BY assigned_at DESC LIMIT {p}"""
        vals.append(limit)
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(sql, tuple(vals))
                return cur.fetchall()

    def remove_temp_role(self, guild_id, user_id, role_id):
        p = self.ph()
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    f"DELETE FROM temp_roles WHERE guild_id={p} AND user_id={p} AND role_id={p}",
                    (guild_id,user_id,role_id),
                )
            conn.commit()

    def temp_roles(self, guild_id, user_id=None, role_id=None, active_only=True):
        p = self.ph()
        filters = [f"guild_id={p}"]
        vals = [guild_id]
        if user_id is not None:
            filters.append(f"user_id={p}")
            vals.append(user_id)
        if role_id is not None:
            filters.append(f"role_id={p}")
            vals.append(role_id)
        if active_only:
            filters.append(f"expires_at>{p}")
            vals.append(self.ts(datetime.now(timezone.utc)))
        sql = f"""SELECT user_id,role_id,assigned_at,expires_at,assigned_by,source
                  FROM temp_roles WHERE {' AND '.join(filters)}
                  ORDER BY expires_at ASC,user_id ASC"""
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(sql, tuple(vals))
                return cur.fetchall()

    def update_latest_temp_role_history(self, guild_id, user_id, role_id, expires_at, assigned_by=None, source=None):
        """Update the newest history record for a member+role after an extension/takeover."""
        expires_at = expires_at.astimezone(timezone.utc)
        p = self.ph()
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    f"""SELECT event_key FROM temp_role_history
                        WHERE guild_id={p} AND user_id={p} AND role_id={p}
                        ORDER BY assigned_at DESC LIMIT 1""",
                    (guild_id, user_id, role_id),
                )
                row = cur.fetchone()
                if row is None:
                    return False

                updates = [f"expires_at={p}"]
                vals = [self.ts(expires_at)]
                if assigned_by is not None:
                    updates.append(f"assigned_by={p}")
                    vals.append(assigned_by)
                if source is not None:
                    updates.append(f"source={p}")
                    vals.append(source)
                vals.append(row[0])
                cur.execute(
                    f"UPDATE temp_role_history SET {', '.join(updates)} WHERE event_key={p}",
                    tuple(vals),
                )
            conn.commit()
        return True


    def due_temp_roles(self, now=None):
        now = now or datetime.now(timezone.utc)
        p = self.ph()
        with self.connect() as conn:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    f"""SELECT guild_id,user_id,role_id,expires_at,source
                        FROM temp_roles WHERE expires_at<={p}
                        ORDER BY expires_at ASC""",
                    (self.ts(now),),
                )
                return cur.fetchall()


def gulag_duration_from_message(content: str) -> timedelta:
    """Return an optional `for 30m` / `for 2h` duration, otherwise the configured default."""
    match = re.search(
        r"\bfor\s+((?:\d+\s*(?:mo|[smhdwy])\s*)+)",
        content,
        flags=re.IGNORECASE,
    )
    if match:
        duration = parse_duration(match.group(1))
    else:
        duration = timedelta(minutes=max(1, GULAG_TIMEOUT_MINUTES))

    if duration > DISCORD_MAX_TIMEOUT:
        raise ValueError("Discord timeouts cannot be longer than 28 days.")
    return duration


def is_gulag_request(message: discord.Message) -> bool:
    """Recognize natural-language requests such as `@bot send @user to the gulag`."""
    if bot.user is None or bot.user not in message.mentions:
        return False
    text = message.content.lower()
    return bool(
        re.search(r"\b(?:send|put|throw)\b.*\bgulag\b", text)
        or re.search(r"\bgulag\b.*\b(?:send|put|throw)\b", text)
    )


async def handle_gulag_request(message: discord.Message) -> bool:
    """Handle the mention-based gulag command. Returns True when the message matched."""
    if not is_gulag_request(message):
        return False

    guild = message.guild
    requester = message.author
    if not isinstance(requester, discord.Member):
        return True

    if not (requester.guild_permissions.moderate_members or requester.guild_permissions.administrator):
        await message.reply(
            "you need **Timeout Members / Moderate Members** permission to send somebody to the gulag.",
            mention_author=False,
        )
        return True

    targets = [m for m in message.mentions if bot.user is None or m.id != bot.user.id]
    if not targets:
        await message.reply(
            "mention who you want me to send to the gulag too, twin.",
            mention_author=False,
        )
        return True

    target = targets[0]

    if target.id == guild.owner_id:
        await message.reply("I can't send the server owner to the gulag.", mention_author=False)
        return True
    if target.id == requester.id:
        await message.reply("you trying to send yourself to the gulag? 😭", mention_author=False)
        return True
    if bot.user is not None and target.id == bot.user.id:
        await message.reply("nice try twin 😭", mention_author=False)
        return True
    if target.guild_permissions.administrator:
        await message.reply("Discord won't let me timeout an administrator.", mention_author=False)
        return True

    # Respect Discord's moderator hierarchy: non-owners cannot moderate an equal/higher role.
    if requester.id != guild.owner_id and target.top_role >= requester.top_role:
        await message.reply(
            "you can't send someone with an equal or higher role than you to the gulag.",
            mention_author=False,
        )
        return True

    me = guild.me
    if me is None:
        me = guild.get_member(bot.user.id) if bot.user else None
    if me is None or not me.guild_permissions.moderate_members:
        await message.reply(
            "I need the **Timeout Members / Moderate Members** permission first.",
            mention_author=False,
        )
        return True
    if target.top_role >= me.top_role:
        await message.reply(
            "my bot role needs to be above that user's highest role before I can send them to the gulag.",
            mention_author=False,
        )
        return True

    try:
        duration = gulag_duration_from_message(message.content)
    except ValueError as exc:
        await message.reply(str(exc), mention_author=False)
        return True

    try:
        await target.timeout(
            duration,
            reason=f"Gulag command by {requester} ({requester.id})",
        )
    except discord.Forbidden:
        await message.reply(
            "Discord blocked the timeout. Check my **Timeout Members** permission and role position.",
            mention_author=False,
        )
        return True
    except discord.HTTPException as exc:
        await message.reply(f"I couldn't send them to the gulag: `{exc}`", mention_author=False)
        return True

    await message.reply(
        f"got you twin {target.mention} sent to the gulag",
        mention_author=False,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )
    return True


# ---------------- Natural-language VIP management ----------------

NATURAL_DURATION_TOKEN_RE = re.compile(
    r"(\d+)\s*(years?|yrs?|yr|y|months?|mos?|mo|weeks?|wks?|wk|w|days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)\b",
    re.IGNORECASE,
)
NATURAL_DURATION_UNITS = {
    "year": "y", "years": "y", "yr": "y", "yrs": "y", "y": "y",
    "month": "mo", "months": "mo", "mo": "mo", "mos": "mo",
    "week": "w", "weeks": "w", "wk": "w", "wks": "w", "w": "w",
    "day": "d", "days": "d", "d": "d",
    "hour": "h", "hours": "h", "hr": "h", "hrs": "h", "h": "h",
    "minute": "m", "minutes": "m", "min": "m", "mins": "m", "m": "m",
    "second": "s", "seconds": "s", "sec": "s", "secs": "s", "s": "s",
}


def natural_duration_from_message(content: str) -> timedelta:
    """Parse natural phrases such as `7 days`, `1 month`, or `1 month 7 days`."""
    pieces = []
    for amount, raw_unit in NATURAL_DURATION_TOKEN_RE.findall(content):
        unit = NATURAL_DURATION_UNITS[raw_unit.lower()]
        pieces.append(f"{amount}{unit}")
    if not pieces:
        raise ValueError("I couldn't find a duration.")
    return parse_duration("".join(pieces))


def configured_vip_role(guild: discord.Guild, message: Optional[discord.Message] = None):
    """Resolve the one role natural-language VIP commands are allowed to manage."""
    if VIP_ROLE_ID:
        role = guild.get_role(VIP_ROLE_ID)
        if role is not None:
            return role

    wanted = VIP_ROLE_NAME.casefold()
    if message is not None:
        for role in message.role_mentions:
            if role.name.casefold() == wanted:
                return role

    return next((role for role in guild.roles if role.name.casefold() == wanted), None)


def natural_vip_targets(message: discord.Message):
    """Return mentioned members excluding the bot itself."""
    bot_id = bot.user.id if bot.user else None
    return [member for member in message.mentions if member.id != bot_id]


def natural_vip_is_check(text: str) -> bool:
    return bool(
        re.search(r"\bhow\s+long\b", text)
        or re.search(r"\btime\s+left\b", text)
        or re.search(r"\b(?:when\s+does|when\s+will).*\b(?:expire|end)\b", text)
        or re.search(r"\b(?:show|check|what(?:'s|\s+is))\b.*\b(?:expiry|expiration|expires?|remaining|left)\b", text)
        or re.search(r"\b(?:expiry|expiration)\b", text)
    )


def natural_vip_is_bulk_extend(text: str) -> bool:
    everyone = bool(
        re.search(r"\beveryone(?:'s)?\b", text)
        or re.search(r"\beverybody(?:'s)?\b", text)
        or re.search(r"\ball\s+(?:active\s+)?(?:vip|members?|subs?|subscriptions?)\b", text)
    )
    extension = bool(re.search(r"\b(?:extend|renew)\b", text) or re.search(r"\badd\b.*\b(?:to|onto)\b", text))
    return everyone and extension


def natural_vip_request_kind(message: discord.Message):
    """Classify a mention-based subscription request without modifying anything."""
    if bot.user is None or bot.user not in message.mentions:
        return None

    text = message.content.lower()
    role_mentioned = any(
        role.id == VIP_ROLE_ID if VIP_ROLE_ID else role.name.casefold() == VIP_ROLE_NAME.casefold()
        for role in message.role_mentions
    )
    mentions_vip = bool(re.search(r"\bvip\b", text)) or role_mentioned

    if mentions_vip and re.search(r"\bhelp\b", text):
        return "help"
    if natural_vip_is_bulk_extend(text) and mentions_vip:
        return "extendall"
    if re.search(r"\b(?:remove|delete|cancel|revoke|strip)\b", text) and mentions_vip:
        return "remove"
    if natural_vip_is_check(text):
        # Checking defaults to the configured VIP role, so the user can simply ask
        # `@bot how long does @member have left?`.
        return "check"
    if mentions_vip and (
        re.search(r"\b(?:extend|renew)\b", text)
        or re.search(r"\badd\b.*\b(?:to|onto)\b", text)
    ):
        return "extend"
    if mentions_vip and re.search(r"\b(?:give|grant|assign|add)\b", text):
        return "add"
    return None


async def validate_natural_vip_request(message: discord.Message, role: discord.Role) -> bool:
    requester = message.author
    guild = message.guild
    if not isinstance(requester, discord.Member) or guild is None:
        return False

    if not (requester.guild_permissions.manage_roles or requester.guild_permissions.administrator):
        await message.reply(
            "You need the **Manage Roles** permission to manage VIP memberships.",
            mention_author=False,
        )
        return False

    me = guild.me or (guild.get_member(bot.user.id) if bot.user else None)
    if me is None or not me.guild_permissions.manage_roles:
        await message.reply(
            "I need the **Manage Roles** permission before I can manage VIP memberships.",
            mention_author=False,
        )
        return False
    if role.is_default() or role.managed:
        await message.reply("That VIP role can't be managed by me.", mention_author=False)
        return False
    if role >= me.top_role:
        await message.reply(
            f"Move my bot role above {role.mention} in **Server Settings → Roles** first.",
            mention_author=False,
        )
        return False
    return True


async def handle_natural_vip_request(message: discord.Message) -> bool:
    """Handle conversational VIP add/extend/check/remove requests. Returns True if matched."""
    kind = natural_vip_request_kind(message)
    if kind is None:
        return False

    guild = message.guild
    requester = message.author
    if guild is None or not isinstance(requester, discord.Member):
        return True

    role = configured_vip_role(guild, message)
    if role is None:
        await message.reply(
            f"I couldn't find the **{VIP_ROLE_NAME}** role. Rename the role to `{VIP_ROLE_NAME}` "
            "or set `VIP_ROLE_ID` in Railway to the role's Discord ID.",
            mention_author=False,
        )
        return True

    if not await validate_natural_vip_request(message, role):
        return True

    if kind == "help":
        await message.reply(
            "**VIP commands**\n"
            f"• Add: `@{bot.user.name} give @member VIP for 7 days`\n"
            f"• Extend: `@{bot.user.name} add 7 days to @member's VIP`\n"
            f"• Check: `@{bot.user.name} how long does @member have left?`\n"
            f"• Remove: `@{bot.user.name} remove VIP from @member`\n"
            f"• Everyone: `@{bot.user.name} add 7 days to everyone's VIP`",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    targets = natural_vip_targets(message)

    if kind == "extendall":
        try:
            length = natural_duration_from_message(message.content)
        except ValueError:
            await message.reply(
                "Tell me how much time to add, for example: `add 7 days to everyone's VIP`.",
                mention_author=False,
            )
            return True

        rows = db.temp_roles(guild.id, role_id=role.id, active_only=True)
        if not rows:
            await message.reply(
                f"I don't have any active tracked {role.mention} timers to extend.",
                mention_author=False,
            )
            return True

        extended = adopted = stale = unresolved = 0
        for user_id, role_id, assigned_at, expires_at, assigned_by, source in rows:
            member = guild.get_member(int(user_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(user_id))
                except discord.NotFound:
                    db.remove_temp_role(guild.id, int(user_id), int(role_id))
                    stale += 1
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    unresolved += 1
                    continue

            if role not in member.roles:
                db.remove_temp_role(guild.id, int(user_id), int(role_id))
                stale += 1
                continue

            assigned = parse_db_datetime(assigned_at)
            old_expires = parse_db_datetime(expires_at)
            new_expires = old_expires + length
            was_carl = str(source).startswith("carl-")
            new_source = "self-migrated" if was_carl else str(source)

            db.upsert_temp_role(
                guild.id, member.id, role.id, assigned, new_expires,
                requester.id, new_source,
            )
            db.update_latest_temp_role_history(
                guild.id, member.id, role.id, new_expires,
                assigned_by=requester.id, source=new_source,
            )
            extended += 1
            adopted += int(was_carl)

        details = []
        if adopted:
            details.append(f"{adopted} Carl timer(s) moved under my control")
        if stale:
            details.append(f"{stale} stale timer(s) cleaned up")
        if unresolved:
            details.append(f"{unresolved} member(s) couldn't be verified")
        note = f"\n{' • '.join(details)}" if details else ""
        await message.reply(
            f"✅ Added **{human_duration_full(length)}** to **{extended}** active {role.mention} membership(s)."
            f"{note}\nTime was added to each person's existing expiration — nothing was reset from today.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(users=False, roles=True, everyone=False),
        )
        return True

    if len(targets) != 1:
        await message.reply(
            "Mention exactly **one member** for that VIP request.",
            mention_author=False,
        )
        return True

    member = targets[0]

    if kind == "check":
        rows = db.temp_roles(guild.id, user_id=member.id, role_id=role.id, active_only=True)
        if not rows:
            if role in member.roles:
                await message.reply(
                    f"{member.mention} has {role.mention}, but I don't have an active timer stored for it.",
                    mention_author=False,
                )
            else:
                await message.reply(
                    f"{member.mention} does not have an active tracked {role.mention} membership.",
                    mention_author=False,
                )
            return True

        _, _, _, expires_at, _, _ = rows[0]
        expires = parse_db_datetime(expires_at)
        unix = int(expires.timestamp())
        remaining = max(timedelta(0), expires - datetime.now(timezone.utc))
        await message.reply(
            f"⏰ {member.mention}'s {role.mention} expires <t:{unix}:F> • <t:{unix}:R>\n"
            f"Time remaining: **{human_duration_full(remaining)}**.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
        )
        return True

    if kind == "remove":
        existing = db.temp_roles(guild.id, user_id=member.id, role_id=role.id, active_only=False)
        db.remove_temp_role(guild.id, member.id, role.id)
        try:
            if role in member.roles:
                await member.remove_roles(
                    role,
                    reason=f"Natural-language VIP removal by {requester} ({requester.id})",
                )
        except (discord.Forbidden, discord.HTTPException) as exc:
            if existing:
                user_id, role_id, assigned_at, expires_at, assigned_by, source = existing[0]
                db.upsert_temp_role(
                    guild.id, int(user_id), int(role_id),
                    parse_db_datetime(assigned_at), parse_db_datetime(expires_at),
                    int(assigned_by), str(source),
                )
            await message.reply(
                f"Discord wouldn't let me remove {role.mention}: `{exc}`",
                mention_author=False,
            )
            return True

        await message.reply(
            f"✅ Removed {role.mention} from {member.mention} and cancelled the tracked timer.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
        )
        return True

    try:
        length = natural_duration_from_message(message.content)
    except ValueError:
        await message.reply(
            "Tell me the duration too — for example **7 days**, **30 days**, **1 month**, or **1 year**.",
            mention_author=False,
        )
        return True

    if kind == "extend":
        if role not in member.roles:
            await message.reply(
                f"{member.mention} doesn't currently have {role.mention}. Say `give @member VIP for 7 days` instead.",
                mention_author=False,
            )
            return True

        rows = db.temp_roles(guild.id, user_id=member.id, role_id=role.id, active_only=True)
        if not rows:
            await message.reply(
                f"I don't have an active timer for {member.mention}. Say `give @member VIP for 7 days` to start one.",
                mention_author=False,
            )
            return True

        _, _, assigned_at, expires_at, _, source = rows[0]
        assigned = parse_db_datetime(assigned_at)
        old_expires = parse_db_datetime(expires_at)
        new_expires = old_expires + length
        was_carl = str(source).startswith("carl-")
        new_source = "self-migrated" if was_carl else str(source)

        db.upsert_temp_role(
            guild.id, member.id, role.id, assigned, new_expires,
            requester.id, new_source,
        )
        db.update_latest_temp_role_history(
            guild.id, member.id, role.id, new_expires,
            assigned_by=requester.id, source=new_source,
        )

        unix = int(new_expires.timestamp())
        migrated = "\n↪️ That imported Carl timer is now managed by me." if was_carl else ""
        await message.reply(
            f"✅ Added **{human_duration_full(length)}** to {member.mention}'s {role.mention}.\n"
            f"New expiration: <t:{unix}:F> • <t:{unix}:R>{migrated}",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
        )
        return True

    # kind == "add"
    active = db.temp_roles(guild.id, user_id=member.id, role_id=role.id, active_only=True)
    if active:
        _, _, _, expires_at, _, _ = active[0]
        expires = parse_db_datetime(expires_at)
        unix = int(expires.timestamp())
        await message.reply(
            f"{member.mention} already has an active tracked {role.mention} membership ending <t:{unix}:R>.\n"
            f"To add time, say `@{bot.user.name} add 7 days to @member's VIP`.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
        )
        return True

    assigned_at = datetime.now(timezone.utc)
    expires_at = assigned_at + length
    try:
        if role not in member.roles:
            await member.add_roles(
                role,
                reason=f"Natural-language VIP assignment by {requester} ({requester.id})",
            )
    except (discord.Forbidden, discord.HTTPException) as exc:
        await message.reply(
            f"Discord wouldn't let me assign {role.mention}: `{exc}`",
            mention_author=False,
        )
        return True

    db.upsert_temp_role(
        guild.id, member.id, role.id, assigned_at, expires_at,
        requester.id, "self-natural",
    )
    db.add_temp_role_history(
        f"natural:{message.id}", guild.id, member.id, role.id,
        assigned_at, expires_at, requester.id, "self-natural",
    )

    unix = int(expires_at.timestamp())
    await message.reply(
        f"✅ Got you. {member.mention} has been given {role.mention} for **{human_duration_full(length)}**.\n"
        f"Expires <t:{unix}:F> • <t:{unix}:R>",
        mention_author=False,
        allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
    )
    return True


db = Database()


class TrackerBot(commands.Bot):
    async def setup_hook(self):
        db.initialize()
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        print(f"Synced {len(synced)} slash commands to guild {GUILD_ID}")


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = TrackerBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print("Winning Slips + capper record + temp-role tracking active.")
    if not temp_role_cleanup.is_running():
        temp_role_cleanup.start()


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild or message.guild.id != GUILD_ID:
        return

    # Community Slips is separate from Winning Slips. Regular members may post
    # one or multiple image attachments, with an optional caption. Text-only
    # posts and messages containing any non-image attachment are removed.
    if not await enforce_community_slips(message):
        return

    if message.channel.id == WINNING_SLIPS_CHANNEL_ID and is_image_message(message):
        db.add_slip(message)

    result = detect_pick_result(message.content)
    if result:
        db.upsert_result(message, result)

    # Natural-language VIP/subscription management, e.g.
    # "@AmiriPicksBot give @member VIP for 7 days".
    if await handle_natural_vip_request(message):
        return

    # Natural-language moderation: "@AmiriPicks Bot send @user to the gulag".
    if await handle_gulag_request(message):
        return

    await bot.process_commands(message)


@bot.event
async def on_message_edit(before, after):
    if after.author.bot or not after.guild or after.guild.id != GUILD_ID:
        return

    # Re-check edited Community Slips posts too, so a member cannot edit a
    # previously valid post into something that violates the channel rules.
    if not await enforce_community_slips(after):
        return

    if after.channel.id == WINNING_SLIPS_CHANNEL_ID:
        db.remove_slip(after.id)
        if is_image_message(after):
            db.add_slip(after)

    result = detect_pick_result(after.content)
    if result:
        db.upsert_result(after, result)
    else:
        db.remove_result(after.id)


@bot.event
async def on_message_delete(message):
    if not message.guild or message.guild.id != GUILD_ID:
        return
    if message.channel.id == WINNING_SLIPS_CHANNEL_ID:
        db.remove_slip(message.id)
    db.remove_result(message.id)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Handle externally removed roles without losing bot-managed migrated timers."""
    if after.guild.id != GUILD_ID:
        return


    before_ids = {role.id for role in before.roles}
    after_ids = {role.id for role in after.roles}
    removed_ids = before_ids - after_ids
    if not removed_ids:
        return

    now = datetime.now(timezone.utc)
    tracked = db.temp_roles(after.guild.id, user_id=after.id, active_only=False)
    for user_id, role_id, assigned_at, expires_at, assigned_by, source in tracked:
        role_id = int(role_id)
        if role_id not in removed_ids:
            continue

        expires = parse_db_datetime(expires_at)

        # A migrated Carl timer may still exist inside Carl-bot. If Carl removes
        # the role at its old expiration after we extended it, restore the role
        # while our newer bot-managed timer is still valid. Intentional removals
        # should be done with /temprole_remove, which cancels the timer first.
        if str(source) == "self-migrated" and expires > now:
            role = after.guild.get_role(role_id)
            if role is not None:
                try:
                    await after.add_roles(
                        role,
                        reason="Restoring active temp role after external/Carl-bot removal",
                    )
                    print(
                        f"Restored active migrated temp role {role_id} to {after.id}; "
                        f"bot timer expires {expires.isoformat()}"
                    )
                    continue
                except (discord.Forbidden, discord.HTTPException) as exc:
                    print(
                        f"Could not restore active migrated temp role {role_id} "
                        f"to {after.id}: {exc}"
                    )
                    # Keep the timer so the situation remains visible/retryable.
                    continue

        db.remove_temp_role(after.guild.id, after.id, role_id)


@tasks.loop(seconds=30)
async def temp_role_cleanup():
    """Remove expired roles and clear their persisted timer rows."""
    for guild_id, user_id, role_id, expires_at, source in db.due_temp_roles():
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            continue

        member = guild.get_member(int(user_id))
        role = guild.get_role(int(role_id))

        # Imported Carl-bot timers are tracking-only. Carl-bot remains responsible
        # for removing those roles; timers created by this bot are removed here.
        if not str(source).startswith("carl-") and member is not None and role is not None and role in member.roles:
            try:
                await member.remove_roles(role, reason="Temporary role expired")
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(
                    f"Could not remove expired role {role_id} from {user_id}: {exc}"
                )
                continue

        db.remove_temp_role(int(guild_id), int(user_id), int(role_id))


@temp_role_cleanup.before_loop
async def before_temp_role_cleanup():
    await bot.wait_until_ready()


SLIP_PERIODS = [
    app_commands.Choice(name="This Week", value="weekly"),
    app_commands.Choice(name="Last Week", value="last_week"),
    app_commands.Choice(name="This Month", value="monthly"),
    app_commands.Choice(name="Last Month", value="last_month"),
    app_commands.Choice(name="All Time", value="alltime"),
]

TOTAL_SLIP_PERIODS = [
    app_commands.Choice(name="Today", value="today"),
    app_commands.Choice(name="Yesterday", value="yesterday"),
    app_commands.Choice(name="This Week", value="weekly"),
    app_commands.Choice(name="Last Week", value="last_week"),
    app_commands.Choice(name="This Month", value="monthly"),
    app_commands.Choice(name="Last Month", value="last_month"),
    app_commands.Choice(name="All Time", value="alltime"),
]

SLIP_POSTER_PERIODS = [
    app_commands.Choice(name="Today", value="today"),
    app_commands.Choice(name="Yesterday", value="yesterday"),
    app_commands.Choice(name="This Week", value="weekly"),
    app_commands.Choice(name="This Month", value="monthly"),
]

RECORD_PERIODS = [
    app_commands.Choice(name="Today", value="today"),
    app_commands.Choice(name="This Week", value="weekly"),
    app_commands.Choice(name="Last Week", value="last_week"),
    app_commands.Choice(name="This Month", value="monthly"),
    app_commands.Choice(name="Last Month", value="last_month"),
    app_commands.Choice(name="All Time", value="alltime"),
]

MONTHS = [
    app_commands.Choice(name="January", value=1),
    app_commands.Choice(name="February", value=2),
    app_commands.Choice(name="March", value=3),
    app_commands.Choice(name="April", value=4),
    app_commands.Choice(name="May", value=5),
    app_commands.Choice(name="June", value=6),
    app_commands.Choice(name="July", value=7),
    app_commands.Choice(name="August", value=8),
    app_commands.Choice(name="September", value=9),
    app_commands.Choice(name="October", value=10),
    app_commands.Choice(name="November", value=11),
    app_commands.Choice(name="December", value=12),
]


def streak_text(results):
    if not results:
        return "—"
    latest = results[-1][0]
    n = 0
    for r,_ in reversed(results):
        if r == latest:
            n += 1
        else:
            break
    return f"{latest}{n}"


def slip_embed(guild, title, rows):
    if not rows:
        return discord.Embed(title=f"🏆 Winning Slips — {title}", description="No image posts found.")
    medals = ["🥇","🥈","🥉"]
    lines = []
    for i,(uid,total) in enumerate(rows,1):
        m = guild.get_member(int(uid)) if guild else None
        who = m.mention if m else f"<@{uid}>"
        prefix = medals[i-1] if i <= 3 else f"**{i}.**"
        lines.append(f"{prefix} {who} — **{total} slips**")
    return discord.Embed(title=f"🏆 Winning Slips — {title}", description="\n".join(lines))


def record_embed(guild, uid, wins, losses, title, streak, channel_name):
    total = wins + losses
    pct = (wins / total * 100) if total else 0
    m = guild.get_member(uid) if guild else None
    who = m.display_name if m else f"User {uid}"
    e = discord.Embed(
        title=f"📊 {who} — {title}",
        description=f"**Record:** {wins}-{losses}\n**Win Rate:** {pct:.1f}%\n**Total Picks:** {total}\n**Current Streak:** {streak}"
    )
    e.set_footer(text=f"✅ = Win • ❌ = Loss • #{channel_name}")
    return e


def record_lb_embed(guild, rows, title, minimum_picks, scope):
    if not rows:
        return discord.Embed(title=f"🏆 Capper Leaderboard — {title}",
                             description=f"No cappers met the minimum of {minimum_picks} picks.")
    medals = ["🥇","🥈","🥉"]
    lines = []
    for i,(uid,w,l,picks) in enumerate(rows,1):
        m = guild.get_member(int(uid)) if guild else None
        who = m.mention if m else f"<@{uid}>"
        pct = (w/picks*100) if picks else 0
        prefix = medals[i-1] if i <= 3 else f"**{i}.**"
        lines.append(f"{prefix} {who} — **{w}-{l}** ({pct:.1f}%) • {picks} picks")
    e = discord.Embed(title=f"🏆 Capper Leaderboard — {title}", description="\n".join(lines))
    e.set_footer(text=f"{scope} • minimum {minimum_picks} picks")
    return e


# ---------------- Winning Slips commands ----------------

@bot.tree.command(name="total_slips", description="Count all Winning Slips for a selected time period.")
@app_commands.choices(period=TOTAL_SLIP_PERIODS)
async def total_slips(interaction, period: app_commands.Choice[str]):
    start, end = period_range(period.value)
    rows = db.slip_counts(start, end)
    total = sum(int(row[1]) for row in rows)
    noun = "slip" if total == 1 else "slips"
    label = period_name(period.value)

    embed = discord.Embed(
        title=f"📸 Winning Slips Total — {label}",
        description=f"**{total} {noun}** posted in the Winning Slips channel.",
    )

    if start is not None and end is not None:
        start_local = start.astimezone(BOT_TIMEZONE)
        end_local = (end - timedelta(seconds=1)).astimezone(BOT_TIMEZONE)

        if start_local.date() == end_local.date():
            range_text = start_local.strftime("%B %d, %Y")
        else:
            range_text = f"{start_local:%b %d, %Y} – {end_local:%b %d, %Y}"

        embed.set_footer(text=f"{range_text} • Central Time")
    else:
        embed.set_footer(text="All recorded slips • Central Time")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="slip_posters",
    description="List everyone who posted Winning Slips during a selected period.",
)
@app_commands.choices(period=SLIP_POSTER_PERIODS)
async def slip_posters(interaction, period: app_commands.Choice[str]):
    start, end = period_range(period.value)
    rows = db.slip_counts(start, end)
    total_slips = sum(int(row[1]) for row in rows)
    total_posters = len(rows)
    label = period_name(period.value)

    if start is not None and end is not None:
        start_local = start.astimezone(BOT_TIMEZONE)
        end_local = (end - timedelta(seconds=1)).astimezone(BOT_TIMEZONE)
        if start_local.date() == end_local.date():
            range_text = start_local.strftime("%B %d, %Y")
        else:
            range_text = f"{start_local:%b %d, %Y} – {end_local:%b %d, %Y}"
    else:
        range_text = "All recorded slips"

    if not rows:
        embed = discord.Embed(
            title=f"📸 Winning Slip Posters — {label}",
            description="No Winning Slips were posted during this period.",
        )
        embed.set_footer(text=f"{range_text} • Central Time")
        await interaction.response.send_message(embed=embed)
        return

    lines = []
    for index, (user_id, count) in enumerate(rows, start=1):
        member = interaction.guild.get_member(int(user_id)) if interaction.guild else None
        who = member.mention if member else f"<@{user_id}>"
        count = int(count)
        noun = "slip" if count == 1 else "slips"
        lines.append(f"**{index}.** {who} — **{count} {noun}**")

    # Keep each embed comfortably under Discord's 4096-character description limit.
    chunks = []
    current = []
    current_length = 0
    max_chunk_length = 3400

    for line in lines:
        added_length = len(line) + 1
        if current and current_length + added_length > max_chunk_length:
            chunks.append(current)
            current = []
            current_length = 0
        current.append(line)
        current_length += added_length

    if current:
        chunks.append(current)

    embeds = []
    page_count = len(chunks)
    for page_index, chunk in enumerate(chunks, start=1):
        page_suffix = f" ({page_index}/{page_count})" if page_count > 1 else ""
        if page_index == 1:
            header = (
                f"**{total_slips} total slips** • **{total_posters} posters**\n\n"
            )
        else:
            header = ""

        embed = discord.Embed(
            title=f"📸 Winning Slip Posters — {label}{page_suffix}",
            description=header + "\n".join(chunk),
        )
        embed.set_footer(text=f"{range_text} • Central Time")
        embeds.append(embed)

    # Discord allows up to 10 embeds per message. Follow-ups ensure every user is shown.
    await interaction.response.send_message(embeds=embeds[:10])
    for i in range(10, len(embeds), 10):
        await interaction.followup.send(embeds=embeds[i:i + 10])


@bot.tree.command(
    name="slip_giveaway",
    description="Randomly pick one or more giveaway winners from Winning Slips posters.",
)
@app_commands.choices(period=TOTAL_SLIP_PERIODS)
@app_commands.checks.has_permissions(manage_guild=True)
async def slip_giveaway(
    interaction: discord.Interaction,
    period: app_commands.Choice[str],
    winners: app_commands.Range[int, 1, 10] = 1,
):
    start, end = period_range(period.value)
    rows = db.slip_counts(start, end)
    label = period_name(period.value)

    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used inside the server.",
            ephemeral=True,
        )
        return

    # One entry per qualifying current server member, regardless of slip count.
    # The slip count is kept only for display purposes.
    eligible = []
    for user_id, slip_count in rows:
        member = interaction.guild.get_member(int(user_id))
        if member is not None and not member.bot:
            eligible.append((member, int(slip_count)))

    if not eligible:
        await interaction.response.send_message(
            f"🎁 No eligible Winning Slips posters were found for **{label}**.",
            ephemeral=True,
        )
        return

    if winners > len(eligible):
        await interaction.response.send_message(
            f"There are only **{len(eligible)} eligible users** for **{label}**. "
            f"Choose between **1** and **{len(eligible)}** winners.",
            ephemeral=True,
        )
        return

    picked = secrets.SystemRandom().sample(eligible, k=winners)
    total_slips = sum(count for _, count in eligible)

    if start is not None and end is not None:
        start_local = start.astimezone(BOT_TIMEZONE)
        end_local = (end - timedelta(seconds=1)).astimezone(BOT_TIMEZONE)
        if start_local.date() == end_local.date():
            range_text = start_local.strftime("%B %d, %Y")
        else:
            range_text = f"{start_local:%b %d, %Y} – {end_local:%b %d, %Y}"
    else:
        range_text = "All recorded slips"

    winner_lines = []
    for index, (member, slip_count) in enumerate(picked, start=1):
        prefix = "🏆" if winners == 1 else f"🏆 **Winner {index}:**"
        noun = "slip" if slip_count == 1 else "slips"
        if winners == 1:
            winner_lines.append(
                f"{prefix} {member.mention}\nPosted **{slip_count} {noun}** during this period."
            )
        else:
            winner_lines.append(
                f"{prefix} {member.mention} — **{slip_count} {noun}**"
            )

    embed = discord.Embed(
        title=f"🎁 Winning Slips Giveaway — {label}",
        description="\n".join(winner_lines),
    )
    embed.add_field(
        name="Eligible Pool",
        value=f"**{len(eligible)} users** • **{total_slips} total slips**",
        inline=False,
    )
    embed.add_field(
        name="Drawing Method",
        value="Equal chance per eligible user — slip count does not add extra entries.",
        inline=False,
    )
    embed.set_footer(text=f"{range_text} • Central Time")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Show the Winning Slips leaderboard.")
@app_commands.choices(period=SLIP_PERIODS)
async def leaderboard(interaction, period: app_commands.Choice[str]):
    s,e = period_range(period.value)
    rows = db.slip_counts(s,e,limit=10)
    await interaction.response.send_message(embed=slip_embed(interaction.guild, period_name(period.value), rows))

@bot.tree.command(name="month_leaderboard", description="Winning Slips leaderboard for a specific month.")
@app_commands.choices(month=MONTHS)
async def month_leaderboard(interaction, month: app_commands.Choice[int], year: app_commands.Range[int,2020,2100]):
    s,e = month_range(year, month.value)
    rows = db.slip_counts(s,e,limit=10)
    await interaction.response.send_message(embed=slip_embed(interaction.guild, month_label(year,month.value), rows))

@bot.tree.command(name="year_leaderboard", description="Winning Slips leaderboard for a specific year.")
async def year_leaderboard(interaction, year: app_commands.Range[int,2020,2100]):
    s,e = year_range(year)
    rows = db.slip_counts(s,e,limit=10)
    await interaction.response.send_message(embed=slip_embed(interaction.guild, str(year), rows))

@bot.tree.command(name="slipcount", description="Check a member's Winning Slips count.")
@app_commands.choices(period=SLIP_PERIODS)
async def slipcount(interaction, member: discord.Member, period: app_commands.Choice[str]):
    s,e = period_range(period.value)
    rows = db.slip_counts(s,e,user_id=member.id)
    total = int(rows[0][1]) if rows else 0
    await interaction.response.send_message(f"📸 {member.mention} has **{total} slips** for **{period_name(period.value)}**.")

@bot.tree.command(name="month_slipcount", description="Check a member's Winning Slips count for a month.")
@app_commands.choices(month=MONTHS)
async def month_slipcount(interaction, member: discord.Member, month: app_commands.Choice[int], year: app_commands.Range[int,2020,2100]):
    s,e = month_range(year, month.value)
    rows = db.slip_counts(s,e,user_id=member.id)
    total = int(rows[0][1]) if rows else 0
    await interaction.response.send_message(f"📸 {member.mention} posted **{total} slips** in **{month_label(year,month.value)}**.")

@bot.tree.command(name="year_slipcount", description="Check a member's Winning Slips count for a year.")
async def year_slipcount(interaction, member: discord.Member, year: app_commands.Range[int,2020,2100]):
    s,e = year_range(year)
    rows = db.slip_counts(s,e,user_id=member.id)
    total = int(rows[0][1]) if rows else 0
    await interaction.response.send_message(f"📸 {member.mention} posted **{total} slips** in **{year}**.")

@bot.tree.command(name="my_slipcount", description="Check your own Winning Slips count.")
@app_commands.choices(period=SLIP_PERIODS)
async def my_slipcount(interaction, period: app_commands.Choice[str]):
    s,e = period_range(period.value)
    rows = db.slip_counts(s,e,user_id=interaction.user.id)
    total = int(rows[0][1]) if rows else 0
    await interaction.response.send_message(f"📸 You have **{total} slips** for **{period_name(period.value)}**.", ephemeral=False)

@bot.tree.command(name="monthly_champions", description="Show recent completed-month Winning Slips champions.")
async def monthly_champions(interaction, months: app_commands.Range[int,1,24] = 6):
    n = local_now()
    y,m = previous_month(n.year,n.month)
    lines = []
    for _ in range(months):
        s,e = month_range(y,m)
        rows = db.slip_counts(s,e,limit=1)
        label = month_label(y,m)
        if rows:
            uid,total = rows[0]
            member = interaction.guild.get_member(int(uid)) if interaction.guild else None
            who = member.mention if member else f"<@{uid}>"
            lines.append(f"🏆 **{label}:** {who} — **{total} slips**")
        else:
            lines.append(f"— **{label}:** No recorded slips")
        y,m = previous_month(y,m)
    await interaction.response.send_message(embed=discord.Embed(title="📅 Monthly Winning Slips Champions", description="\n".join(lines)))

@bot.tree.command(name="hall_of_fame", description="Show most monthly Winning Slips titles.")
async def hall_of_fame(interaction):
    first = db.first_slip()
    if not first:
        await interaction.response.send_message("No Winning Slips history yet.")
        return
    first_local = first.astimezone(BOT_TIMEZONE)
    n = local_now()
    fy,fm = previous_month(n.year,n.month)
    y,m = first_local.year, first_local.month
    champs = Counter()
    while (y,m) <= (fy,fm):
        s,e = month_range(y,m)
        rows = db.slip_counts(s,e,limit=1)
        if rows:
            champs[int(rows[0][0])] += 1
        if m == 12:
            y,m = y+1,1
        else:
            m += 1
    if not champs:
        await interaction.response.send_message("No completed monthly champions yet.")
        return
    medals = ["🥇","🥈","🥉"]
    lines = []
    for i,(uid,titles) in enumerate(champs.most_common(10),1):
        member = interaction.guild.get_member(uid) if interaction.guild else None
        who = member.mention if member else f"<@{uid}>"
        prefix = medals[i-1] if i <= 3 else f"**{i}.**"
        lines.append(f"{prefix} {who} — **{titles} monthly titles**")
    await interaction.response.send_message(embed=discord.Embed(title="👑 Winning Slips Hall of Fame", description="\n".join(lines)))

@bot.tree.command(name="backfill_slips", description="Scan old Winning Slips images.")
@app_commands.checks.has_permissions(manage_guild=True)
async def backfill_slips(interaction):
    channel = interaction.guild.get_channel(WINNING_SLIPS_CHANNEL_ID) if interaction.guild else None
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("Winning Slips channel not found.", ephemeral=False)
        return
    await interaction.response.defer(ephemeral=False, thinking=True)
    scanned = added = 0
    async for message in channel.history(limit=None, oldest_first=True):
        scanned += 1
        if not message.author.bot and is_image_message(message) and db.add_slip(message):
            added += 1
    await interaction.followup.send(f"✅ Backfill complete. Scanned **{scanned}** messages and added **{added}** image posts.", ephemeral=False)


# ---------------- New capper-record commands ----------------

@bot.tree.command(name="record", description="Show a capper's W-L record in this channel.")
@app_commands.choices(period=RECORD_PERIODS)
async def record(interaction, period: app_commands.Choice[str], member: Optional[discord.Member] = None):
    s,e = period_range(period.value)
    uid = member.id if member else db.primary_capper(interaction.channel_id, s, e)
    if uid is None:
        await interaction.response.send_message("No resolved picks found in this channel for that period.")
        return
    w,l = db.record(uid, interaction.channel_id, s, e)
    results = db.user_results(uid, interaction.channel_id, s, e)
    await interaction.response.send_message(
        embed=record_embed(
            interaction.guild, uid, w, l, period_name(period.value),
            streak_text(results), getattr(interaction.channel,"name","current-channel")
        )
    )

@bot.tree.command(name="month_record", description="Show a capper's record for a specific month in this channel.")
@app_commands.choices(month=MONTHS)
async def month_record(interaction, month: app_commands.Choice[int], year: app_commands.Range[int,2020,2100], member: Optional[discord.Member] = None):
    s,e = month_range(year, month.value)
    uid = member.id if member else db.primary_capper(interaction.channel_id, s, e)
    if uid is None:
        await interaction.response.send_message("No resolved picks found in this channel for that month.")
        return
    w,l = db.record(uid, interaction.channel_id, s, e)
    results = db.user_results(uid, interaction.channel_id, s, e)
    await interaction.response.send_message(
        embed=record_embed(
            interaction.guild, uid, w, l, month_label(year,month.value),
            streak_text(results), getattr(interaction.channel,"name","current-channel")
        )
    )

@bot.tree.command(name="record_leaderboard", description="Rank cappers by win percentage.")
@app_commands.choices(period=RECORD_PERIODS)
async def record_leaderboard(
    interaction,
    period: app_commands.Choice[str],
    minimum_picks: app_commands.Range[int,1,100] = 3,
    server_wide: bool = True
):
    s,e = period_range(period.value)
    channel_id = None if server_wide else interaction.channel_id
    rows = db.record_leaderboard(channel_id, s, e, minimum_picks, 10)
    scope = "All tracked/backfilled channels" if server_wide else f"#{getattr(interaction.channel,'name','current-channel')}"
    await interaction.response.send_message(
        embed=record_lb_embed(interaction.guild, rows, period_name(period.value), minimum_picks, scope)
    )

@bot.tree.command(name="month_record_leaderboard", description="Rank cappers for a specific month.")
@app_commands.choices(month=MONTHS)
async def month_record_leaderboard(
    interaction,
    month: app_commands.Choice[int],
    year: app_commands.Range[int,2020,2100],
    minimum_picks: app_commands.Range[int,1,100] = 3,
    server_wide: bool = True
):
    s,e = month_range(year, month.value)
    channel_id = None if server_wide else interaction.channel_id
    rows = db.record_leaderboard(channel_id, s, e, minimum_picks, 10)
    scope = "All tracked/backfilled channels" if server_wide else f"#{getattr(interaction.channel,'name','current-channel')}"
    await interaction.response.send_message(
        embed=record_lb_embed(interaction.guild, rows, month_label(year,month.value), minimum_picks, scope)
    )

@bot.tree.command(name="backfill_records", description="Scan this channel for old picks marked ✅ or ❌.")
@app_commands.checks.has_permissions(manage_guild=True)
async def backfill_records(interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("Run this in a normal text channel.", ephemeral=False)
        return
    await interaction.response.defer(ephemeral=False, thinking=True)
    scanned = resolved = wins = losses = 0
    async for message in channel.history(limit=None, oldest_first=True):
        scanned += 1
        if message.author.bot:
            continue
        result = detect_pick_result(message.content)
        if result:
            db.upsert_result(message, result)
            resolved += 1
            wins += int(result == "W")
            losses += int(result == "L")
        else:
            db.remove_result(message.id)
    await interaction.followup.send(
        f"✅ Record backfill complete for **#{channel.name}**.\n"
        f"Scanned **{scanned}** messages.\n"
        f"Resolved picks: **{resolved}**\n"
        f"✅ Wins: **{wins}**\n"
        f"❌ Losses: **{losses}**\n\n"
        f"Safe to run again — no double counting.",
        ephemeral=False
    )


# ---------------- Temporary-role management ----------------

async def validate_role_management(interaction, member, role):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command only works inside the server.", ephemeral=False
        )
        return False

    me = interaction.guild.me
    if me is None:
        await interaction.response.send_message(
            "I couldn't resolve my server member record.", ephemeral=False
        )
        return False

    if not me.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "I need the **Manage Roles** permission before I can manage temp roles.",
            ephemeral=False,
        )
        return False

    if role.is_default() or role.managed:
        await interaction.response.send_message(
            "That role can't be managed as a temporary role.", ephemeral=False
        )
        return False

    if role >= me.top_role:
        await interaction.response.send_message(
            f"Move my bot role above {role.mention} in **Server Settings → Roles** first.",
            ephemeral=False,
        )
        return False

    if member == interaction.guild.owner:
        # Discord still permits lower-role edits in some cases, but avoid a confusing failure.
        pass

    return True


@bot.tree.command(name="temprole_add", description="Give a member a timed role and track its expiration.")
@app_commands.describe(
    member="Member receiving the temporary role",
    duration="Examples: 7d, 30d, 1mo, 1y, 1w2d, 12h30m",
    role="Role to assign temporarily",
)
@app_commands.checks.has_permissions(manage_roles=True)
async def temprole_add(
    interaction: discord.Interaction,
    member: discord.Member,
    duration: str,
    role: discord.Role,
):
    if not await validate_role_management(interaction, member, role):
        return

    try:
        length = parse_duration(duration)
    except ValueError:
        await interaction.response.send_message(
            "Invalid duration. Use formats like **7d**, **30d**, **1mo**, **1y**, "
            "**1w2d**, or **12h30m**.",
            ephemeral=False,
        )
        return

    assigned_at = datetime.now(timezone.utc)
    expires_at = assigned_at + length

    try:
        if role not in member.roles:
            await member.add_roles(
                role, reason=f"Temporary role assigned by {interaction.user}"
            )
    except (discord.Forbidden, discord.HTTPException) as exc:
        await interaction.response.send_message(
            f"Discord would not let me assign that role: `{exc}`", ephemeral=False
        )
        return

    db.upsert_temp_role(
        interaction.guild.id, member.id, role.id, assigned_at, expires_at,
        interaction.user.id, "self"
    )
    db.add_temp_role_history(
        f"self:{interaction.id}", interaction.guild.id, member.id, role.id,
        assigned_at, expires_at, interaction.user.id, "self"
    )

    unix = int(expires_at.timestamp())
    await interaction.response.send_message(
        f"✅ {member.mention} now has {role.mention} for **{human_remaining(length)}**.\n"
        f"Expires <t:{unix}:F> • <t:{unix}:R>",
        ephemeral=False,
    )


@bot.tree.command(name="temprole_import", description="Track an existing Carl-bot temp role when you know its time remaining.")
@app_commands.describe(
    member="Member who already has the role",
    duration="TIME REMAINING, e.g. 18d, 6h, 1mo",
    role="Existing temporary role to track",
)
@app_commands.checks.has_permissions(manage_roles=True)
async def temprole_import(
    interaction: discord.Interaction,
    member: discord.Member,
    duration: str,
    role: discord.Role,
):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command only works inside the server.", ephemeral=False
        )
        return

    if role not in member.roles:
        await interaction.response.send_message(
            f"{member.mention} does not currently have {role.mention}.", ephemeral=False
        )
        return

    try:
        length = parse_duration(duration)
    except ValueError:
        await interaction.response.send_message(
            "Invalid duration. Use formats like **7d**, **30d**, **1mo**, **1y**, "
            "**1w2d**, or **12h30m**.",
            ephemeral=False,
        )
        return

    assigned_at = datetime.now(timezone.utc)
    expires_at = assigned_at + length
    db.upsert_temp_role(
        interaction.guild.id, member.id, role.id, assigned_at, expires_at,
        interaction.user.id, "carl-import"
    )
    db.add_temp_role_history(
        f"manual:{interaction.id}", interaction.guild.id, member.id, role.id,
        assigned_at, expires_at, interaction.user.id, "carl-import"
    )
    unix = int(expires_at.timestamp())
    await interaction.response.send_message(
        f"✅ Tracking {member.mention}'s {role.mention}.\n"
        f"Expires <t:{unix}:F> • <t:{unix}:R>",
        ephemeral=False,
    )


@bot.tree.command(name="backfill_temproles", description="Rebuild temp-role timers and history from this Carl-bot log channel.")
@app_commands.describe(
    role="The temporary role these Carl-bot logs belong to",
)
@app_commands.checks.has_permissions(manage_roles=True)
async def backfill_temproles(
    interaction: discord.Interaction,
    role: discord.Role,
):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "Run this command inside the Carl-bot temp-role log text channel.",
            ephemeral=False,
        )
        return

    await interaction.response.defer(ephemeral=False, thinking=True)

    now = datetime.now(timezone.utc)
    scanned = parsed = history_saved = imported = expired = missing_role = wrong_role = unparsed = 0
    managed_skipped = 0
    latest_by_member = {}

    # Scan oldest -> newest. Every parsed grant is saved to permanent history,
    # while only the newest still-active grant becomes the member's current timer.
    async for message in interaction.channel.history(limit=None, oldest_first=True):
        scanned += 1
        if not message.author.bot:
            continue

        text = carl_log_text(message)
        if not text.strip():
            continue

        # If the log explicitly mentions role IDs, only accept the selected role.
        mentioned_role_ids = {int(x) for x in re.findall(r"<@&(\d{15,22})>", text)}
        if mentioned_role_ids and role.id not in mentioned_role_ids:
            wrong_role += 1
            continue

        member_id = member_id_from_carl_log(text)
        expires_at = expiry_from_carl_log(message, text)
        if member_id is None or expires_at is None:
            unparsed += 1
            continue

        parsed += 1
        assigned_at = message.created_at.astimezone(timezone.utc)

        db.add_temp_role_history(
            f"carl:{interaction.channel.id}:{message.id}",
            interaction.guild.id,
            member_id,
            role.id,
            assigned_at,
            expires_at,
            interaction.user.id,
            "carl-backfill",
            interaction.channel.id,
            message.id,
        )
        history_saved += 1
        latest_by_member[member_id] = (assigned_at, expires_at)

    # Refresh current timers for this role from the reconstructed history.
    # Existing rows are removed first so members who lost/expired the role do not
    # remain on /temproles after a new backfill.
    existing = db.temp_roles(interaction.guild.id, role_id=role.id, active_only=False)
    protected_members = set()
    for user_id, role_id, assigned_at, expires_at, assigned_by, source in existing:
        if str(source).startswith("carl-"):
            db.remove_temp_role(interaction.guild.id, int(user_id), int(role_id))
        else:
            # Once your bot has taken over or created a timer, never let an old
            # Carl log backfill overwrite that live expiration.
            protected_members.add(int(user_id))

    for member_id, (assigned_at, expires_at) in latest_by_member.items():
        if member_id in protected_members:
            managed_skipped += 1
            continue
        if expires_at <= now:
            expired += 1
            continue

        member = interaction.guild.get_member(member_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(member_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None

        if member is None or role not in member.roles:
            missing_role += 1
            continue

        db.upsert_temp_role(
            interaction.guild.id,
            member.id,
            role.id,
            assigned_at,
            expires_at,
            interaction.user.id,
            "carl-backfill",
        )
        imported += 1

    await interaction.followup.send(
        f"✅ **Carl-bot temp-role backfill finished for {role.mention}.**\n"
        f"Scanned: **{scanned}** messages\n"
        f"Parsed temp-role grants: **{parsed}**\n"
        f"History entries saved/refreshed: **{history_saved}**\n"
        f"Active timers imported: **{imported}**\n"
        f"Already managed by your bot (left untouched): **{managed_skipped}**\n"
        f"Expired grants: **{expired}**\n"
        f"Members who no longer have the role: **{missing_role}**\n"
        f"Other explicitly-mentioned roles skipped: **{wrong_role}**\n"
        f"Could not parse: **{unparsed}**\n\n"
        f"Use `/temproles`, `/expired_temproles`, or `/temprole_history` to view the results.",
        ephemeral=False,
    )


@bot.tree.command(name="temprolecheck", description="Show a member's tracked temporary roles and time remaining.")
@app_commands.describe(member="Member to check")
@app_commands.checks.has_permissions(manage_roles=True)
async def temprolecheck(interaction: discord.Interaction, member: discord.Member):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command only works inside the server.", ephemeral=False
        )
        return

    rows = db.temp_roles(interaction.guild.id, user_id=member.id)
    if not rows:
        await interaction.response.send_message(
            f"No active tracked temp roles for {member.mention}.", ephemeral=False
        )
        return

    now = datetime.now(timezone.utc)
    lines = []
    for user_id, role_id, assigned_at, expires_at, assigned_by, source in rows:
        role = interaction.guild.get_role(int(role_id))
        expires = parse_db_datetime(expires_at)
        unix = int(expires.timestamp())
        role_text = role.mention if role else f"Role `{role_id}`"
        source_text = {
            "self": "Your bot",
            "carl-import": "Carl-bot manual import",
            "carl-backfill": "Carl-bot log backfill",
        }.get(str(source), str(source))
        lines.append(
            f"• {role_text} — **{human_remaining(expires - now)} left** "
            f"(<t:{unix}:R>) • {source_text}"
        )

    embed = discord.Embed(
        title=f"⏳ Temp Roles — {member.display_name}",
        description="\n".join(lines),
    )
    await interaction.response.send_message(embed=embed, ephemeral=False)


@bot.tree.command(name="temproles", description="List all tracked temporary roles and time remaining.")
@app_commands.describe(role="Optional: only show one role")
@app_commands.checks.has_permissions(manage_roles=True)
async def temproles(
    interaction: discord.Interaction,
    role: Optional[discord.Role] = None,
):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command only works inside the server.", ephemeral=False
        )
        return

    await interaction.response.defer(ephemeral=False, thinking=True)

    rows = db.temp_roles(
        interaction.guild.id, role_id=role.id if role else None
    )
    if not rows:
        await interaction.followup.send(
            "No active tracked temp roles found.", ephemeral=False
        )
        return

    now = datetime.now(timezone.utc)
    entries = []
    user_cache = {}
    for user_id, role_id, assigned_at, expires_at, assigned_by, source in rows:
        who, member = await resolve_user_label(interaction.guild, int(user_id), user_cache)
        tracked_role = interaction.guild.get_role(int(role_id))
        expires = parse_db_datetime(expires_at)
        unix = int(expires.timestamp())
        role_text = tracked_role.mention if tracked_role else f"Role `{role_id}`"
        entries.append(
            f"{who} — {role_text} — **{human_remaining(expires - now)} left** "
            f"(<t:{unix}:R>)"
        )

    # Keep each embed well below Discord's description limit.
    pages = [entries[i:i + 15] for i in range(0, len(entries), 15)]
    for index, page in enumerate(pages):
        title = "⏳ Active Temp Roles"
        if len(pages) > 1:
            title += f" ({index + 1}/{len(pages)})"
        embed = discord.Embed(title=title, description="\n".join(page))
        if index == 0:
            await interaction.followup.send(embed=embed, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, ephemeral=False)


async def resolve_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    """Resolve a current guild member even when they are not in the local cache."""
    member = guild.get_member(int(user_id))
    if member is not None:
        return member
    try:
        return await guild.fetch_member(int(user_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def resolve_user_label(guild: discord.Guild, user_id: int, cache: Optional[dict] = None):
    """Return (display label, current Member or None) for current and former members."""
    uid = int(user_id)
    if cache is not None and uid in cache:
        return cache[uid]

    member = await resolve_member(guild, uid)
    if member is not None:
        # IMPORTANT: user mentions such as <@123...> do not reliably render as
        # names inside embed descriptions. Use plain-text Discord names instead.
        username = member.name
        display_name = member.display_name
        if display_name and display_name != username:
            label = f"{display_name} (@{username})"
        else:
            label = f"@{username}"
        result = (label, member)
        if cache is not None:
            cache[uid] = result
        return result

    # Former members are no longer fetchable as Guild Members, but their Discord
    # user profile can usually still be resolved globally by ID. Keep the account
    # username in plain text so embeds never fall back to a raw <@USER_ID> token.
    user = bot.get_user(uid)
    if user is None:
        try:
            user = await bot.fetch_user(uid)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            user = None

    if user is not None:
        username = user.name
        display_name = getattr(user, "global_name", None) or getattr(user, "display_name", None)
        if display_name and display_name != username:
            label = f"{display_name} (@{username})"
        else:
            label = f"@{username}"
        result = (label, None)
    else:
        result = (f"Unknown user ({uid})", None)

    if cache is not None:
        cache[uid] = result
    return result


def temp_history_status(guild: discord.Guild, user_id: int, role_id: int, expires_at: datetime, member: Optional[discord.Member] = None):
    """Return a readable current status for a historical temp-role grant."""
    now = datetime.now(timezone.utc)
    role = guild.get_role(int(role_id))

    if member is None:
        return "🚪 Left server"

    has_role = role is not None and role in member.roles
    if has_role and expires_at > now:
        return "🟢 Active"
    if has_role and expires_at <= now:
        return "⌛ Grant expired • member currently has role"
    if expires_at <= now:
        return "⌛ Expired / role gone"
    return "🔴 Removed early / no longer has role"


def temp_source_label(source: str) -> str:
    return {
        "self": "Your bot",
        "self-migrated": "Your bot (migrated from Carl-bot)",
        "carl-import": "Carl-bot manual import",
        "carl-backfill": "Carl-bot log backfill",
    }.get(str(source), str(source))


@bot.tree.command(name="temprole_duration", description="Show members who received a temp role for a specific original duration.")
@app_commands.describe(
    duration="Original grant length: 7d, 1w, 30d, 1mo, 90d, 6mo, 1y, etc.",
    role="Optional: only search one role",
    active_only="Only show memberships that are active right now",
    limit="Maximum matching grants to show (1-100)",
)
@app_commands.checks.has_permissions(manage_roles=True)
async def temprole_duration(
    interaction: discord.Interaction,
    duration: str,
    role: Optional[discord.Role] = None,
    active_only: bool = False,
    limit: app_commands.Range[int, 1, 100] = 50,
):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command only works inside the server.", ephemeral=False
        )
        return

    try:
        wanted = parse_duration(duration)
    except ValueError:
        await interaction.response.send_message(
            "Invalid duration. Try **7d**, **1w**, **30d**, **1mo**, **90d**, **6mo**, or **1y**.",
            ephemeral=False,
        )
        return

    await interaction.response.defer(ephemeral=False, thinking=True)

    rows = db.temp_role_history(
        interaction.guild.id,
        role_id=role.id if role else None,
        limit=5000,
    )
    if not rows:
        await interaction.followup.send(
            "No temp-role history has been recorded yet.", ephemeral=False
        )
        return

    matches = []
    user_cache = {}
    now = datetime.now(timezone.utc)

    for row in rows:
        event_key, user_id, role_id, assigned_at, expires_at, assigned_by, source, log_channel_id, log_message_id = row
        assigned = parse_db_datetime(assigned_at)
        expires = parse_db_datetime(expires_at)
        original_length = expires - assigned
        if not duration_matches(original_length, wanted):
            continue

        who, member_obj = await resolve_user_label(
            interaction.guild, int(user_id), user_cache
        )
        role_obj = interaction.guild.get_role(int(role_id))
        has_role = (
            member_obj is not None
            and role_obj is not None
            and role_obj in member_obj.roles
            and expires > now
        )
        if active_only and not has_role:
            continue

        role_text = role_obj.mention if role_obj else f"Role `{role_id}`"
        assigned_unix = int(assigned.timestamp())
        expires_unix = int(expires.timestamp())
        status = temp_history_status(
            interaction.guild, int(user_id), int(role_id), expires, member_obj
        )
        matches.append(
            (
                assigned,
                f"{status} • {who} — {role_text}\n"
                f"**{human_duration_full(original_length)}** • Granted <t:{assigned_unix}:d> • "
                f"Expires/expired <t:{expires_unix}:d>",
            )
        )

    if not matches:
        scope = f" for {role.mention}" if role else ""
        active_text = " active" if active_only else ""
        await interaction.followup.send(
            f"No{active_text} temp-role grants with an original duration of **{human_duration_full(wanted)}**{scope} were found.",
            ephemeral=False,
        )
        return

    matches.sort(key=lambda item: item[0], reverse=True)
    text_entries = [text for _, text in matches[:limit]]
    pages = [text_entries[i:i + 10] for i in range(0, len(text_entries), 10)]
    for index, page in enumerate(pages):
        title = f"⏱️ Temp Roles — {human_duration_full(wanted)}"
        if active_only:
            title += " (Active Only)"
        if len(pages) > 1:
            title += f" ({index + 1}/{len(pages)})"
        embed = discord.Embed(title=title, description="\n\n".join(page))
        embed.set_footer(
            text=f"Showing {min(len(matches), limit)} of {len(matches)} matching grant(s). Original grant duration, not time remaining."
        )
        if index == 0:
            await interaction.followup.send(embed=embed, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, ephemeral=False)


@bot.tree.command(name="recently_expired", description="Show memberships that expired within a recent time window.")
@app_commands.describe(
    period="Lookback window: 7d, 1w, 30d, 1mo, 90d, 6mo, 1y, etc.",
    role="Optional: only show one role",
    limit="Maximum expired memberships to show (1-100)",
)
@app_commands.checks.has_permissions(manage_roles=True)
async def recently_expired(
    interaction: discord.Interaction,
    period: str = "7d",
    role: Optional[discord.Role] = None,
    limit: app_commands.Range[int, 1, 100] = 50,
):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command only works inside the server.", ephemeral=False
        )
        return

    try:
        window = parse_duration(period)
    except ValueError:
        await interaction.response.send_message(
            "Invalid period. Try **7d**, **1w**, **30d**, **1mo**, **90d**, **6mo**, or **1y**.",
            ephemeral=False,
        )
        return

    await interaction.response.defer(ephemeral=False, thinking=True)

    rows = db.temp_role_history(
        interaction.guild.id,
        role_id=role.id if role else None,
        limit=5000,
    )
    if not rows:
        await interaction.followup.send(
            "No temp-role history has been recorded yet.", ephemeral=False
        )
        return

    # Newest history is returned first. Only the latest grant per member+role is
    # considered, so a member who renewed is not falsely listed as expired.
    latest = {}
    for row in rows:
        key = (int(row[1]), int(row[2]))
        if key not in latest:
            latest[key] = row

    now = datetime.now(timezone.utc)
    cutoff = now - window
    entries = []
    user_cache = {}

    for row in latest.values():
        event_key, user_id, role_id, assigned_at, expires_at, assigned_by, source, log_channel_id, log_message_id = row
        assigned = parse_db_datetime(assigned_at)
        expires = parse_db_datetime(expires_at)

        # This command is a true expiration log, so only scheduled expirations
        # inside the requested lookback window are included.
        if not (cutoff <= expires <= now):
            continue

        who, member_obj = await resolve_user_label(
            interaction.guild, int(user_id), user_cache
        )
        role_obj = interaction.guild.get_role(int(role_id))
        has_role = member_obj is not None and role_obj is not None and role_obj in member_obj.roles

        # If they renewed and their latest grant is active, they won't reach this
        # branch because the latest grant's expiration would be in the future.
        if has_role:
            status = "⚠️ Expired timer • role still present"
        elif member_obj is None:
            status = "🚪 Expired • left server"
        else:
            status = "⌛ Expired"

        role_text = role_obj.mention if role_obj else f"Role `{role_id}`"
        expires_unix = int(expires.timestamp())
        original_length = expires - assigned
        entries.append(
            (
                expires,
                f"{status} • {who} — {role_text}\n"
                f"Expired <t:{expires_unix}:f> • <t:{expires_unix}:R> • "
                f"Original term: **{human_duration_full(original_length)}**",
            )
        )

    if not entries:
        scope = f" for {role.mention}" if role else ""
        await interaction.followup.send(
            f"No latest memberships expired within the last **{human_duration_full(window)}**{scope}.",
            ephemeral=False,
        )
        return

    entries.sort(key=lambda item: item[0], reverse=True)
    text_entries = [text for _, text in entries[:limit]]
    pages = [text_entries[i:i + 10] for i in range(0, len(text_entries), 10)]
    for index, page in enumerate(pages):
        title = f"🕒 Recently Expired — Last {human_duration_full(window)}"
        if len(pages) > 1:
            title += f" ({index + 1}/{len(pages)})"
        embed = discord.Embed(title=title, description="\n\n".join(page))
        embed.set_footer(
            text=f"Showing {min(len(entries), limit)} of {len(entries)} latest expired membership(s). Renewed members are excluded."
        )
        if index == 0:
            await interaction.followup.send(embed=embed, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, ephemeral=False)


@bot.tree.command(name="temprole_history", description="Show permanent temp-role assignment history.")
@app_commands.describe(
    member="Optional member to filter",
    role="Optional role to filter",
    limit="Maximum history entries to show (1-100)",
)
@app_commands.checks.has_permissions(manage_roles=True)
async def temprole_history(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
    role: Optional[discord.Role] = None,
    limit: app_commands.Range[int, 1, 100] = 50,
):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command only works inside the server.", ephemeral=False
        )
        return

    await interaction.response.defer(ephemeral=False, thinking=True)

    rows = db.temp_role_history(
        interaction.guild.id,
        user_id=member.id if member else None,
        role_id=role.id if role else None,
        limit=limit,
    )
    if not rows:
        await interaction.followup.send(
            "No temp-role history has been recorded for that filter yet.", ephemeral=False
        )
        return

    entries = []
    user_cache = {}
    for event_key, user_id, role_id, assigned_at, expires_at, assigned_by, source, log_channel_id, log_message_id in rows:
        assigned = parse_db_datetime(assigned_at)
        expires = parse_db_datetime(expires_at)
        assigned_unix = int(assigned.timestamp())
        expires_unix = int(expires.timestamp())
        who, guild_member = await resolve_user_label(interaction.guild, int(user_id), user_cache)
        tracked_role = interaction.guild.get_role(int(role_id))
        role_text = tracked_role.mention if tracked_role else f"Role `{role_id}`"
        status = temp_history_status(interaction.guild, int(user_id), int(role_id), expires, guild_member)
        entries.append(
            f"{status} • {who} — {role_text}\n"
            f"Granted <t:{assigned_unix}:f> • Expired/expires <t:{expires_unix}:f> "
            f"(<t:{expires_unix}:R>) • {temp_source_label(source)}"
        )

    pages = [entries[i:i + 10] for i in range(0, len(entries), 10)]
    for index, page in enumerate(pages):
        title = "📚 Temp Role History"
        if len(pages) > 1:
            title += f" ({index + 1}/{len(pages)})"
        embed = discord.Embed(title=title, description="\n\n".join(page))
        embed.set_footer(text="History is permanent; old grants are not erased when a role is removed.")
        if index == 0:
            await interaction.followup.send(embed=embed, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, ephemeral=False)


@bot.tree.command(name="expired_temproles", description="Show people whose latest temp role expired or is no longer present.")
@app_commands.describe(role="Optional: only show one role")
@app_commands.checks.has_permissions(manage_roles=True)
async def expired_temproles(
    interaction: discord.Interaction,
    role: Optional[discord.Role] = None,
):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command only works inside the server.", ephemeral=False
        )
        return

    await interaction.response.defer(ephemeral=False, thinking=True)

    rows = db.temp_role_history(
        interaction.guild.id,
        role_id=role.id if role else None,
        limit=5000,
    )
    if not rows:
        await interaction.followup.send(
            "No temp-role history has been recorded yet.", ephemeral=False
        )
        return

    # History is newest first. Keep only the latest grant for each member+role so
    # somebody who renewed is not listed just because an older membership expired.
    latest = {}
    for row in rows:
        key = (int(row[1]), int(row[2]))
        if key not in latest:
            latest[key] = row

    entries = []
    user_cache = {}
    for row in latest.values():
        event_key, user_id, role_id, assigned_at, expires_at, assigned_by, source, log_channel_id, log_message_id = row
        expires = parse_db_datetime(expires_at)
        who, member_obj = await resolve_user_label(interaction.guild, int(user_id), user_cache)
        role_obj = interaction.guild.get_role(int(role_id))
        has_role = member_obj is not None and role_obj is not None and role_obj in member_obj.roles

        # Only show records that are not currently valid active memberships.
        if has_role and expires > datetime.now(timezone.utc):
            continue

        if has_role and expires <= datetime.now(timezone.utc):
            status = "⚠️ Timer expired but role still present"
        else:
            status = temp_history_status(interaction.guild, int(user_id), int(role_id), expires, member_obj)
        role_text = role_obj.mention if role_obj else f"Role `{role_id}`"
        unix = int(expires.timestamp())
        entries.append(
            (expires, f"{status} • {who} — {role_text}\n"
                      f"Scheduled expiration <t:{unix}:f> • <t:{unix}:R>")
        )

    if not entries:
        await interaction.followup.send(
            "Everyone in the latest tracked history still has an active temp role.",
            ephemeral=False,
        )
        return

    entries.sort(key=lambda item: item[0], reverse=True)
    text_entries = [text for _, text in entries]
    pages = [text_entries[i:i + 12] for i in range(0, len(text_entries), 12)]
    for index, page in enumerate(pages):
        title = "🗂️ Expired / Removed Temp Roles"
        if len(pages) > 1:
            title += f" ({index + 1}/{len(pages)})"
        embed = discord.Embed(title=title, description="\n\n".join(page))
        embed.set_footer(text="Uses each member's latest recorded grant for the selected role.")
        if index == 0:
            await interaction.followup.send(embed=embed, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, ephemeral=False)


@bot.tree.command(name="temprole_takeover", description="Move active imported Carl-bot timers under this bot's control.")
@app_commands.describe(role="Temporary role to take over from Carl-bot")
@app_commands.checks.has_permissions(manage_roles=True)
async def temprole_takeover(
    interaction: discord.Interaction,
    role: discord.Role,
):
    # Reuse the same hierarchy/permission validation as individual temp-role actions.
    if not await validate_role_management(interaction, interaction.user, role):
        return

    await interaction.response.defer(ephemeral=False, thinking=True)

    rows = db.temp_roles(interaction.guild.id, role_id=role.id, active_only=True)
    if not rows:
        await interaction.followup.send(
            f"No active tracked timers were found for {role.mention}. "
            f"If these are still Carl-bot timers, run `/backfill_temproles` in the Carl log channel first.",
            ephemeral=False,
        )
        return

    adopted = already_managed = stale = unresolved = 0
    for user_id, role_id, assigned_at, expires_at, assigned_by, source in rows:
        member = interaction.guild.get_member(int(user_id))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(user_id))
            except discord.NotFound:
                db.remove_temp_role(interaction.guild.id, int(user_id), int(role_id))
                stale += 1
                continue
            except (discord.Forbidden, discord.HTTPException):
                unresolved += 1
                continue

        if role not in member.roles:
            db.remove_temp_role(interaction.guild.id, int(user_id), int(role_id))
            stale += 1
            continue

        if not str(source).startswith("carl-"):
            already_managed += 1
            continue

        assigned = parse_db_datetime(assigned_at)
        expires = parse_db_datetime(expires_at)
        db.upsert_temp_role(
            interaction.guild.id, member.id, role.id, assigned, expires,
            interaction.user.id, "self-migrated"
        )
        db.update_latest_temp_role_history(
            interaction.guild.id, member.id, role.id, expires,
            assigned_by=interaction.user.id, source="self-migrated"
        )
        adopted += 1

    await interaction.followup.send(
        f"✅ **{role.mention} takeover complete.**\n"
        f"Carl-bot timers moved under your bot: **{adopted}**\n"
        f"Already managed by your bot: **{already_managed}**\n"
        f"Stale timers cleaned up: **{stale}**\n"
        f"Could not verify right now: **{unresolved}**\n\n"
        f"From now on, your bot will remove these tracked roles when their timers expire.",
        ephemeral=False,
    )


@bot.tree.command(name="temprole_extend", description="Add time to one member's active temporary-role timer.")
@app_commands.describe(
    member="Member whose timer should be extended",
    duration="Time to ADD, e.g. 7d, 30d, 1mo, 12h",
    role="Temporary role to extend",
)
@app_commands.checks.has_permissions(manage_roles=True)
async def temprole_extend(
    interaction: discord.Interaction,
    member: discord.Member,
    duration: str,
    role: discord.Role,
):
    if not await validate_role_management(interaction, member, role):
        return

    try:
        length = parse_duration(duration)
    except ValueError:
        await interaction.response.send_message(
            "Invalid duration. Use formats like **7d**, **30d**, **1mo**, **1y**, "
            "**1w2d**, or **12h30m**.",
            ephemeral=False,
        )
        return

    if role not in member.roles:
        await interaction.response.send_message(
            f"{member.mention} does not currently have {role.mention}.",
            ephemeral=False,
        )
        return

    rows = db.temp_roles(
        interaction.guild.id, user_id=member.id, role_id=role.id, active_only=True
    )
    if not rows:
        await interaction.response.send_message(
            f"I don't have an active tracked timer for {member.mention}'s {role.mention}. "
            f"If Carl-bot owns it, run `/backfill_temproles` first.",
            ephemeral=False,
        )
        return

    user_id, role_id, assigned_at, expires_at, assigned_by, source = rows[0]
    assigned = parse_db_datetime(assigned_at)
    old_expires = parse_db_datetime(expires_at)
    new_expires = old_expires + length
    new_source = "self-migrated" if str(source).startswith("carl-") else str(source)

    db.upsert_temp_role(
        interaction.guild.id, member.id, role.id, assigned, new_expires,
        interaction.user.id, new_source
    )
    db.update_latest_temp_role_history(
        interaction.guild.id, member.id, role.id, new_expires,
        assigned_by=interaction.user.id, source=new_source
    )

    old_unix = int(old_expires.timestamp())
    new_unix = int(new_expires.timestamp())
    adopted_note = "\n↪️ This timer was imported from Carl-bot and is now managed by your bot." if str(source).startswith("carl-") else ""
    await interaction.response.send_message(
        f"✅ Added **{human_duration_full(length)}** to {member.mention}'s {role.mention}.\n"
        f"Old expiration: <t:{old_unix}:F>\n"
        f"New expiration: <t:{new_unix}:F> • <t:{new_unix}:R>"
        f"{adopted_note}",
        ephemeral=False,
    )


@bot.tree.command(name="temprole_extendall", description="Add the same amount of time to everyone with an active tracked temp role.")
@app_commands.describe(
    duration="Time to ADD to every active timer, e.g. 7d, 30d, 1mo",
    role="Temporary role whose active timers should all be extended",
)
@app_commands.checks.has_permissions(manage_roles=True)
async def temprole_extendall(
    interaction: discord.Interaction,
    duration: str,
    role: discord.Role,
):
    if not await validate_role_management(interaction, interaction.user, role):
        return

    try:
        length = parse_duration(duration)
    except ValueError:
        await interaction.response.send_message(
            "Invalid duration. Use formats like **7d**, **30d**, **1mo**, **1y**, "
            "**1w2d**, or **12h30m**.",
            ephemeral=False,
        )
        return

    await interaction.response.defer(ephemeral=False, thinking=True)

    rows = db.temp_roles(interaction.guild.id, role_id=role.id, active_only=True)
    if not rows:
        await interaction.followup.send(
            f"No active tracked timers were found for {role.mention}. "
            f"If Carl-bot currently owns them, run `/backfill_temproles` in the Carl log channel first.",
            ephemeral=False,
        )
        return

    extended = adopted = stale = unresolved = 0
    for user_id, role_id, assigned_at, expires_at, assigned_by, source in rows:
        member = interaction.guild.get_member(int(user_id))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(user_id))
            except discord.NotFound:
                db.remove_temp_role(interaction.guild.id, int(user_id), int(role_id))
                stale += 1
                continue
            except (discord.Forbidden, discord.HTTPException):
                unresolved += 1
                continue

        if role not in member.roles:
            db.remove_temp_role(interaction.guild.id, int(user_id), int(role_id))
            stale += 1
            continue

        assigned = parse_db_datetime(assigned_at)
        old_expires = parse_db_datetime(expires_at)
        new_expires = old_expires + length
        was_carl = str(source).startswith("carl-")
        new_source = "self-migrated" if was_carl else str(source)

        db.upsert_temp_role(
            interaction.guild.id, member.id, role.id, assigned, new_expires,
            interaction.user.id, new_source
        )
        db.update_latest_temp_role_history(
            interaction.guild.id, member.id, role.id, new_expires,
            assigned_by=interaction.user.id, source=new_source
        )
        extended += 1
        adopted += int(was_carl)

    await interaction.followup.send(
        f"✅ **Bulk extension complete for {role.mention}.**\n"
        f"Added **{human_duration_full(length)}** to **{extended}** active timer(s).\n"
        f"Carl-bot timer(s) automatically moved under your bot: **{adopted}**\n"
        f"Stale timers cleaned up: **{stale}**\n"
        f"Could not verify right now: **{unresolved}**\n\n"
        f"Each person's time was added to their existing expiration — it was **not** reset from today.",
        ephemeral=False,
    )


@bot.tree.command(name="temprole_remove", description="Remove a tracked temporary role now and cancel its timer.")
@app_commands.describe(member="Member losing the role", role="Temporary role to remove")
@app_commands.checks.has_permissions(manage_roles=True)
async def temprole_remove(
    interaction: discord.Interaction,
    member: discord.Member,
    role: discord.Role,
):
    if not await validate_role_management(interaction, member, role):
        return

    # Cancel the timer before changing Discord so the on_member_update safeguard
    # does not restore a migrated role that an admin intentionally removed here.
    existing = db.temp_roles(
        interaction.guild.id, user_id=member.id, role_id=role.id, active_only=False
    )
    db.remove_temp_role(interaction.guild.id, member.id, role.id)

    try:
        if role in member.roles:
            await member.remove_roles(
                role, reason=f"Temporary role removed by {interaction.user}"
            )
    except (discord.Forbidden, discord.HTTPException) as exc:
        # If Discord rejects the role removal, restore the timer we just cancelled.
        if existing:
            user_id, role_id, assigned_at, expires_at, assigned_by, source = existing[0]
            db.upsert_temp_role(
                interaction.guild.id, int(user_id), int(role_id),
                parse_db_datetime(assigned_at), parse_db_datetime(expires_at),
                int(assigned_by), str(source),
            )
        await interaction.response.send_message(
            f"Discord would not let me remove that role: `{exc}`", ephemeral=False
        )
        return

    await interaction.response.send_message(
        f"✅ Removed {role.mention} from {member.mention} and cancelled its tracked timer.",
        ephemeral=False,
    )


@temprole_add.error
@temprole_import.error
@backfill_temproles.error
@temprolecheck.error
@temproles.error
@temprole_history.error
@temprole_duration.error
@recently_expired.error
@expired_temproles.error
@temprole_takeover.error
@temprole_extend.error
@temprole_extendall.error
@temprole_remove.error
async def temprole_permission_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the **Manage Roles** permission to use temp-role controls."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=False)
        else:
            await interaction.response.send_message(message, ephemeral=False)
        return
    raise error


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Add it in Railway Variables.")

bot.run(TOKEN)
