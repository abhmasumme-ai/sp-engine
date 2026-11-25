import asyncio
import re

from config import BET_STAKE


class MelbetEngine:
    # Fallback type hints (শুধু fallback — learning এর পর override হয়)
    DEFAULT_OVER_TYPES = {2066}
    DEFAULT_UNDER_TYPES = {2067}
    DEFAULT_WICKET_UNDER_TYPES = {13523}

    def __init__(self, page):
        self.page = page
        self.partner = 8
        self.group = 1184
        self.user_id = 0

        # runtime-learned type sets
        self._learned_over_types = set()
        self._learned_under_types = set()
        self._learned_wicket_under_types = set()

    async def get_user_id(self):
        if self.user_id != 0:
            return self.user_id
        js = "() => { try { return parseInt(localStorage.getItem('userId')); } catch(e) { return 0; } }"
        try:
            uid = await self.page.evaluate(js)
            if uid and uid > 0:
                self.user_id = uid
        except Exception:
            pass
        return self.user_id

    async def get_latest_odds(self, match_game_id):
        url = (
            f"/service-api/main-live-feed/v3/gameEvents"
            f"?cfView=3&countEvents=250&fcountry=19&gameId={match_game_id}"
            f"&gr=1184&grMode=4&lng=en&marketType=1"
        )
        js_fetch = "async (url) => { const res = await fetch(url); return await res.json(); }"
        try:
            return await self.page.evaluate(js_fetch, url)
        except Exception as e:
            print(f"❌ Odds fetch error: {e}")
            return None

    # ---------- iter helpers ----------
    @staticmethod
    def _iter_events(events_field):
        if not events_field:
            return
        if isinstance(events_field, list) and events_field and isinstance(events_field[0], dict):
            for ev in events_field:
                if isinstance(ev, dict):
                    yield ev
        else:
            for group_list in events_field:
                if isinstance(group_list, list):
                    for ev in group_list:
                        if isinstance(ev, dict):
                            yield ev
                elif isinstance(group_list, dict):
                    yield group_list

    @staticmethod
    def _clean_num(v):
        """'3.50' -> '3.5', '3.0' -> '3', but '10' stays '10' (no stripping without a decimal point)."""
        s = str(v)
        return s.rstrip("0").rstrip(".") if "." in s else s

    @staticmethod
    def _param_strings(event):
        """Normalize all possible param fields into a set of strings."""
        clean = MelbetEngine._clean_num
        ep = event.get("eventParams")
        raw = ep.get("params", []) if isinstance(ep, dict) else []
        out = set()
        if isinstance(raw, list):
            for v in raw:
                if isinstance(v, (str, int, float)):
                    out.add(str(v))
                    out.add(clean(v))
                elif isinstance(v, dict):
                    for vv in v.values():
                        if isinstance(vv, (str, int, float)):
                            out.add(str(vv))
                            out.add(clean(vv))
        for k in ("parameter", "param", "line", "handicap"):
            v = event.get(k)
            if isinstance(v, (str, int, float)):
                out.add(str(v))
                out.add(clean(v))
        return out

    @staticmethod
    def _normalize_number_str(s):
        """'3.50' → '3.5', '3.0' → '3', '3' → '3'"""
        try:
            f = float(s)
            if f == int(f):
                return str(int(f))
            return f"{f:.4f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return s

    def _match_param(self, params_set, target):
        if target is None:
            return True
        want = self._normalize_number_str(str(target))
        for p in params_set:
            if self._normalize_number_str(p) == want:
                return True
        return False

    # ---------- fixed conversion: Entity N → Melbet N+1 ----------
    def _over_matches(self, params_set, over_num_entity):
        """
        Entity over: 0-indexed (0, 1, 2, ...)
        Melbet over: 1-indexed (1, 2, 3, ...)
        Entity N → Melbet N+1
        """
        if over_num_entity is None:
            return True

        melbet_over = over_num_entity + 1
        candidates = {
            str(melbet_over),
            f"{melbet_over}.0",
            f"Over {melbet_over}",
            f"over {melbet_over}",
            f"OVER {melbet_over}",
        }
        for p in params_set:
            ps = str(p).strip()
            if ps in candidates:
                return True
            low = ps.lower()
            if low.startswith("over"):
                try:
                    n = int(re.findall(r"\d+", low)[0])
                    if n == melbet_over:
                        return True
                except (IndexError, ValueError):
                    pass
        return False

    # ---------- player name matching ----------
    @staticmethod
    def _norm_name(s):
        s = re.sub(r"[^a-z\s]", " ", (s or "").lower())
        return " ".join(s.split())

    @classmethod
    def name_matches(cls, player_name, subgame_name):
        """
        'Virat Kohli' ↔ 'V. Kohli' ধরনের পার্থক্য সামলায়।
        মিলবে যদি:
          1) পুরো নাম সাবগেমের নামে থাকে, অথবা
          2) পদবির ঠিক আগের শব্দটা হয় নামের প্রথম অক্ষর (যেমন 'V Kohli') অথবা হুবহু প্রথম নাম।
        শুধু পদবি মিললেই চলবে না, যাতে ভুল প্লেয়ারে বেট না বসে।
        """
        p = cls._norm_name(player_name)
        g = cls._norm_name(subgame_name)
        if not p:
            return True
        if p in g:
            return True
        parts = p.split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            toks = g.split()
            for i, t in enumerate(toks):
                if t == last and i > 0:
                    prev = toks[i - 1]
                    if prev == first or (len(prev) == 1 and prev == first[0]):
                        return True
        return False

    # ---------- classification ----------
    @staticmethod
    def _infer_group_kind(subgame_name: str) -> dict:
        n = (subgame_name or "").lower()
        return {
            "is_wicket": any(k in n for k in (
                "wicket", "fall of wicket", "fow", "1st wicket", "total wickets",
            )),
            "is_batsman": any(k in n for k in (
                "batsman", "batter", "player", "individual",
            )),
            "is_over_under": any(k in n for k in (
                "over", "runs", "total", "handicap", "line",
            )),
        }

    # ---------- learning ----------
    def _remember_type(self, bet_type: str, ev_type: int):
        if bet_type == "over":
            self._learned_over_types.add(ev_type)
        elif bet_type == "under":
            self._learned_under_types.add(ev_type)
        elif bet_type == "wicket_under":
            self._learned_wicket_under_types.add(ev_type)

    def _candidate_over_types(self):
        return (self._learned_over_types or self.DEFAULT_OVER_TYPES) | self.DEFAULT_OVER_TYPES

    def _candidate_under_types(self):
        return (self._learned_under_types or self.DEFAULT_UNDER_TYPES) | self.DEFAULT_UNDER_TYPES

    def _candidate_wicket_under_types(self):
        return (self._learned_wicket_under_types or self.DEFAULT_WICKET_UNDER_TYPES) | self.DEFAULT_WICKET_UNDER_TYPES

    # ---------- core finder ----------
    def find_market(self, game_events_data, bet_type, line_value=None,
                    over_num=None, player_name=None, player_id=None,
                    verbose=False):
        """
        bet_type: 'over' | 'under' | 'wicket_under'
        line_value: '3.5' (over/under) or '1.5' (wicket)
        over_num: Entity over (0-indexed) — conversion handled internally
        """
        if not game_events_data or "subGamesForMainGame" not in game_events_data:
            return None

        candidates_types = (
            self._candidate_over_types() if bet_type == "over"
            else self._candidate_wicket_under_types() if bet_type == "wicket_under"
            else self._candidate_under_types()
        )

        debug_rows = []

        for subgame in game_events_data["subGamesForMainGame"]:
            game_id = subgame.get("id")
            subgame_name = subgame.get("subGameName", "") or ""
            kinds = self._infer_group_kind(subgame_name)

            if player_name and not self.name_matches(player_name, subgame_name):
                continue

            if bet_type == "wicket_under" and not kinds["is_wicket"]:
                continue

            if bet_type == "under" and player_name and not kinds["is_batsman"]:
                continue

            for group in subgame.get("eventGroups", []):
                for event in self._iter_events(group.get("events", [])):
                    ev_type = event.get("type")
                    if ev_type is None:
                        continue

                    params_set = self._param_strings(event)

                    if verbose:
                        debug_rows.append({
                            "sg": subgame_name, "type": ev_type,
                            "param": event.get("parameter"),
                            "params": sorted(params_set),
                            "cf": event.get("cf"),
                        })

                    if ev_type not in candidates_types:
                        continue

                    if player_id:
                        # strict: event must carry the same playerId
                        ev_pid = event.get("playerId", 0)
                        try:
                            if not ev_pid or int(ev_pid) != int(player_id):
                                continue
                        except (TypeError, ValueError):
                            continue

                    if bet_type in ("over", "under", "wicket_under"):
                        if line_value is not None and not self._match_param(params_set, line_value):
                            continue

                    if bet_type in ("over", "under") and over_num is not None:
                        if not self._over_matches(params_set, over_num):
                            continue

                    # ✅ MATCH
                    self._remember_type(bet_type, ev_type)
                    return {
                        "game_id": game_id,
                        "type": ev_type,
                        "param": event.get("parameter"),
                        "coef": event.get("cf"),
                        "player_id": event.get("playerId", 0),
                        "params": sorted(params_set),
                        "subgame": subgame_name,
                    }

        if verbose:
            print(f"🔍 [find_market] bet={bet_type} line={line_value} "
                  f"over_entity={over_num} (melbet={None if over_num is None else over_num+1}) "
                  f"types_tried={sorted(candidates_types)}")
            for r in debug_rows[:80]:
                print(f"   sg={r['sg']!r} type={r['type']} param={r['param']} "
                      f"params={r['params']} cf={r['cf']}")

        return None

    def discover_types(self, game_events_data):
        """Dump all unique (subgame, type) → example params."""
        seen = {}
        if not game_events_data or "subGamesForMainGame" not in game_events_data:
            return seen
        for sg in game_events_data["subGamesForMainGame"]:
            name = sg.get("subGameName", "")
            for grp in sg.get("eventGroups", []):
                for ev in self._iter_events(grp.get("events", [])):
                    t = ev.get("type")
                    if t is None:
                        continue
                    key = (name, t)
                    if key not in seen:
                        seen[key] = {
                            "param": ev.get("parameter"),
                            "params": sorted(self._param_strings(ev)),
                            "cf": ev.get("cf"),
                        }
        return seen

    # ---------- place bet ----------
    async def place_bet(self, game_id, bet_type, coef, param, stake=None, player_id=0):
        if stake is None:
            stake = BET_STAKE  # .env-এর BET_STAKE (default 15)
        uid = await self.get_user_id()
        if uid == 0:
            print("❌ User ID পাওয়া যায়নি! লগইন চেক করুন।")
            return False

        api_url = "/service-api/LiveBet/Secure/MakeBetWeb"
        payload = {
            "UserId": uid,
            "Events": [{
                "GameId": game_id, "Type": bet_type, "Coef": coef, "Param": param,
                "PV": None, "PlayerId": player_id, "Kind": 1, "InstrumentId": 0,
                "Seconds": 0, "Price": 0, "Expired": 0, "PlayersDuel": [],
            }],
            "Vid": 0, "partner": self.partner, "Group": self.group,
            "live": True, "CheckCf": 1, "Lng": "en", "notWait": True,
            "IsPowerBet": False, "Summ": stake, "Source": 55, "OneClickBet": 1,
        }

        js_fetch = """
        async (args) => {
            const res = await fetch(args.url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(args.payload) });
            return { status: res.status, data: await res.json() };
        }
        """

        # Retry ONLY the betGUID confirmation step. Any other response is a
        # final failure, so the same bet is never re-sent blindly.
        for _ in range(3):
            try:
                res = await self.page.evaluate(js_fetch, {"url": api_url, "payload": payload})
            except Exception as e:
                print(f"❌ Bet eval failed: {e}")
                return False

            data = res.get('data') if isinstance(res, dict) else None
            data = data if isinstance(data, dict) else {}
            if res.get('status') == 200 and data.get('ErrorCode') == 0:
                value = data.get('Value') if isinstance(data.get('Value'), dict) else {}
                if value.get('Id', 0) > 0:
                    print(f"✅ বেট সফল! ID: {value['Id']} | Stake: {stake}")
                    return True
                if value.get('betGUID'):
                    payload['betGUID'] = value['betGUID']
                    await asyncio.sleep(0.3)
                    continue
            print(f"❌ বেট ফেইল: status={res.get('status')} ErrorCode={data.get('ErrorCode')}")
            return False
        print("❌ বেট কনফার্ম হয়নি।")
        return False
