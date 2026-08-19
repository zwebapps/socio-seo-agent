"""Grant or revoke the platform-admin role.

Deliberately a script, not an endpoint.

Platform admin controls model routing and provider settings — the operator's cost and
quality decisions. Any HTTP path to granting it, however well guarded, is a path an
attacker can try; a script requires access to the machine and the database, which is a
different and much higher bar. So the only way in is out of band, on purpose.

    uv run python scripts/grant_platform_admin.py --email you@example.com
    uv run python scripts/grant_platform_admin.py --email you@example.com --revoke
    uv run python scripts/grant_platform_admin.py --list

Prints what changed, and refuses quietly-wrong input rather than guessing: an unknown
email is an error, not a no-op, because "nothing happened" is indistinguishable from
"it worked" at a glance.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# A script run directly is not on the package path, unlike a test (pytest adds the
# rootdir) or the app (uvicorn is started from it). Adding the repo root here means
# `uv run python scripts/...` works from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from sqlalchemy import select

load_dotenv(override=False)

from backend.app.db.models import Role, User  # noqa: E402
from backend.app.db.session import session  # noqa: E402


async def _list() -> int:
    async with session() as s:
        users = (await s.execute(select(User).order_by(User.created_at))).scalars().all()

    if not users:
        print("No users yet. Create one at /login, then run this again.")
        return 0

    width = max(len(u.email) for u in users)
    print(f"{'EMAIL'.ljust(width)}  ROLE            ACTIVE")
    for user in users:
        flag = "yes" if user.is_active else "NO"
        marker = " <-- platform admin" if user.role == Role.PLATFORM_ADMIN else ""
        print(f"{user.email.ljust(width)}  {user.role.ljust(15)} {flag}{marker}")

    admins = [u for u in users if u.role == Role.PLATFORM_ADMIN]
    if not admins:
        print("\nNo platform admin exists. /developer/models is unreachable until one does.")
    return 0


async def _set_role(email: str, role: Role) -> int:
    normalised = email.strip().lower()
    async with session() as s, s.begin():
        user = (await s.execute(select(User).where(User.email == normalised))).scalar_one_or_none()

        if user is None:
            print(f"No user with email {normalised!r}.", file=sys.stderr)
            print("Run with --list to see who exists.", file=sys.stderr)
            return 1

        if user.role == role:
            print(f"{normalised} is already {role.value}; nothing to do.")
            return 0

        previous = user.role
        user.role = role
        print(f"{normalised}: {previous} -> {role.value}")

    if role == Role.PLATFORM_ADMIN:
        print("They can now change model routing and provider settings.")
    else:
        print("They can no longer change platform settings.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="the account to change")
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="demote to owner instead of granting platform admin",
    )
    parser.add_argument("--list", action="store_true", help="show every user and role")
    args = parser.parse_args()

    if args.list:
        return asyncio.run(_list())
    if not args.email:
        parser.print_help()
        return 2

    role = Role.OWNER if args.revoke else Role.PLATFORM_ADMIN
    return asyncio.run(_set_role(args.email, role))


if __name__ == "__main__":
    raise SystemExit(main())
