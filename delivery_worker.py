
import re


class DeliveryWorker:
    def __init__(self, engine, match_id):
        self.engine = engine
        self.match_id = match_id
        self._dumped = False

    async def on_dot_ball(self, over_num, ball_num):
        await self.process_ball(over_num, ball_num, "0")

    async def process_ball(self, over, ball, ev, event_kind=None):
        # ⚠️ over = Entity over (0-indexed). Melbet over = over + 1
        melbet_over = None if over is None else over + 1
        print(f"\n🏏 [Delivery Worker] Entity: {over}.{ball} - {ev} "
              f"| Melbet over ≈ {melbet_over}")

        game_events = await self.engine.get_latest_odds(self.match_id)
        if not game_events:
            print("⚠️ [Delivery] ওডস পাওয়া যায়নি।")
            return

        ev_str = str(ev).strip().lower()
        is_wicket = event_kind == "wicket" or bool(re.match(
            r"^(wicket|out|w)(?:[.!]|\s|$)|^(bowled|lbw|caught|c\s*&\s*b|"
            r"run\s*out|stumped?|hit\s*wicket|retired|handled|obstruct)", ev_str,
        ))
        is_dot = event_kind == "dot" or ev_str in ("0", "dot", "no run", "norun")
        is_four = event_kind == "four" or bool(re.match(r"^(?:4|four)(?:\b|$)", ev_str))

        # ---------- Strategy 1: dot / wicket (ball 1-3) → current over 0.5 Under ----------
        if (is_dot or is_wicket) and ball is not None and ball <= 3:
            print(f"   [debug] dot/wicket → search Entity over {over} "
                  f"(Melbet {over+1}) 0.5 Under")
            market = self._find_under(game_events, line="0.5", over_entity=over)
            if market:
                print(f"🎯 [Delivery] Dot/Wicket @ {over}.{ball} → "
                      f"Entity over {over} (Melbet {over+1}) 0.5 Under | "
                      f"sg={market['subgame']} coef={market['coef']}")
                await self.engine.place_bet(
                    market["game_id"], market["type"],
                    market["coef"], market["param"],
                )
                return
            print(f"⚠️ [Delivery] Entity over {over} (Melbet {over+1}) "
                  f"0.5 Under market নেই")

        # ---------- Strategy 2: 4 runs (ball 1-4) → next over 3.5 Over ----------
        elif is_four and ball is not None and ball <= 4:
            next_over_entity = over + 1
            next_melbet = next_over_entity + 1
            print(f"   [debug] four → search Entity over {next_over_entity} "
                  f"(Melbet {next_melbet}) Over 3.5/4.5/2.5/5.5")
            for line in ("3.5", "4.5", "2.5", "5.5"):
                market = self._find_over(game_events, line=line, over_entity=next_over_entity)
                if market:
                    print(f"🎯 [Delivery] 4 @ {over}.{ball} → "
                          f"Entity over {next_over_entity} "
                          f"(Melbet {next_melbet}) {line} Over | "
                          f"sg={market['subgame']} coef={market['coef']}")
                    await self.engine.place_bet(
                        market["game_id"], market["type"],
                        market["coef"], market["param"],
                    )
                    return
            print(f"⚠️ [Delivery] Entity over {next_over_entity} "
                  f"(Melbet {next_melbet}) Over market নেই")

    def _find_under(self, game_events, line, over_entity):
        m = self.engine.find_market(
            game_events, bet_type="under", line_value=line, over_num=over_entity,
        )
        if m:
            return m
        if not self._dumped:
            self._dumped = True
            print("🔍 [Delivery] Under miss — dumping all markets:")
            self.engine.find_market(
                game_events, bet_type="under",
                line_value=line, over_num=over_entity, verbose=True,
            )
        return None

    def _find_over(self, game_events, line, over_entity):
        # 1) requested next over
        m = self.engine.find_market(
            game_events, bet_type="over", line_value=line, over_num=over_entity,
        )
        if m:
            return m
        # 2) fallback: current over (কিছু Melbet-এ next over market late আসে)
        m = self.engine.find_market(
            game_events, bet_type="over", line_value=line,
            over_num=max(0, over_entity - 1),
        )
        if m:
            print(f"   ⚠️ fallback: next over market নেই, current over-এর market use")
            return m
        # 3) dump once
        if not self._dumped:
            self._dumped = True
            print("🔍 [Delivery] Over miss — dumping all markets:")
            self.engine.find_market(
                game_events, bet_type="over",
                line_value=line, over_num=over_entity, verbose=True,
            )
        return None