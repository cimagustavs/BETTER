"""Gateway-based invite tracker. Watches member joins, attributes each join to the invite that
was used (by diffing invite use-counts), tallies invites per inviter, and grants a reward role
once an inviter reaches the threshold (default: 5 invites -> VIP Silver).

Requires the privileged Server Members intent (enable it in the Discord Developer Portal).
Runs on its own gateway connection; the betting poller runs in a separate thread.
"""
import os
import json

import discord

GUILD_ID = int(os.environ["GUILD_ID"])
INVITE_REWARD_ROLE_ID = int(os.environ["INVITE_REWARD_ROLE_ID"])
INVITE_THRESHOLD = int(os.environ.get("INVITE_THRESHOLD", "5"))
ANNOUNCE_CHANNEL_ID = os.environ.get("INVITE_ANNOUNCE_CHANNEL_ID")  # optional

STATE_DIR = os.environ.get("STATE_DIR", "/data")
INVITE_STATE_PATH = os.path.join(STATE_DIR, "invites.json")


def _load_counts():
    try:
        with open(INVITE_STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_counts(counts):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = INVITE_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(counts, f)
        os.replace(tmp, INVITE_STATE_PATH)
    except OSError as e:
        print(f"[invites] save failed (is STATE_DIR a writable volume?): {e}")


def run_invite_bot(token):
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True  # privileged — enable "Server Members Intent" in the dev portal

    client = discord.Client(intents=intents)
    cache = {}                  # invite code -> uses (snapshot)
    counts = _load_counts()     # inviter_id (str) -> retained invite count

    async def refresh_cache(guild):
        try:
            cache.clear()
            for inv in await guild.invites():
                cache[inv.code] = inv.uses or 0
        except discord.Forbidden:
            print("[invites] missing Manage Server permission to read invites")

    @client.event
    async def on_ready():
        guild = client.get_guild(GUILD_ID)
        if guild:
            await refresh_cache(guild)
        print(f"[invites] tracker online as {client.user} — {len(cache)} invites cached, "
              f"reward at {INVITE_THRESHOLD} -> role {INVITE_REWARD_ROLE_ID}")

    @client.event
    async def on_invite_create(invite):
        cache[invite.code] = invite.uses or 0

    @client.event
    async def on_invite_delete(invite):
        cache.pop(invite.code, None)

    @client.event
    async def on_member_join(member):
        if member.guild.id != GUILD_ID or member.bot:
            return
        try:
            current = await member.guild.invites()
        except discord.Forbidden:
            return

        # Find which invite's use-count went up since our last snapshot.
        inviter = None
        for inv in current:
            prev = cache.get(inv.code, 0)
            if (inv.uses or 0) > prev:
                inviter = inv.inviter
            cache[inv.code] = inv.uses or 0

        if inviter is None or inviter.bot:
            return

        key = str(inviter.id)
        counts[key] = counts.get(key, 0) + 1
        _save_counts(counts)
        n = counts[key]

        role = member.guild.get_role(INVITE_REWARD_ROLE_ID)
        role_name = role.name if role else "the reward role"
        log_ch = member.guild.get_channel(int(ANNOUNCE_CHANNEL_ID)) if ANNOUNCE_CHANNEL_ID else None

        # Live progress log on every attributed join.
        if log_ch:
            await log_ch.send(
                f"🔗 {member.mention} joined — invited by **{inviter.display_name}** "
                f"(**{n}/{INVITE_THRESHOLD}** toward {role_name})"
            )

        # Reward once they reach the threshold.
        if n >= INVITE_THRESHOLD and role:
            inviter_member = member.guild.get_member(inviter.id)
            if inviter_member and role not in inviter_member.roles:
                try:
                    await inviter_member.add_roles(role, reason=f"Reached {INVITE_THRESHOLD} invites")
                    print(f"[invites] granted reward role to {inviter} ({n} invites)")
                    if log_ch:
                        await log_ch.send(
                            f"🎉 {inviter.mention} just hit **{INVITE_THRESHOLD} invites** "
                            f"and unlocked **{role.name}**! Invite friends to earn it too."
                        )
                except discord.Forbidden:
                    print("[invites] missing permission / role hierarchy to assign reward role")

    client.run(token, log_handler=None)
