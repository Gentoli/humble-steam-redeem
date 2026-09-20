"""Regression tests for Humble Choice filtering and interruption handling."""

import io
import json
from contextlib import redirect_stderr
from unittest.mock import patch

import src.chooser as chooser
import src.__main__ as app
from src.humble_api import (
    HUMBLE_CHOOSE_CONTENT,
    HUMBLE_ORDER_DETAILS_API,
    HUMBLE_SUB_PAGE,
    get_choices,
)


_DATA_MARKER = '<script id="webpack-monthly-product-data" type="application/json">'


class _Response:
    status_code = 200

    def __init__(self, choice_data):
        self.text = (
            _DATA_MARKER
            + json.dumps({"contentChoiceOptions": choice_data})
            + "</script>"
        )


class _Session:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url):
        return _Response(self.pages[url.removeprefix(HUMBLE_SUB_PAGE)])


def _month(choice_url, *, created=None):
    month = {
        "product": {
            "category": "subscriptioncontent",
            "choice_url": choice_url,
            "human_name": choice_url,
            "is_subs_v3_product": True,
        },
        "choices_remaining": 0,
        "tpkd_dict": {},
    }
    if created is not None:
        month["created"] = created
    return month


def _game(title, *, expiration=None, is_expired=False):
    tpkd = {
        "machine_name": f"{title.lower().replace(' ', '_')}_steam",
        "gamekey": f"{title}-key",
    }
    if expiration is not None:
        tpkd["expiration_date|datetime"] = expiration
        tpkd["is_expired"] = is_expired
    return {
        "display_item_machine_name": title.lower().replace(" ", "_"),
        "title": title,
        "tpkds": [tpkd],
    }


def _choice_data(*games):
    return {
        "canRedeemGames": True,
        "usesChoices": False,
        "contentChoiceData": {
            "game_data": {
                game["display_item_machine_name"]: game for game in games
            }
        },
    }


def test_only_expiring_skips_non_expiring_and_expired_months():
    progress = []
    session = _Session(
        {
            "mixed": _choice_data(
                _game("Future Game", expiration="2026-12-01T00:00:00"),
                _game("No Expiry"),
            ),
            "expired": _choice_data(
                _game(
                    "Expired Game",
                    expiration="2025-01-01T00:00:00",
                    is_expired=True,
                )
            ),
        }
    )

    months = list(
        get_choices(
            session,
            [_month("mixed"), _month("expired")],
            only_expiring=True,
            progress=progress.append,
        )
    )

    assert [month["product"]["choice_url"] for month in months] == ["mixed"]
    assert [choice["title"] for choice in months[0]["available_choices"]] == [
        "Future Game"
    ]
    assert progress[0] == "Loading Choice month 1/2: mixed"
    assert any("Checking available games for mixed" in action for action in progress)
    assert any("Found 1 available games in mixed" in action for action in progress)


def test_choice_months_are_oldest_first_from_start_bundle():
    session = _Session(
        {
            "january-2023": _choice_data(_game("January Game")),
            "november-2023": _choice_data(_game("November Game")),
            "march-2024": _choice_data(_game("March Game")),
        }
    )
    months = list(
        get_choices(
            session,
            [
                _month("march-2024", created="2024-03-01"),
                _month("january-2023", created="2023-01-01"),
                _month("november-2023", created="2023-11-01"),
            ],
            start_bundle="november-2023",
        )
    )

    assert [month["product"]["choice_url"] for month in months] == [
        "november-2023",
        "march-2024",
    ]


def test_unknown_start_bundle_is_reported_without_fetching_pages():
    session = _Session({"january-2023": _choice_data(_game("January Game"))})

    try:
        list(
            get_choices(
                session,
                [_month("january-2023", created="2023-01-01")],
                start_bundle="november-2023",
            )
        )
    except ValueError as e:
        assert str(e) == (
            "Choice start bundle 'november-2023' was not found in your orders."
        )
    else:
        raise AssertionError("Unknown start bundle should fail clearly")


def test_chooser_fetches_next_month_after_current_view():
    events = []

    def _view_month(title):
        return {
            "available_choices": [_game(title)],
            "uses_choices": False,
            "parent_identifier": "initial",
            "product": {"choice_url": title.lower(), "human_name": title},
        }

    first_month = _view_month("First Month")
    second_month = _view_month("Second Month")

    def _lazy_choices(*args, **kwargs):
        assert kwargs["start_bundle"] == "november-2023"

        def _months():
            events.append("first month fetched")
            yield first_month
            events.append("second month fetched")
            yield second_month

        return _months()

    def _prompt(question):
        if question == "After all months are chosen, sign into Steam and redeem the keys?":
            assert "second month fetched" not in events
            return False
        return True

    with (
        patch.object(chooser, "get_choices", side_effect=_lazy_choices),
        patch.object(chooser, "prompt_yes_no", side_effect=_prompt),
        patch.object(chooser, "choose_games", return_value=[]),
        patch.object(chooser, "cls"),
    ):
        chooser.humble_chooser_mode(object(), [], start_bundle="november-2023")

    assert events == ["first month fetched", "second month fetched"]


def test_choice_label_ends_with_expiry():
    choice = _game("Future Game", expiration="2026-12-01T00:00:00")
    choice["user_rating"] = {
        "review_text": "very_positive",
        "steam_percent|decimal": 0.95,
    }

    label = chooser._choice_label(choice)

    assert "Future Game" in label
    assert "very positive (95%)" in label
    assert label.endswith("exp: 2026-12-01T00:00:00")


def test_redeem_all_prompt_shows_game_list():
    month = {
        "available_choices": [
            _game("Future Game", expiration="2026-12-01T00:00:00"),
            _game("Another Game"),
        ],
        "uses_choices": False,
        "parent_identifier": "initial",
        "product": {"choice_url": "mixed", "human_name": "mixed"},
    }
    rendered: list[str] = []

    def _capture_print(*args, **kwargs):
        rendered.append(" ".join(str(arg) for arg in args))

    def _prompt(question):
        if question == "Redeem all?":
            assert "Future Game" in "\n".join(rendered)
            assert "Another Game" in "\n".join(rendered)
            return True
        if question == "Confirm selection?":
            return True
        return False

    with (
        patch.object(chooser, "get_choices", return_value=[month]),
        patch.object(chooser, "prompt_yes_no", side_effect=_prompt),
        patch.object(chooser, "choose_games", return_value=[]),
        patch.object(chooser, "cls"),
        patch.object(chooser.console, "print", side_effect=_capture_print),
    ):
        chooser.humble_chooser_mode(object(), [])


def test_choose_games_uses_order_key_and_ajax_headers():
    choice = _game("Future Game")
    requests = []

    class _Response:
        status_code = 200
        headers = {"Content-Type": "application/json"}

        def json(self):
            return {"success": True}

    class _Session:
        def post(self, url, *, data, headers):
            requests.append((url, data, headers))
            return _Response()

    assert (
        chooser.choose_games(
            _Session(),
            "mixed",
            "initial",
            [choice],
            order_gamekey="order-key",
        )
        == []
    )
    url, payload, headers = requests[0]
    assert url == HUMBLE_CHOOSE_CONTENT
    assert payload["gamekey"] == "order-key"
    assert payload["chosen_identifiers[]"] == "future_game"
    assert headers["Referer"] == f"{HUMBLE_SUB_PAGE}mixed"
    assert headers["X-Requested-With"] == "XMLHttpRequest"


def test_choose_games_marks_non_json_response_as_failed():
    class _Response:
        status_code = 403
        headers = {"Content-Type": "text/html"}
        url = HUMBLE_CHOOSE_CONTENT
        text = "<html>Cloudflare challenge details</html>"

        def json(self):
            raise ValueError("not JSON")

    class _Session:
        def post(self, url, *, data, headers):
            return _Response()

    log = io.StringIO()
    with redirect_stderr(log):
        assert chooser.choose_games(
            _Session(), "mixed", "initial", [_game("Future Game")]
        ) == ["Future Game"]
    output = log.getvalue()
    assert "HTTP 403, text/html" in output
    assert "Cloudflare challenge details" in output


def test_cli_parses_start_bundle_flag():
    args = app._parse_args(["--start-bundle", "november-2023"])
    assert args.choice_start == "november-2023"


def test_choice_order_refresh_handles_non_json_response():
    class _Response:
        status_code = 403
        headers = {"Content-Type": "text/html"}

        def json(self):
            raise ValueError("not JSON")

    class _Session:
        def get(self, url):
            assert url == f"{HUMBLE_ORDER_DETAILS_API}order-key?all_tpkds=true"
            return _Response()

    assert chooser._refresh_choice_order(_Session(), "order-key") is None


def test_choice_mode_skips_failed_order_refresh():
    month = {
        "available_choices": [_game("Future Game")],
        "uses_choices": False,
        "parent_identifier": "initial",
        "gamekey": "order-key",
        "product": {"choice_url": "mixed", "human_name": "mixed"},
    }

    class _Response:
        status_code = 502
        headers = {"Content-Type": "text/html"}

        def json(self):
            raise ValueError("not JSON")

    class _Session:
        def get(self, url):
            return _Response()

    with (
        patch.object(chooser, "get_choices", return_value=[month]),
        patch.object(chooser, "prompt_yes_no", return_value=True),
        patch.object(chooser, "choose_games", return_value=[]),
        patch.object(chooser, "redeem_steam_keys") as redeem,
        patch.object(chooser, "cls"),
    ):
        chooser.humble_chooser_mode(_Session(), [])

    redeem.assert_not_called()


def test_cli_returns_interrupt_status_without_traceback():
    with (
        patch.object(app, "main", side_effect=KeyboardInterrupt),
        patch.object(app.console, "print"),
    ):
        assert app.cli() == 130


def test_ctrl_c_exits_chooser_instead_of_advancing():
    month = {
        "available_choices": [_game("Future Game")],
        "uses_choices": False,
        "parent_identifier": "initial",
        "product": {"choice_url": "mixed", "human_name": "mixed"},
    }

    class _InterruptingCheckbox:
        def __init__(self, *args, **kwargs):
            pass

        def execute(self):
            raise KeyboardInterrupt

    with (
        patch.object(chooser, "get_choices", return_value=[month]),
        patch.object(chooser, "prompt_yes_no", return_value=False),
        patch.object(chooser, "_CountingCheckbox", _InterruptingCheckbox),
    ):
        try:
            chooser.humble_chooser_mode(object(), [], only_expiring=True)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("Ctrl+C should stop chooser mode")


if __name__ == "__main__":
    test_only_expiring_skips_non_expiring_and_expired_months()
    test_choice_months_are_oldest_first_from_start_bundle()
    test_unknown_start_bundle_is_reported_without_fetching_pages()
    test_chooser_fetches_next_month_after_current_view()
    test_choice_label_ends_with_expiry()
    test_redeem_all_prompt_shows_game_list()
    test_choose_games_uses_order_key_and_ajax_headers()
    test_choose_games_marks_non_json_response_as_failed()
    test_cli_parses_start_bundle_flag()
    test_choice_order_refresh_handles_non_json_response()
    test_choice_mode_skips_failed_order_refresh()
    test_cli_returns_interrupt_status_without_traceback()
    test_ctrl_c_exits_chooser_instead_of_advancing()
    print("Chooser tests passed")
