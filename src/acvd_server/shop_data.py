"""Load the online shop's fixed game tables and authenticated regulation.

Operators supply the four named v1.02 tables under data/shop. Loading freezes
those inputs for this process; changing them requires a tested reload. Prices
come only from the same authenticated CDN file that the server serves. No save
cache, rank override, missing-snapshot default or ownership grant is used.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import rsa

from .config import ServerConfig
from .mission_progress import (
    ShopLevelResult,
    calculate_shop_level,
    hardcore_rate,
    level_thresholds,
    mission_records,
)
from .parameter_tables import MAX_PARAMETER_BYTES, read_rows
from .player_storage import _requested_mission_snapshot
from .regulation import read_container
from .shop_catalogue import (
    CataloguePart,
    catalogue_rows,
    encode_catalogue_rows,
    part_prices,
    part_tuning_budgets,
    parts_from_regulation_cache,
    regulation_file,
    sale_multiplier,
    sale_price,
    shop_lineup,
)
from .shop_unlocks import STORY_SHOP_PARTS
from .story_item_data import STORY_PART_REWARDS

PARAMETER_FILES = (
    "partsshoplineup.bin",
    "missionmenudataparam.bin",
    "playerexp2ratevaluelv.bin",
    "hardcoremodeparam.bin",
)


def _read_bounded(path: Path, limit: int = MAX_PARAMETER_BYTES) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Shop input exceeds the supported size bound")
    return data


@dataclass(frozen=True, slots=True)
class ShopCatalogue:
    entries: tuple[CataloguePart, ...]
    mission_menu: bytes
    level_table: bytes
    normal_rate: int
    regulation_path: Path
    regulation_digest: str
    input_hashes: tuple[tuple[str, str], ...]
    part_price_rows: tuple[tuple[int, int, int], ...] = ()
    sell_multiplier: float | None = None
    part_tuning_rows: tuple[tuple[int, int, int], ...] = ()
    ultimate_entries: tuple[CataloguePart, ...] = ()
    story_entries: tuple[CataloguePart, ...] = ()

    @classmethod
    def load(cls, config: ServerConfig, public_key: rsa.RSAPublicKey) -> ShopCatalogue | None:
        directory = config.shop_parameter_path
        if not directory.exists():
            return None
        sources = {name: _read_bounded(directory / name) for name in PARAMETER_FILES}
        container = _read_bounded(config.regulation_path, MAX_PARAMETER_BYTES + 256)
        clear = read_container(container, public_key)
        part_data = parts_from_regulation_cache(clear)
        prices = part_prices(part_data)
        budgets = part_tuning_budgets(part_data)
        entries = shop_lineup(sources[PARAMETER_FILES[0]], prices)
        if not entries:
            raise ValueError("Online shop lineup is empty")
        ultimate_parts = STORY_PART_REWARDS[(100, 1010, 0)]
        if len(ultimate_parts) != 6 or any(category != 11 for category, _ in ultimate_parts):
            raise ValueError("Final-mission Ultimate weapon rule changed")
        present = [(category, part_id) in prices for category, part_id in ultimate_parts]
        if any(present) and not all(present):
            raise ValueError("Final-mission Ultimate weapon prices are incomplete")
        if any(row.category == 11 for row in entries):
            raise ValueError("Ultimate weapon stock must not duplicate the normal lineup")
        next_lineup_id = max(row.lineup_id for row in entries) + 1
        ultimate_entries = (
            tuple(
                CataloguePart(next_lineup_id + index, 0, category, part_id, prices[(category, part_id)])
                for index, (category, part_id) in enumerate(ultimate_parts)
            )
            if all(present)
            else ()
        )
        normal_parts = {(row.category, row.part_id) for row in entries}
        story_entries = tuple(
            CataloguePart(next_lineup_id + len(ultimate_entries) + index, 0, category, part_id,
                          prices[(category, part_id)])
            for index, (category, part_id) in enumerate(
                sorted((STORY_SHOP_PARTS & prices.keys()) - normal_parts)
            )
        )
        system_parameter = regulation_file(clear, b"serversystemparam.bin")
        multiplier = sale_multiplier(system_parameter) if system_parameter is not None else None
        menu, levels = sources[PARAMETER_FILES[1]], sources[PARAMETER_FILES[2]]
        # Reject broken inputs at load time, not when the first player opens Shop.
        if not read_rows(menu, b"MISSION_MENU_DATA_PARAM", 0x56):
            raise ValueError("Shop mission-point table is empty")
        level_thresholds(levels)
        # This service provides the standard online shop. The native rate
        # accessor selects row zero for normal contexts; Hardcore is separate.
        rate = hardcore_rate(sources[PARAMETER_FILES[3]], 0)
        if rate <= 0:
            raise ValueError("Normal online shop rate must be positive")
        hashes = tuple((name, hashlib.sha256(data).hexdigest()) for name, data in sources.items())
        return cls(
            entries,
            menu,
            levels,
            rate,
            config.regulation_path,
            hashlib.sha256(container).hexdigest(),
            hashes,
            tuple(
                (category, part_id, price) for (category, part_id), price in sorted(prices.items())
            ),
            multiplier,
            tuple(
                (category, part_id, budget)
                for (category, part_id), budget in sorted(budgets.items())
            ),
            ultimate_entries,
            story_entries,
        )

    def tuning_budget(self, category: int, part_id: int) -> int:
        for part_category, identifier, budget in self.part_tuning_rows:
            if (part_category, identifier) == (category, part_id):
                return budget
        raise ValueError("Part tuning capability is not qualified")

    def sale_credit(self, category: int, part_id: int) -> int:
        """Price existing ownership from all part banks, not the buy lineup."""
        if self.sell_multiplier is None:
            raise ValueError("Sale parameter is not installed")
        current = _read_bounded(self.regulation_path, MAX_PARAMETER_BYTES + 256)
        if hashlib.sha256(current).hexdigest() != self.regulation_digest:
            raise ValueError("Active regulation changed; reload the verified shop inputs")
        for part_category, identifier, price in self.part_price_rows:
            if (part_category, identifier) == (category, part_id):
                return sale_price(price, self.sell_multiplier)
        raise ValueError("Owned part has no verified regulation price")

    @property
    def version(self) -> str:
        material = self.regulation_digest + "".join(
            name + digest for name, digest in self.input_hashes
        )
        material += "".join(
            f"{row.lineup_id}:{row.category}:{row.part_id}:{row.price};"
            for row in (*self.ultimate_entries, *self.story_entries)
        )
        return hashlib.sha256(material.encode("ascii")).hexdigest()

    @property
    def shop_key(self) -> int:
        # The native purchase echoes this uint32 listing key. Keep it positive
        # in the signed range and bind it to the immutable input version.
        return (int(self.version[:8], 16) & 0x7FFFFFFF) or 1

    def offer(self, entry_key: int) -> CataloguePart | None:
        return next(
            (row for row in (*self.entries, *self.ultimate_entries, *self.story_entries)
             if row.lineup_id == entry_key),
            None,
        )

    def unlocked_offer(self, offer: CataloguePart, unlocked_parts) -> CataloguePart:
        """Use the same earned-part eligibility for listing and purchase."""
        if (offer.category, offer.part_id) in unlocked_parts:
            return replace(offer, minimum_level=0)
        if offer in self.story_entries:
            raise ValueError("Named part requires its earned Story reward")
        return offer

    def progression_for(
        self,
        *,
        player_data_path: Path,
        player_id: int,
        additional_points: int,
    ) -> tuple[ShopLevelResult, bool]:
        """Use one authenticated mission snapshot for rank and Mission 10 stock."""
        if type(player_id) is not int or player_id <= 0:
            raise ValueError("Shop requires an authenticated player ID")
        current = _read_bounded(self.regulation_path, MAX_PARAMETER_BYTES + 256)
        if hashlib.sha256(current).hexdigest() != self.regulation_digest:
            raise ValueError("Active regulation changed; reload the verified shop inputs")
        snapshot, _preseed = _requested_mission_snapshot(player_data_path, player_id=player_id)
        level = calculate_shop_level(
            snapshot,
            self.mission_menu,
            self.level_table,
            additional_points=additional_points,
            rate_percent=self.normal_rate,
        )
        final_cleared = any(
            record.mission_id == 1010 and record.rank >= 0 for record in mission_records(snapshot)
        )
        return level, final_cleared

    def level_for(
        self,
        *,
        player_data_path: Path,
        player_id: int,
        additional_points: int,
    ) -> ShopLevelResult:
        return self.progression_for(
            player_data_path=player_data_path,
            player_id=player_id,
            additional_points=additional_points,
        )[0]

    def page(
        self,
        *,
        player_data_path: Path,
        player_id: int,
        additional_points: int,
        category: int,
        offset: int,
        limit: int,
        unlocked_parts: frozenset[tuple[int, int]] = frozenset(),
    ) -> tuple[bytes, tuple[CataloguePart, ...], ShopLevelResult]:
        level, final_cleared = self.progression_for(
            player_data_path=player_data_path,
            player_id=player_id,
            additional_points=additional_points,
        )
        available = tuple(self.unlocked_offer(row, unlocked_parts) for row in self.entries)
        available += self.ultimate_entries if final_cleared else ()
        available += tuple(
            row for row in self.story_entries if (row.category, row.part_id) in unlocked_parts
        )
        rows = catalogue_rows(available, level=level.level, category=category, offset=offset, limit=limit)
        return encode_catalogue_rows(rows, self.shop_key), rows, level
