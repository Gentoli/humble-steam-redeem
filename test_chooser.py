"""Regression tests for Humble Choice filtering and interruption handling."""

import json
from unittest.mock import patch

import src.chooser as chooser
from src.humble_api import HUMBLE_SUB_PAGE, get_choices


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


def _month(choice_url):
    return {
        "product": {
            "category": "subscriptioncontent",
            "choice_url": choice_url,
            "human_name": choice_url,
        },
        "choices_remaining": 0,
        "is_subs_v3_product": True,
        "tpkd_dict": {},
    }


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
        )
    )

    assert [month["product"]["choice_url"] for month in months] == ["mixed"]
    assert [choice["title"] for choice in months[0]["available_choices"]] == [
        "Future Game"
    ]


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
    test_ctrl_c_exits_chooser_instead_of_advancing()
    print("Chooser tests passed")
