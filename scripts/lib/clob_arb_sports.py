from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from lib.runtime_common import iso_now, parse_iso_or_epoch_to_ms


ESPN_SCOREBOARD_BASE = "https://site.api.espn.com/apis/site/v2/sports"
DEFAULT_ESPN_SCOREBOARD_PATHS = (
    "basketball/nba",
    "basketball/mens-college-basketball",
    "football/nfl",
    "baseball/mlb",
    "hockey/nhl",
    "soccer/eng.1",
    "soccer/esp.1",
    "soccer/ita.1",
    "soccer/ger.1",
    "soccer/fra.1",
    "soccer/usa.1",
    "soccer/uefa.champions",
)


@dataclass(frozen=True)
class SportsFeedEvent:
    provider: str
    source_path: str
    event_id: str
    state: str
    start_ms: Optional[int]
    expected_duration_ms: int
    home_team: str
    away_team: str
    home_aliases: Tuple[str, ...]
    away_aliases: Tuple[str, ...]
    short_name: str


def _normalize_text_key(value: str) -> str:
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = s.encode("ascii", errors="ignore").decode("ascii", errors="ignore").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_sports_market_type(value: str) -> str:
    raw = _normalize_text_key(value).replace(" ", "")
    aliases = {
        "moneyline": "moneyline",
        "winner": "moneyline",
        "win": "moneyline",
        "spread": "spread",
        "handicap": "spread",
        "total": "total",
        "totals": "total",
        "ou": "total",
        "overunder": "total",
        "draw": "draw",
        "btts": "btts",
        "bothteamstoscore": "btts",
    }
    return aliases.get(raw, "other")


def parse_sports_market_type_filter(value: str) -> Set[str]:
    out: Set[str] = set()
    for part in str(value or "").split(","):
        raw = _normalize_text_key(part)
        if not raw:
            continue
        t = _normalize_sports_market_type(raw)
        if t == "other" and raw not in {"other"}:
            continue
        out.add(t)
    return out


def gamma_first_event(m: dict) -> dict:
    events = m.get("events")
    if isinstance(events, list):
        for e in events:
            if isinstance(e, dict):
                return e
    return {}


def infer_sports_market_type(m: dict) -> str:
    t = _normalize_sports_market_type(str(m.get("sportsMarketType") or ""))
    if t != "other":
        return t

    e0 = gamma_first_event(m)
    q = str(m.get("question") or e0.get("title") or "").lower()
    if not q:
        return "other"

    if "both teams to score" in q or "btts" in q:
        return "btts"
    if "draw" in q or "end in a draw" in q:
        return "draw"
    if "o/u" in q or "over/under" in q:
        return "total"
    if "spread:" in q or "handicap" in q:
        return "spread"
    if "moneyline" in q or re.search(r"\bto win\b", q) or re.search(r"\bwin on\b", q):
        return "moneyline"
    if " vs. " in q or " vs " in q or " @ " in q:
        return "moneyline"
    return "other"


def _cleanup_team_fragment(value: str) -> str:
    s = _normalize_text_key(value)
    if s.startswith("will "):
        s = s[5:].strip()
    return s


def extract_matchup_teams_from_text(value: str) -> Optional[Tuple[str, str]]:
    if not value:
        return None

    s = unicodedata.normalize("NFKD", str(value or ""))
    s = s.encode("ascii", errors="ignore").decode("ascii", errors="ignore")
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None

    m = re.search(r"(.+?)\s+(?:vs\.?|v\.?|@)\s+(.+?)(?:\s*[:|\-]|$)", s, flags=re.IGNORECASE)
    if not m:
        return None

    a = _cleanup_team_fragment(m.group(1))
    b = _cleanup_team_fragment(m.group(2))
    if not a or not b or a == b:
        return None

    return (a, b) if a < b else (b, a)


def market_matchup_pair(m: dict) -> Optional[Tuple[str, str]]:
    e0 = gamma_first_event(m)
    candidates = [
        str(m.get("question") or ""),
        str(e0.get("title") or ""),
        str(e0.get("name") or ""),
        str(e0.get("slug") or ""),
        str(m.get("slug") or ""),
    ]
    for text in candidates:
        pair = extract_matchup_teams_from_text(text)
        if pair is not None:
            return pair
    return None


def _expected_duration_ms_from_path(path: str) -> int:
    sport = str(path or "").split("/", 1)[0].strip().lower()
    if sport == "soccer":
        return int(150 * 60_000)
    if sport == "football":
        return int(240 * 60_000)
    if sport == "baseball":
        return int(210 * 60_000)
    if sport == "hockey":
        return int(180 * 60_000)
    if sport == "basketball":
        return int(180 * 60_000)
    return int(180 * 60_000)


def _unique_aliases(values: List[str]) -> Tuple[str, ...]:
    out: List[str] = []
    seen: Set[str] = set()
    for v in values:
        n = _cleanup_team_fragment(v)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return tuple(out)


def _iter_espn_feed_events(paths: List[str], timeout_sec: float = 5.0) -> Tuple[List[SportsFeedEvent], List[str]]:
    events: List[SportsFeedEvent] = []
    warnings: List[str] = []

    for path in paths:
        p = str(path or "").strip().strip("/")
        if not p:
            continue
        url = f"{ESPN_SCOREBOARD_BASE}/{p}/scoreboard"
        try:
            req = Request(url, headers={"User-Agent": "clob-arb-monitor/1.0"})
            with urlopen(req, timeout=max(1.0, float(timeout_sec or 5.0))) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            warnings.append(f"espn:{p} HTTP {int(getattr(e, 'code', 0) or 0)}")
            continue
        except URLError:
            warnings.append(f"espn:{p} URLError")
            continue
        except Exception as e:
            warnings.append(f"espn:{p} {type(e).__name__}")
            continue

        for item in payload.get("events") or []:
            if not isinstance(item, dict):
                continue
            status_t = ((item.get("status") or {}).get("type") or {}) if isinstance(item.get("status"), dict) else {}
            state_raw = str(status_t.get("state") or "").strip().lower()
            if state_raw in {"in", "live"}:
                state = "live"
            elif state_raw in {"pre"}:
                state = "pre"
            elif state_raw in {"post"}:
                state = "post"
            else:
                state = "unknown"

            start_ms = parse_iso_or_epoch_to_ms(item.get("date"))

            home = ""
            away = ""
            home_alias_raw: List[str] = []
            away_alias_raw: List[str] = []
            comps = item.get("competitions")
            if isinstance(comps, list) and comps:
                comp0 = comps[0] if isinstance(comps[0], dict) else {}
                competitors = comp0.get("competitors")
                if isinstance(competitors, list):
                    for c in competitors:
                        if not isinstance(c, dict):
                            continue
                        t = c.get("team") if isinstance(c.get("team"), dict) else {}
                        if not isinstance(t, dict):
                            t = {}
                        alias_values = [
                            str(t.get("displayName") or ""),
                            str(t.get("shortDisplayName") or ""),
                            str(t.get("name") or ""),
                            str(t.get("abbreviation") or ""),
                            f"{str(t.get('location') or '').strip()} {str(t.get('name') or '').strip()}".strip(),
                        ]
                        home_away = str(c.get("homeAway") or "").strip().lower()
                        if home_away == "home":
                            if not home:
                                home = str(t.get("displayName") or t.get("shortDisplayName") or "").strip()
                            home_alias_raw.extend(alias_values)
                        elif home_away == "away":
                            if not away:
                                away = str(t.get("displayName") or t.get("shortDisplayName") or "").strip()
                            away_alias_raw.extend(alias_values)

            if not home or not away:
                n = str(item.get("name") or "")
                m = re.search(r"(.+?)\s+at\s+(.+)", n, flags=re.IGNORECASE)
                if m:
                    away = away or m.group(1).strip()
                    home = home or m.group(2).strip()
                    away_alias_raw.append(away)
                    home_alias_raw.append(home)

            home_aliases = _unique_aliases(home_alias_raw)
            away_aliases = _unique_aliases(away_alias_raw)
            if not home_aliases or not away_aliases:
                continue

            events.append(
                SportsFeedEvent(
                    provider="espn",
                    source_path=p,
                    event_id=str(item.get("id") or "").strip(),
                    state=state,
                    start_ms=start_ms,
                    expected_duration_ms=_expected_duration_ms_from_path(p),
                    home_team=home,
                    away_team=away,
                    home_aliases=home_aliases,
                    away_aliases=away_aliases,
                    short_name=str(item.get("shortName") or item.get("name") or "").strip(),
                )
            )

    return events, warnings


def _sports_feed_event_rank(e: SportsFeedEvent) -> int:
    if e.state == "live":
        return 30
    if e.state == "pre":
        return 20
    if e.state == "post":
        return 10
    return 0


def build_sports_feed_snapshot(provider: str, espn_paths: List[str], timeout_sec: float, logger) -> dict:
    p = str(provider or "none").strip().lower()
    if p != "espn":
        return {"provider": "none", "fetched_ms": int(time.time() * 1000), "events": [], "pair_index": {}}

    events, warnings = _iter_espn_feed_events(paths=espn_paths, timeout_sec=timeout_sec)
    pair_index: Dict[Tuple[str, str], SportsFeedEvent] = {}
    for e in events:
        for a in e.home_aliases:
            for b in e.away_aliases:
                if not a or not b or a == b:
                    continue
                key = (a, b) if a < b else (b, a)
                prev = pair_index.get(key)
                if prev is None:
                    pair_index[key] = e
                    continue
                if _sports_feed_event_rank(e) > _sports_feed_event_rank(prev):
                    pair_index[key] = e
                    continue
                if _sports_feed_event_rank(e) == _sports_feed_event_rank(prev):
                    now_ms = int(time.time() * 1000)
                    prev_dist = abs((prev.start_ms or now_ms) - now_ms)
                    new_dist = abs((e.start_ms or now_ms) - now_ms)
                    if new_dist < prev_dist:
                        pair_index[key] = e

    if warnings:
        logger.info(f"[{iso_now()}] sports-feed warnings: {', '.join(warnings[:5])}")
    logger.info(
        f"[{iso_now()}] sports-feed provider=espn paths={len(espn_paths)} "
        f"events={len(events)} pair_keys={len(pair_index)}"
    )
    return {
        "provider": "espn",
        "fetched_ms": int(time.time() * 1000),
        "events": events,
        "pair_index": pair_index,
    }


def _sports_feed_window_match(
    m: dict,
    now_ms: int,
    prestart_min: float,
    postend_min: float,
    sports_feed_snapshot: Optional[dict],
    sports_feed_live_buffer_sec: float,
) -> Optional[bool]:
    if not isinstance(sports_feed_snapshot, dict):
        return None

    pair_index = sports_feed_snapshot.get("pair_index")
    if not isinstance(pair_index, dict) or not pair_index:
        return None

    key = market_matchup_pair(m)
    if key is None:
        return None

    event = pair_index.get(key)
    if not isinstance(event, SportsFeedEvent):
        return None

    pre_ms = int(max(0.0, float(prestart_min or 0.0)) * 60_000)
    post_ms = int(max(0.0, float(postend_min or 0.0)) * 60_000)
    buffer_ms = int(max(0.0, float(sports_feed_live_buffer_sec or 0.0)) * 1000)
    start_ms = event.start_ms

    if event.state == "live":
        return True

    if start_ms is None:
        return False

    if event.state == "pre":
        return (start_ms - pre_ms) <= now_ms <= (start_ms + buffer_ms)

    if event.state == "post":
        end_est_ms = int(start_ms + int(event.expected_duration_ms or 0))
        return (start_ms - pre_ms) <= now_ms <= (end_est_ms + post_ms + buffer_ms)

    return (start_ms - pre_ms) <= now_ms <= (start_ms + post_ms + buffer_ms)


def is_likely_sports_market(m: dict) -> bool:
    if m.get("sportsMarketType"):
        return True

    e0 = gamma_first_event(m)
    sports_keys = ("gameId", "startTime", "finishedTimestamp", "score", "period", "elapsed")
    if any(e0.get(k) not in (None, "") for k in sports_keys):
        return True

    slug = str(e0.get("slug") or m.get("slug") or "").lower()
    if slug.startswith(("nba-", "nfl-", "mlb-", "nhl-", "cbb-", "ncaa-", "epl-", "khl-", "soccer-")):
        return True

    q = str(m.get("question") or e0.get("title") or "").lower()
    return (" vs. " in q) or (" vs " in q) or (" @ " in q)


def is_in_sports_live_window(
    m: dict,
    now_ms: int,
    prestart_min: float,
    postend_min: float,
    sports_feed_snapshot: Optional[dict] = None,
    sports_feed_live_buffer_sec: float = 90.0,
    sports_feed_strict: bool = False,
) -> bool:
    if not is_likely_sports_market(m):
        hit = _sports_feed_window_match(
            m=m,
            now_ms=now_ms,
            prestart_min=prestart_min,
            postend_min=postend_min,
            sports_feed_snapshot=sports_feed_snapshot,
            sports_feed_live_buffer_sec=sports_feed_live_buffer_sec,
        )
        return bool(hit)

    hit = _sports_feed_window_match(
        m=m,
        now_ms=now_ms,
        prestart_min=prestart_min,
        postend_min=postend_min,
        sports_feed_snapshot=sports_feed_snapshot,
        sports_feed_live_buffer_sec=sports_feed_live_buffer_sec,
    )
    if hit is not None:
        return bool(hit)
    if sports_feed_strict and isinstance(sports_feed_snapshot, dict) and sports_feed_snapshot.get("provider") != "none":
        return False

    e0 = gamma_first_event(m)
    now = int(now_ms)
    pre_ms = int(max(0.0, prestart_min) * 60_000)
    post_ms = int(max(0.0, postend_min) * 60_000)

    st = parse_iso_or_epoch_to_ms(e0.get("startTime")) or parse_iso_or_epoch_to_ms(m.get("startDate"))
    et = parse_iso_or_epoch_to_ms(e0.get("finishedTimestamp")) or parse_iso_or_epoch_to_ms(m.get("endDate"))

    if st and et:
        return (st - pre_ms) <= now <= (et + post_ms)
    if st:
        return (st - pre_ms) <= now <= (st + post_ms)
    if et:
        return now <= (et + post_ms)

    return True

