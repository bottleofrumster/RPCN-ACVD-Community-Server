"""Cumulative settled mercenary pay, independent of wins and wallet balance.

Gross mission pay includes paid reward components before repair/ammunition
deductions and wallet saturation. Only an explicitly classified hired sortie
can contribute. Recovery uses retained, account-bound settlement receipts;
missing history is counted, never invented or replaced with the wallet.
"""

UINT64_MAX = (1 << 64) - 1


def _amount(value: int) -> int:
    if type(value) is not int or not 0 <= value <= UINT64_MAX:
        raise ValueError("Stored mercenary earnings need review")
    return value


def reconcile(state: dict) -> bool:
    """Initialize legacy earnings once, without changing wallets or receipts."""
    changed = False
    history = state.get("world_battle_settlements", [])
    if not isinstance(history, list) or len(history) > 1000:
        raise ValueError("Mercenary earnings history needs review")
    for online_id, profile in state.get("player_profiles", {}).items():
        if not isinstance(profile, dict):
            continue
        sorties = profile.get("observed_mercenary_sorties")
        if sorties is None:
            continue
        if type(sorties) is not int or not 0 <= sorties < 1 << 32:
            raise ValueError("Stored mercenary sortie count needs review")
        if "observed_mercenary_earnings" in profile:
            _amount(profile["observed_mercenary_earnings"])
            continue
        player_id = profile.get("player_id")
        if type(player_id) is not int or not 0 < player_id < 1 << 32:
            raise ValueError("Mercenary earnings require an account-bound player")
        recovered = {}
        for receipt in history:
            if not isinstance(receipt, dict):
                raise ValueError("Mercenary settlement receipt needs review")
            progress = receipt.get("player_ranking_progress", [])
            if not isinstance(progress, list):
                raise ValueError("Mercenary receipt roles need review")
            hired = [row for row in progress if isinstance(row, dict)
                     and row.get("player_id") == player_id
                     and type(row.get("mercenary_sorties_delta")) is int
                     and row.get("mercenary_sorties_delta") == 1]
            if not hired:
                continue
            participants = receipt.get("participants", [])
            if not isinstance(participants, list):
                raise ValueError("Mercenary receipt participants need review")
            payments = [row for row in participants
                        if isinstance(row, dict) and row.get("player_id") == player_id
                        and row.get("online_id") == online_id]
            digest = receipt.get("result_digest")
            if len(hired) != 1 or len(payments) != 1 or not isinstance(digest, str):
                raise ValueError("Mercenary receipt identity is ambiguous")
            try:
                raw = bytes.fromhex(digest)
                if len(raw) != 32 or raw.hex() != digest:
                    raise ValueError()
            except ValueError as error:
                raise ValueError("Mercenary receipt digest needs review") from error
            gross = _amount(payments[0].get("gross_earnings"))
            if digest in recovered and recovered[digest] != gross:
                raise ValueError("Conflicting mercenary payment receipts")
            recovered[digest] = gross
        if len(recovered) > sorties:
            raise ValueError("Mercenary receipts exceed the observed sortie count")
        profile["observed_mercenary_earnings"] = _amount(sum(recovered.values()))
        profile["mercenary_earnings_missing_sorties"] = sorties - len(recovered)
        changed = True
    return changed


def credit(profile: dict, gross_earnings: int) -> None:
    """Call inside the existing once-only settlement transaction, for a hired pilot."""
    previous = _amount(profile.get("observed_mercenary_earnings", 0))
    profile["observed_mercenary_earnings"] = _amount(previous + _amount(gross_earnings))
    profile.setdefault("mercenary_earnings_missing_sorties", 0)
