"""Humble Bundle login, API calls, and key fetching."""

from __future__ import annotations

import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generator
from zoneinfo import ZoneInfo

from rich.prompt import Prompt

from src import HUMBLE_COOKIE_FILE
from src.utils import (
    cls,
    console,
    export_cookies,
    find_dict_keys,
    import_cookies,
    print_error,
    print_rule,
    print_warning,
    try_recover_cookies,
    verify_logins_session,
)

# Humble endpoints
HUMBLE_LOGIN_PAGE = "https://www.humblebundle.com/login"
HUMBLE_SUB_PAGE = "https://www.humblebundle.com/subscription/"

HUMBLE_LOGIN_API = "https://www.humblebundle.com/processlogin"
HUMBLE_REDEEM_API = "https://www.humblebundle.com/humbler/redeemkey"
HUMBLE_ORDERS_API = "https://www.humblebundle.com/api/v1/user/order"
HUMBLE_ORDER_DETAILS_API = "https://www.humblebundle.com/api/v1/order/"
HUMBLE_SUB_API = (
    "https://www.humblebundle.com/api/v1/subscriptions/"
    "humble_monthly/subscription_products_with_gamekeys/"
)

HUMBLE_PAY_EARLY = "https://www.humblebundle.com/subscription/payearly"
HUMBLE_CHOOSE_CONTENT = "https://www.humblebundle.com/humbler/choosecontent"

# Shared headers for Humble API calls
HUMBLE_HEADERS: dict[str, str] = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def humble_login(
    session,
    *,
    auto: bool = False,
    cookies_file: str | Path | None = None,
) -> bool:
    """Log into Humble Bundle. Updates *session* in place. Returns True on success."""
    cls()

    if cookies_file is not None:
        if not import_cookies(cookies_file, session, ".humblebundle.com"):
            print_error(f"Couldn't load Humble cookies from {cookies_file}")
            sys.exit(1)
        if not verify_logins_session(session)[0]:
            print_error("Humble cookies are invalid or expired.")
            sys.exit(1)
        csrf_cookie = session.cookies.get_dict().get("csrf_cookie")
        if csrf_cookie:
            HUMBLE_HEADERS["CSRF-Prevention-Token"] = csrf_cookie
        return True

    # Attempt to use saved session
    if (
        try_recover_cookies(HUMBLE_COOKIE_FILE, session)
        and verify_logins_session(session)[0]
    ):
        HUMBLE_HEADERS["CSRF-Prevention-Token"] = session.cookies["csrf_cookie"]
        return True
    else:
        session.cookies.clear()

    if auto:
        print_error("Humble session expired. Run interactively to re-authenticate.")
        sys.exit(1)

    # Saved session didn't work — interactive login
    print_rule("Humble Bundle Login")

    authorized = False
    while not authorized:
        username = Prompt.ask("[bold cyan]Email[/bold cyan]")
        password = Prompt.ask("[bold cyan]Password[/bold cyan]", password=True)
        session.get(HUMBLE_LOGIN_PAGE)

        payload = {
            "access_token": "",
            "access_token_provider_id": "",
            "goto": "/",
            "qs": "",
            "username": username,
            "password": password,
        }
        HUMBLE_HEADERS["CSRF-Prevention-Token"] = session.cookies["csrf_cookie"]

        r = session.post(
            HUMBLE_LOGIN_API, data=payload, headers=HUMBLE_HEADERS.copy()
        )
        login_json = r.json()

        if "errors" in login_json and "username" in login_json["errors"]:
            print_error(login_json["errors"]["username"][0])
            console.print()
            continue

        auth_response = None
        while "humble_guard_required" in login_json or "two_factor_required" in login_json:
            if "humble_guard_required" in login_json:
                humble_guard_code = Prompt.ask(
                    "[bold cyan]Humble Guard code[/bold cyan]"
                )
                payload["guard"] = humble_guard_code.upper()
                auth_response = session.post(
                    HUMBLE_LOGIN_API,
                    data=payload,
                    headers=HUMBLE_HEADERS.copy(),
                )
                login_json = auth_response.json()

                if (
                    "user_terms_opt_in_data" in login_json
                    and login_json["user_terms_opt_in_data"]["needs_to_opt_in"]
                ):
                    print_error(
                        "TOS update required — please sign in to Humble on your browser."
                    )
                    sys.exit()
            elif (
                "two_factor_required" in login_json
                and "errors" in login_json
                and "authy-input" in login_json["errors"]
            ):
                code = Prompt.ask("[bold cyan]2FA code[/bold cyan]")
                payload["code"] = code
                auth_response = session.post(
                    HUMBLE_LOGIN_API,
                    data=payload,
                    headers=HUMBLE_HEADERS.copy(),
                )
                login_json = auth_response.json()
            elif "errors" in login_json:
                print_error("Unexpected login error detected.")
                console.print_json(data=login_json["errors"])
                sys.exit()

            if auth_response is not None and auth_response.status_code == 200:
                break

        export_cookies(HUMBLE_COOKIE_FILE, session)
        return True


def redeem_humble_key(session, tpk: dict[str, Any]) -> str:
    """Reveal a key on Humble's API for the given *tpk* entry. Returns the key string."""
    payload = {
        "keytype": tpk["machine_name"],
        "key": tpk["gamekey"],
        "keyindex": tpk["keyindex"],
    }
    resp = session.post(
        HUMBLE_REDEEM_API, data=payload, headers=HUMBLE_HEADERS.copy()
    )

    resp_json = resp.json()
    if resp.status_code != 200 or "error_msg" in resp_json or not resp_json["success"]:
        print_error(f"Error redeeming key on Humble for {tpk['human_name']}")
        if "error_msg" in resp_json:
            print_error(resp_json["error_msg"])
        return ""
    try:
        return resp_json["key"]
    except Exception:
        return resp.text


def get_month_data(humble_session, month: dict) -> dict:
    """Fetch Humble Choice month data from the subscription page."""
    r = humble_session.get(HUMBLE_SUB_PAGE + month["product"]["choice_url"])

    data_indicator = '<script id="webpack-monthly-product-data" type="application/json">'
    if data_indicator not in r.text:
        raise RuntimeError(
            f"Couldn't find product data for "
            f"{month['product'].get('choice_url')!r} (HTTP {r.status_code}); "
            f"Humble may have redirected to login or changed the page layout."
        )
    json_text = r.text.split(data_indicator)[1].split("</script>")[0].strip()
    return json.loads(json_text)["contentChoiceOptions"]


_EXPIRATION_FIELD = "expiration_date|datetime"
_CUSTOM_INSTRUCTIONS_FIELD = "custom_instructions_html"
_EXPIRY_IDENTIFIER_FIELDS = (
    "machine_name",
    "gamekey",
    "steam_app_id",
    "human_name",
    "display_item_machine_name",
)
_CUSTOM_EXPIRATION_RE = re.compile(
    r"\bredeemed\s+by\s+"
    r"(?P<date>"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|"
    r"Nov|Dec)\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s+\d{4}"
    r")"
    r"(?:\s+by\s+(?P<time>\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?))?"
    r"(?:\s+(?P<timezone>(?:Pacific|Mountain|Central|Eastern)\s+Time|UTC|GMT))?",
    re.IGNORECASE,
)
_CUSTOM_TIMEZONE_NAMES = {
    "pacific time": "America/Los_Angeles",
    "mountain time": "America/Denver",
    "central time": "America/Chicago",
    "eastern time": "America/New_York",
    "utc": "UTC",
    "gmt": "UTC",
}


def _parse_custom_expiration(value: Any) -> tuple[str, datetime] | None:
    """Parse a Humble redemption deadline from custom instruction HTML."""
    if not isinstance(value, str):
        return None

    text = html.unescape(re.sub(r"<[^>]+>", " ", value))
    text = re.sub(r"\s+", " ", text).strip()
    match = _CUSTOM_EXPIRATION_RE.search(text)
    if match is None:
        return None

    date_text = re.sub(
        r"(\d)(?:st|nd|rd|th)\b", r"\1", match.group("date"), flags=re.IGNORECASE
    )
    date_text = re.sub(r"\s+", " ", date_text).strip()
    parsed_date = None
    for date_format in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            parsed_date = datetime.strptime(date_text, date_format)
            break
        except ValueError:
            continue
    if parsed_date is None:
        return None

    time_text = match.group("time")
    if time_text:
        time_text = re.sub(r"\.", "", time_text).upper()
        for time_format in ("%I:%M %p", "%I %p"):
            try:
                parsed_time = datetime.strptime(time_text, time_format).time()
                parsed_date = parsed_date.replace(
                    hour=parsed_time.hour,
                    minute=parsed_time.minute,
                )
                break
            except ValueError:
                continue
        else:
            return None
    else:
        parsed_date = parsed_date.replace(hour=23, minute=59, second=59)

    timezone_name = (match.group("timezone") or "UTC").casefold()
    timezone_id = _CUSTOM_TIMEZONE_NAMES.get(timezone_name, "UTC")
    try:
        expiration = parsed_date.replace(tzinfo=ZoneInfo(timezone_id))
    except Exception:
        expiration = parsed_date.replace(tzinfo=timezone.utc)

    expiration_utc = expiration.astimezone(timezone.utc)
    display_value = expiration_utc.replace(tzinfo=None).isoformat(timespec="seconds")
    return display_value, expiration_utc


def _has_unexpired_expiry(node: Any) -> bool:
    """Return whether *node* contains an unexpired key with an expiry date."""
    return any(
        entry.get(_EXPIRATION_FIELD) and not entry.get("is_expired", False)
        for entry in _expiry_entries(node)
    )


def _has_expired_expiry(node: Any) -> bool:
    """Return whether all expiry-bearing Steam keys on *node* are expired."""
    entries = _expiry_entries(node)
    return bool(entries) and all(entry.get("is_expired", False) for entry in entries)


def _is_steam_expiry_entry(entry: dict[str, Any]) -> bool:
    """Return whether an expiry-bearing entry describes a Steam key."""
    key_type = entry.get("key_type")
    machine_name = entry.get("machine_name")
    return (
        str(key_type).casefold() == "steam"
        or (
            isinstance(machine_name, str)
            and machine_name.casefold().endswith("_steam")
        )
        or entry.get("steam_app_id") is not None
    )


def _looks_like_key_entry(entry: dict[str, Any]) -> bool:
    """Return whether an expiry-bearing entry has platform/key metadata."""
    return any(
        field in entry
        for field in (
            "key_type",
            "machine_name",
            "gamekey",
            "steam_app_id",
            "is_expired",
            "num_days_until_expired",
        )
    )


def _expiry_entries(node: Any) -> list[dict[str, Any]]:
    """Return expiry entries, preferring Steam keys when platforms are mixed."""
    entries = [
        entry
        for entry in find_dict_keys(node, _EXPIRATION_FIELD, parent=True)
        if isinstance(entry, dict)
    ]
    custom_entries = []
    now = datetime.now(timezone.utc)
    for entry in find_dict_keys(node, _CUSTOM_INSTRUCTIONS_FIELD, parent=True):
        if not isinstance(entry, dict) or entry.get(_EXPIRATION_FIELD):
            continue
        parsed = _parse_custom_expiration(entry.get(_CUSTOM_INSTRUCTIONS_FIELD))
        if parsed is None:
            continue
        expiration, expiration_at = parsed
        custom_entry = dict(entry)
        custom_entry[_EXPIRATION_FIELD] = expiration
        custom_entry["is_expired"] = entry.get(
            "is_expired", expiration_at <= now
        )
        custom_entries.append(custom_entry)
    entries.extend(custom_entries)
    key_entries = [entry for entry in entries if _looks_like_key_entry(entry)]
    steam_entries = [entry for entry in key_entries if _is_steam_expiry_entry(entry)]
    if key_entries:
        return steam_entries
    return entries


def get_steam_expiration(node: Any) -> str | None:
    """Return the expiry date from the relevant Steam key entry, if present."""
    for entry in _expiry_entries(node):
        expiration = entry.get(_EXPIRATION_FIELD)
        if expiration:
            return str(expiration)
    return None


def get_expiring_game_identifiers(
    humble_session, order_details: list[dict]
) -> dict[str, set[str]]:
    """Fetch Choice pages and collect identifiers for games with an expiry date."""
    identifiers = {field: set() for field in _EXPIRY_IDENTIFIER_FIELDS}
    choice_urls = {
        order.get("product", {}).get("choice_url")
        for order in order_details
        if order.get("product", {}).get("choice_url")
    }

    for choice_url in choice_urls:
        try:
            choice_data = get_month_data(
                humble_session, {"product": {"choice_url": choice_url}}
            )
        except Exception as exc:
            print_warning(f"Couldn't check expiry for {choice_url}: {exc}")
            continue

        expiry_entries = list(
            find_dict_keys(choice_data, _EXPIRATION_FIELD, parent=True)
        )
        expiry_entries.extend(
            entry
            for entry in find_dict_keys(
                choice_data, _CUSTOM_INSTRUCTIONS_FIELD, parent=True
            )
            if entry not in expiry_entries
        )
        for game in expiry_entries:
            if not _has_unexpired_expiry(game):
                continue
            for field in _EXPIRY_IDENTIFIER_FIELDS:
                value = game.get(field)
                if value is not None:
                    identifiers[field].add(str(value).strip().casefold())

    return identifiers


def filter_expiring_keys(
    humble_session, order_details: list[dict], keys: list[dict]
) -> list[dict]:
    """Return only keys whose Humble Choice data includes an expiry date."""
    identifiers = get_expiring_game_identifiers(humble_session, order_details)
    expiring_keys: list[dict] = []

    for key in keys:
        if _has_unexpired_expiry(key):
            expiring_keys.append(key)
            continue
        if any(
            value is not None
            and str(value).strip().casefold() in identifiers[field]
            for field in _EXPIRY_IDENTIFIER_FIELDS
            for value in [key.get(field)]
        ):
            expiring_keys.append(key)

    return expiring_keys


def get_choices(
    humble_session,
    order_details: list[dict],
    *,
    only_expiring: bool = False,
    start_bundle: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> Generator[dict, None, None]:
    """Yield Humble Choice months that still have unchosen games.

    Handles both the classic "choose N of X" months (subs v2) and the newer
    "unlock everything" months (subs v3). v3 months report
    ``choices_remaining == 0`` and expose their games under
    ``contentChoiceData["game_data"]`` rather than ``content_choices``, so they
    are gated and parsed separately below. When *only_expiring* is true, only
    games with an expiry date that have not expired are considered. When
    *start_bundle* is set, iteration begins at that choice URL after sorting
    months by their ``created`` value in ascending order.
    """
    months = [
        month
        for month in order_details
        if month["product"].get("category") == "subscriptioncontent"
        and "choice_url" in month["product"]
    ]

    months = sorted(months, key=lambda m: m.get("created", ""))
    if start_bundle is not None:
        start_index = next(
            (
                index
                for index, month in enumerate(months)
                if month["product"].get("choice_url") == start_bundle
            ),
            None,
        )
        if start_index is None:
            raise ValueError(
                f"Choice start bundle {start_bundle!r} was not found in your orders."
            )
        months = months[start_index:]
    total_months = len(months)

    for index, month in enumerate(months, 1):
        month_name = month["product"].get(
            "human_name", month["product"].get("choice_url", "unknown")
        )
        if progress:
            progress(f"Loading Choice month {index}/{total_months}: {month_name}")
        is_v3 = month["product"].get("is_subs_v3_product", False)

        # v3 unlock-all months don't advertise a choice count but still have
        # claimable games, so surface them explicitly alongside classic months.
        if not (month.get("choices_remaining", 0) > 0 or is_v3):
            continue

        chosen_games = set(find_dict_keys(month.get("tpkd_dict", {}), "machine_name"))

        if progress:
            progress(f"Checking available games for {month_name}…")
        month["choice_data"] = get_month_data(humble_session, month)

        # Some months (fully region-locked, expired, etc.) can't be redeemed.
        if not month["choice_data"].get("canRedeemGames", True):
            continue

        content_choice_data = month["choice_data"]["contentChoiceData"]
        uses_choices = month["choice_data"].get("usesChoices", True)

        if not uses_choices:
            # v3: every game lives under game_data and the whole set is claimable.
            identifier = "initial"
            choice_options = content_choice_data.get("game_data", {})
        else:
            identifier = (
                "initial"
                if "initial" in content_choice_data
                else "initial-classic"
            )

            if identifier not in content_choice_data:
                for key in content_choice_data:
                    if "content_choices" in content_choice_data[key]:
                        identifier = key

            choice_options = content_choice_data[identifier]["content_choices"]

        choice_options = {
            name: game
            for name, game in choice_options.items()
            if not _has_expired_expiry(game)
            and (not only_expiring or _has_unexpired_expiry(game))
        }

        month["available_choices"] = [
            game[1]
            for game in choice_options.items()
            if set(find_dict_keys(game[1], "machine_name")).isdisjoint(chosen_games)
        ]

        month["uses_choices"] = uses_choices
        month["parent_identifier"] = identifier

        # Skip months with nothing left to claim (fully-chosen v2 or v3).
        if month["available_choices"]:
            if progress:
                progress(
                    f"Found {len(month['available_choices'])} available games "
                    f"in {month_name}"
                )
            yield month
