"""Humble Choice game selector."""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path
from typing import Any

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from InquirerPy.prompts.checkbox import CheckboxPrompt
from rich.markup import escape
from rich.prompt import Prompt

from src.humble_api import (
    HUMBLE_CHOOSE_CONTENT,
    HUMBLE_HEADERS,
    HUMBLE_ORDER_DETAILS_API,
    HUMBLE_SUB_PAGE,
    filter_expiring_keys,
    get_choices,
)
from src.redeemer import redeem_steam_keys
from src.utils import (
    cls,
    console,
    find_dict_keys,
    print_error,
    print_info,
    print_rule,
    print_success,
    print_warning,
    prompt_yes_no,
)


class _CountingCheckbox(CheckboxPrompt):
    """CheckboxPrompt that shows live `(N/MAX selected)` in the instruction line."""

    def __init__(self, *args: Any, max_selected: int, **kwargs: Any) -> None:
        self._max_selected = max_selected
        super().__init__(*args, **kwargs)

    @property
    def instruction(self) -> str:
        try:
            n = len(self.selected_choices)
        except Exception:
            n = 0
        return f"({n}/{self._max_selected} selected, space=toggle, enter=confirm)"


def _choice_label(choice: dict[str, Any]) -> str:
    """Format a Choice game name and rating for the game list."""
    parts = [choice["title"]]
    rating = choice.get("user_rating") or {}
    review = rating.get("review_text")
    pct = rating.get("steam_percent|decimal")
    if review and pct is not None:
        parts.append(f"  — {review.replace('_', ' ')} ({int(pct * 100)}%)")
    elif review:
        parts.append(f"  — {review.replace('_', ' ')}")
    if "tpkds" not in choice:
        parts.append("  [must redeem via Humble]")
    return "".join(parts)


def _log_full_response_error(
    action: str, response: Any, error: BaseException
) -> None:
    """Write the complete non-JSON response to the error log."""
    status = getattr(response, "status_code", "unknown")
    url = getattr(response, "url", "unknown")
    response_headers = getattr(response, "headers", {}) or {}
    content_type = (
        response_headers.get("Content-Type")
        or response_headers.get("content-type")
        or "unknown"
    )
    body = getattr(response, "text", "")
    if not isinstance(body, str) or not body:
        body = "<empty response body>"
    print(
        f"{action}: {error!r}\n"
        f"URL: {url}\n"
        f"HTTP {status}, {content_type}\n"
        f"Response body:\n{body}\n"
        "--- end response ---",
        file=sys.stderr,
    )


def choose_games(
    humble_session,
    choice_month_name: str,
    identifier: str,
    chosen: list[dict[str, Any]],
    *,
    order_gamekey: str | None = None,
) -> list[str]:
    """Submit chosen games for a Humble Choice month.

    The Choice endpoint expects the month's order gamekey, not the individual
    game's reveal key. Returns a list of failed titles.
    """
    failed: list[str] = []
    headers = {
        **HUMBLE_HEADERS,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": f"{HUMBLE_SUB_PAGE}{choice_month_name}",
        "X-Requested-With": "XMLHttpRequest",
    }
    for choice in chosen:
        display_name = choice["display_item_machine_name"]
        if "tpkds" not in choice:
            url = f"{HUMBLE_SUB_PAGE}{choice_month_name}/{display_name}"
            console.print(f"[cyan]Open in browser:[/cyan] {url}")
            webbrowser.open(url)
        else:
            payload = {
                "gamekey": order_gamekey or choice["tpkds"][0]["gamekey"],
                "parent_identifier": identifier,
                "chosen_identifiers[]": display_name,
                "is_multikey_and_from_choice_modal": "false",
            }
            response = None
            try:
                response = humble_session.post(
                    HUMBLE_CHOOSE_CONTENT, data=payload, headers=headers
                )
                res = response.json()
            except ValueError as e:
                status = getattr(response, "status_code", "unknown")
                response_headers = getattr(response, "headers", {}) or {}
                content_type = (
                    response_headers.get("Content-Type")
                    or response_headers.get("content-type")
                    or "unknown"
                )
                message = (
                    f"Error choosing {escape(choice['title'])}: Humble returned "
                    f"a non-JSON response (HTTP {status}, {content_type})"
                )
                print_error(message)
                _log_full_response_error(
                    f"choose_games non-JSON response for {choice['title']!r}",
                    response,
                    e,
                )
                failed.append(choice["title"])
                continue
            except Exception as e:
                print_error(f"Error choosing {escape(choice['title'])}: {e}")
                print(
                    f"choose_games exception for {choice['title']!r}: {e!r}",
                    file=sys.stderr,
                )
                failed.append(choice["title"])
                continue
            if not isinstance(res, dict) or not res.get("success"):
                print_error(f"Error choosing {escape(choice['title'])}")
                console.print(res)
                print(
                    f"choose_games failure for {choice['title']!r}: {res!r}",
                    file=sys.stderr,
                )
                failed.append(choice["title"])
            else:
                print_success(f"Chose game {escape(choice['title'])}")
    return failed


def _refresh_choice_order(humble_session, order: str) -> dict[str, Any] | None:
    """Fetch refreshed order data, returning None when Humble did not send JSON."""
    response = None
    try:
        response = humble_session.get(
            f"{HUMBLE_ORDER_DETAILS_API}{order}?all_tpkds=true"
        )
        data = response.json()
    except ValueError as e:
        status = getattr(response, "status_code", "unknown")
        response_headers = getattr(response, "headers", {}) or {}
        content_type = (
            response_headers.get("Content-Type")
            or response_headers.get("content-type")
            or "unknown"
        )
        message = (
            f"Couldn't refresh Choice order {escape(order)}: Humble returned "
            f"a non-JSON response (HTTP {status}, {content_type})"
        )
        print_error(message)
        _log_full_response_error(
            f"choice order refresh non-JSON response for {order!r}",
            response,
            e,
        )
        return None
    except Exception as e:
        print_error(f"Couldn't refresh Choice order {escape(order)}: {e}")
        print(
            f"choice order refresh exception for {order!r}: {e!r}",
            file=sys.stderr,
        )
        return None

    if not isinstance(data, dict):
        print_error(f"Couldn't refresh Choice order {escape(order)}: invalid response")
        print(
            f"choice order refresh returned {data!r} for {order!r}",
            file=sys.stderr,
        )
        return None
    return data


def humble_chooser_mode(
    humble_session,
    order_details: list[dict[str, Any]],
    *,
    steam_cookies: str | Path | None = None,
    only_expiring: bool = False,
    start_bundle: str | None = None,
) -> None:
    """Interactive Humble Choice game selection UI."""
    try_redeem_keys: list[str] = []
    loading_status = None

    def update_loading_status(message: str) -> None:
        if loading_status is not None:
            loading_status.update(message)

    choice_months = iter(
        get_choices(
            humble_session,
            order_details,
            only_expiring=only_expiring,
            start_bundle=start_bundle,
            progress=update_loading_status,
        )
    )
    first = True
    redeem_keys = False

    while True:
        with console.status(
            "Loading next Humble Choice month…", spinner="dots"
        ) as status:
            loading_status = status
            try:
                month = next(choice_months)
            except StopIteration:
                break
            except ValueError as e:
                print_error(str(e))
                return
            finally:
                loading_status = None

        redeem_all = None
        if first:
            redeem_keys = prompt_yes_no(
                "After all months are chosen, sign into Steam and redeem the keys?"
            )
            first = False

        ready = False
        while not ready:
            cls()
            choices = month["available_choices"]
            if month.get("uses_choices", True):
                remaining = month["choices_remaining"]
                label = f"[cyan]{remaining}[/cyan] choices remaining"
            else:
                # v3 unlock-all months: every available game can be claimed.
                remaining = len(choices)
                label = f"[cyan]{remaining}[/cyan] games to claim"

            month_name = escape(month["product"]["human_name"])
            print_rule(f"{month_name}  ·  {label}")

            def _label(choice: dict[str, Any]) -> str:
                return _choice_label(choice)

            if redeem_all is None and remaining == len(choices):
                console.print("[bold]Games:[/bold]")
                for choice in choices:
                    console.print(f"  {escape(_label(choice))}")
                console.print()
                redeem_all = prompt_yes_no("Redeem all?")
            else:
                redeem_all = False

            if redeem_all:
                chosen = list(choices)
            else:
                console.print()
                console.print(
                    "[dim]Submit empty for more options (browser / skip).[/dim]"
                )
                console.print()

                checkbox_choices = [
                    Choice(value=idx, name=_label(choice))
                    for idx, choice in enumerate(choices)
                ]

                try:
                    selected_indexes = _CountingCheckbox(
                        message=f"Pick up to {remaining} for {month['product']['human_name']}:",
                        choices=checkbox_choices,
                        max_selected=remaining,
                        transformer=lambda result: f"{len(result)} selected",
                        validate=lambda result: len(result) <= remaining,
                        invalid_message=f"Pick at most {remaining}",
                    ).execute()
                except KeyboardInterrupt:
                    raise

                if not selected_indexes:
                    next_action = inquirer.select(
                        message="No games selected. What now?",
                        choices=[
                            "Skip this month",
                            "Open this month in browser",
                            "Re-pick",
                        ],
                    ).execute()

                    if next_action == "Skip this month":
                        ready = True
                        continue
                    if next_action == "Open this month in browser":
                        url = HUMBLE_SUB_PAGE + month["product"]["choice_url"]
                        console.print(f"[cyan]Open in browser:[/cyan] {url}")
                        webbrowser.open(url)
                        Prompt.ask(
                            "[dim]Press Enter once you've made your picks in the browser[/dim]",
                            default="",
                        )
                        if redeem_keys:
                            try_redeem_keys.append(month["gamekey"])
                        ready = True
                        continue
                    # else "Re-pick" — fall through and the loop redraws
                    continue

                chosen = [choices[i] for i in selected_indexes]

            console.print()
            console.print("[bold]Selected:[/bold]")
            for choice in chosen:
                console.print(f"  [green]{escape(choice['title'])}[/green]")
            console.print()
            if prompt_yes_no("Confirm selection?"):
                choice_month_name = month["product"]["choice_url"]
                identifier = month["parent_identifier"]
                failed = choose_games(
                    humble_session,
                    choice_month_name,
                    identifier,
                    chosen,
                    order_gamekey=month.get("gamekey"),
                )
                if failed:
                    print_error(
                        f"{len(failed)} pick(s) failed — see above and error.log"
                    )
                    Prompt.ask(
                        "[dim]Press Enter to continue[/dim]", default=""
                    )
                    # Stay on this month so the user can retry
                    continue
                if redeem_keys:
                    try_redeem_keys.append(month["gamekey"])
                ready = True

    if first:
        print_info("No Humble Choices need choosing — you're all up-to-date!")
    else:
        print_info("No more unchosen Humble Choices")
        if redeem_keys and try_redeem_keys:
            print_success("Redeeming keys now!")
            updated_monthlies = [
                refreshed
                for order in try_redeem_keys
                if (refreshed := _refresh_choice_order(humble_session, order))
                is not None
            ]
            if len(updated_monthlies) != len(try_redeem_keys):
                print_warning(
                    f"Couldn't refresh {len(try_redeem_keys) - len(updated_monthlies)} "
                    "selected Choice order(s); continuing with the rest."
                )
            if updated_monthlies:
                chosen_keys = list(
                    find_dict_keys(updated_monthlies, "steam_app_id", True)
                )
                if only_expiring:
                    original_length = len(chosen_keys)
                    chosen_keys = filter_expiring_keys(
                        humble_session, updated_monthlies, chosen_keys
                    )
                    print_info(
                        f"Filtered {original_length - len(chosen_keys)} keys without "
                        "an expiry date"
                    )
                redeem_steam_keys(
                    humble_session, chosen_keys, steam_cookies=steam_cookies
                )
