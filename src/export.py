"""Export mode -- interactive CSV export of Humble keys."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from src.humble_api import filter_expiring_keys, redeem_humble_key
from src.ownership import get_owned_apps, match_ownership
from src.steam_auth import steam_login
from src.utils import (
    cls,
    console,
    find_dict_keys,
    print_rule,
    print_success,
    prompt_yes_no,
    verify_logins_session,
)

EXPORT_KEY_HEADERS = [
    "human_name",
    "redeemed_key_val",
    "is_gift",
    "key_type_human_name",
    "is_expired",
    "steam_ownership",
]


def export_mode(
    humble_session,
    order_details: list[dict[str, Any]],
    *,
    steam_cookies: str | Path | None = None,
    user_agent: str | None = None,
    only_expiring: bool = False,
) -> None:
    """Interactive CSV export of Humble keys with optional Steam ownership info."""
    cls()

    steam_session = None
    owned_app_details: dict[int, str] | None = None

    print_rule("Export Configuration")

    export_steam_only = prompt_yes_no("Export only Steam keys?")
    export_revealed = prompt_yes_no("Export revealed keys?")
    export_unrevealed = prompt_yes_no("Export unrevealed keys?")

    if not export_revealed and not export_unrevealed:
        console.print("[bold]That leaves 0 keys…[/bold]")
        sys.exit()

    reveal_unrevealed = False
    confirm_reveal = False
    if export_unrevealed:
        reveal_unrevealed = prompt_yes_no(
            "Reveal all unrevealed keys? (This will remove your ability to "
            "claim gift links on these)"
        )
        if reveal_unrevealed:
            extra = "Steam " if export_steam_only else ""
            confirm_reveal = prompt_yes_no(
                f"Please CONFIRM that you would like ALL {extra}keys on Humble "
                f"to be revealed, this can't be undone."
            )

    steam_config = prompt_yes_no(
        "Sign into Steam to detect ownership on the export data?"
    )

    if steam_config:
        steam_session = steam_login(
            cookies_file=steam_cookies, user_agent=user_agent
        )
        if verify_logins_session(steam_session)[1]:
            owned_app_details = get_owned_apps(
                steam_session, user_agent=user_agent
            )

    desired_keys = "steam_app_id" if export_steam_only else "key_type_human_name"
    keylist = list(find_dict_keys(order_details, desired_keys, True))
    if only_expiring:
        original_length = len(keylist)
        keylist = filter_expiring_keys(humble_session, order_details, keylist)
        console.print(
            f"[dim]Filtered {original_length - len(keylist)} keys without an expiry date.[/dim]"
        )

    keys: list[dict] = []
    for tpk in keylist:
        revealed = "redeemed_key_val" in tpk
        export = (export_revealed and revealed) or (export_unrevealed and not revealed)

        if export:
            if export_unrevealed and confirm_reveal:
                tpk["redeemed_key_val"] = redeem_humble_key(humble_session, tpk)

            if owned_app_details is not None and "steam_app_id" in tpk:
                owned = tpk["steam_app_id"] in owned_app_details
                if not owned:
                    best_match = match_ownership(owned_app_details, tpk)
                    owned = (
                        best_match[1] is not None and best_match[1] in owned_app_details
                    )
                tpk["steam_ownership"] = owned

            keys.append(tpk)

    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"humble_export_{ts}.csv"
    with open(filename, "w", encoding="utf-8-sig") as f:
        f.write(",".join(EXPORT_KEY_HEADERS) + "\n")
        for key in keys:
            row = []
            for col in EXPORT_KEY_HEADERS:
                if col in key:
                    row.append(f'"{key[col]}"')
                else:
                    row.append("")
            f.write(",".join(row) + "\n")

    print_success(f"Exported to {filename}")
