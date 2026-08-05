"""Seed a dev fixture for testing the helper app end-to-end.

Creates one approved helper and one bus, then prints a bus id and a fresh access
token to paste into the app. There is no admin approval endpoint yet, so the
`users.status` / `helpers.status` flip that an admin would normally perform is
done directly here.

Idempotent — rerun it to mint a new token against the same fixture.

    docker compose exec api python -m scripts.seed_dev

DEV ONLY. It creates an account with a known password.
"""

import asyncio
import sys

from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal
from app.models.fleet import Bus, BusStatus
from app.models.user import Helper, HelperStatus, User, UserRole, UserStatus

# A `.test` domain here would seed an account that cannot log in: `EmailStr`
# rejects RFC 2606 reserved TLDs, so /auth/login answers 422 before checking the
# password. See the same note in scripts/seed.py.
HELPER_EMAIL = "helper@buscrew.com.bd"
HELPER_PASSWORD = "helper-dev-password"
BUS_REG_NO = "DHK-METRO-GA-11-1234"


async def seed() -> None:
    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.email == HELPER_EMAIL))
        ).scalar_one_or_none()

        if user is None:
            user = User(
                email=HELPER_EMAIL,
                password_hash=hash_password(HELPER_PASSWORD),
                role=UserRole.helper,
                name="Dev Helper",
                phone="+8801700000000",
                # An admin would normally set both of these on approval.
                status=UserStatus.active,
            )
            user.helper = Helper(status=HelperStatus.approved)
            db.add(user)
            await db.commit()
            await db.refresh(user)
            print(f"created helper {HELPER_EMAIL}")
        else:
            # Re-assert approval in case an earlier run left it pending;
            # /helper/gps rejects anything else.
            user.status = UserStatus.active
            helper = (
                await db.execute(select(Helper).where(Helper.user_id == user.id))
            ).scalar_one_or_none()
            if helper is None:
                helper = Helper(user_id=user.id, status=HelperStatus.approved)
                db.add(helper)
            else:
                helper.status = HelperStatus.approved
            await db.commit()
            print(f"reusing helper {HELPER_EMAIL}")

        bus = (
            await db.execute(select(Bus).where(Bus.reg_no == BUS_REG_NO))
        ).scalar_one_or_none()
        if bus is None:
            bus = Bus(
                reg_no=BUS_REG_NO,
                nickname="Dev Bus 1",
                capacity=40,
                status=BusStatus.active,
            )
            db.add(bus)
            await db.commit()
            await db.refresh(bus)
            print(f"created bus {BUS_REG_NO}")
        else:
            print(f"reusing bus {BUS_REG_NO}")

        token = create_access_token(str(user.id), user.role)

        print("\n--- paste into the helper app ---")
        print(f"BUS ID: {bus.id}")
        print(f"TOKEN:  {token}")
        print(
            "\nToken expires in 15 minutes. Rerun this script, or POST "
            f"/auth/login with {HELPER_EMAIL} / {HELPER_PASSWORD}, for a new one."
        )


if __name__ == "__main__":
    try:
        asyncio.run(seed())
    except Exception as exc:  # noqa: BLE001 - dev script, surface the reason plainly
        print(f"seed failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
