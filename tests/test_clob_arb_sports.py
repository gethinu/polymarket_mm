from __future__ import annotations

from lib.clob_arb_sports import (
    extract_matchup_teams_from_text,
    market_matchup_pair,
    parse_sports_market_type_filter,
)


def test_parse_sports_market_type_filter_normalizes_aliases():
    actual = parse_sports_market_type_filter("winner, overunder, draw, other, unknown")
    assert actual == {"moneyline", "total", "draw", "other"}


def test_extract_matchup_teams_from_text_orders_and_normalizes():
    pair = extract_matchup_teams_from_text("Will Lakers vs Celtics: tonight")
    assert pair == ("celtics", "lakers")


def test_market_matchup_pair_checks_market_then_event_fields():
    market = {
        "question": "",
        "slug": "",
        "events": [
            {
                "title": "Yankees @ Red Sox",
                "name": "",
                "slug": "",
            }
        ],
    }
    assert market_matchup_pair(market) == ("red sox", "yankees")

