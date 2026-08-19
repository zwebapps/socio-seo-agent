"""Postgres-backed model routing configuration.

Reads and writes are deliberately separate types. :class:`PostgresRouteStore` satisfies
the read-only ``RouteStore`` protocol the resolver depends on, and
:class:`RouteConfigWriter` holds the mutations. The resolver sits on the hot path of
every model call and has no business being able to change configuration.

Neither table carries ``business_id``, so neither is under RLS: which model serves which
task is an operator decision about cost and quality, not customer data. See the note on
the models themselves.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, select

from backend.app.db.models import ModelRoute, ProviderSetting
from backend.app.db.session import session
from backend.app.llm.route_config import (
    ProviderSettingRecord,
    RouteRecord,
)


class PostgresRouteStore:
    """The read side. This is what the router's resolver holds."""

    async def load_routes(self) -> Sequence[RouteRecord]:
        async with session() as s:
            rows = (await s.execute(select(ModelRoute))).scalars().all()
        records: list[RouteRecord] = []
        for row in rows:
            try:
                records.append(
                    RouteRecord(
                        task_class=row.task_class,  # type: ignore[arg-type]
                        tier=row.tier,  # type: ignore[arg-type]
                        chain=[
                            {
                                "provider": str(e.get("provider", "")),
                                "model": str(e.get("model", "")),
                            }
                            for e in row.chain
                        ],
                        note=row.note,
                    )
                )
            except ValueError:
                # A row whose task_class or tier is no longer a valid enum value --
                # possible after a rename. Skipping it means that task falls back to its
                # code default, which is strictly better than refusing to start.
                continue
        return records

    async def load_providers(self) -> Sequence[ProviderSettingRecord]:
        async with session() as s:
            rows = (await s.execute(select(ProviderSetting))).scalars().all()
        return [
            ProviderSettingRecord(
                provider=row.provider,
                enabled=row.enabled,
                base_url=row.base_url,
                note=row.note,
            )
            for row in rows
        ]


class RouteConfigWriter:
    """The write side. Used by the admin API only."""

    async def set_route(
        self,
        *,
        task_class: str,
        tier: str,
        chain: Sequence[dict[str, str]],
        updated_by: UUID | None = None,
        note: str | None = None,
    ) -> None:
        """Upsert one task class's route.

        An empty chain DELETES the row rather than storing an empty list. "Use the
        default" and "use nothing" are different intentions, and only one of them is
        ever what an admin means -- storing empty would leave a row that the resolver
        has to ignore anyway.
        """
        async with session() as s, s.begin():
            existing = (
                await s.execute(select(ModelRoute).where(ModelRoute.task_class == task_class))
            ).scalar_one_or_none()

            cleaned = [
                {"provider": e["provider"].strip(), "model": e["model"].strip()}
                for e in chain
                if e.get("provider", "").strip() and e.get("model", "").strip()
            ]

            if not cleaned:
                if existing is not None:
                    await s.delete(existing)
                return

            if existing is None:
                s.add(
                    ModelRoute(
                        task_class=task_class,
                        tier=tier,
                        chain=cleaned,
                        updated_by=updated_by,
                        note=note,
                    )
                )
            else:
                existing.tier = tier
                existing.chain = cleaned
                existing.updated_by = updated_by
                existing.note = note

    async def clear_route(self, task_class: str) -> None:
        """Revert one task class to its code default."""
        async with session() as s, s.begin():
            await s.execute(delete(ModelRoute).where(ModelRoute.task_class == task_class))

    async def set_provider(
        self,
        *,
        provider: str,
        enabled: bool,
        base_url: str | None = None,
        updated_by: UUID | None = None,
        note: str | None = None,
    ) -> None:
        """Upsert one provider's availability and address.

        No API key is accepted here, deliberately: keys stay in the injected
        environment. A key in this table would be a key in every backup and replica.
        """
        async with session() as s, s.begin():
            existing = (
                await s.execute(select(ProviderSetting).where(ProviderSetting.provider == provider))
            ).scalar_one_or_none()
            if existing is None:
                s.add(
                    ProviderSetting(
                        provider=provider,
                        enabled=enabled,
                        base_url=base_url,
                        updated_by=updated_by,
                        note=note,
                    )
                )
            else:
                existing.enabled = enabled
                existing.base_url = base_url
                existing.updated_by = updated_by
                existing.note = note


__all__ = ["PostgresRouteStore", "RouteConfigWriter"]
