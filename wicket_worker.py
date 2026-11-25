
class WicketWorker:
    def __init__(self, engine, match_id):
        self.engine = engine
        self.match_id = match_id
        self._dumped = False

    async def on_wicket(self, line_value="1.5"):
        return await self.process_wicket(line_value=line_value)

    async def process_wicket(self, line_value="1.5"):
        print(f"\n🏏 [Wicket Worker] Fall in Wicket! (line={line_value})")

        game_events = await self.engine.get_latest_odds(self.match_id)
        if not game_events:
            print("⚠️ [Wicket] ওডস পাওয়া যায়নি।")
            return

        candidates = [line_value]
        try:
            v = float(line_value)
            for delta in (-0.5, 0.5, 1.0, -1.0):
                nv = v + delta
                if nv > 0:
                    candidates.append(f"{nv:g}")
        except (TypeError, ValueError):
            pass

        for line in candidates:
            m = self.engine.find_market(
                game_events, bet_type="wicket_under", line_value=line,
            )
            if m:
                print(f"🎯 [Wicket] Fall in Wicket Under {line} | "
                      f"sg={m['subgame']} coef={m['coef']}")
                await self.engine.place_bet(
                    m["game_id"], m["type"], m["coef"], m["param"],
                )
                return

        if not self._dumped:
            self._dumped = True
            print("🔍 [Wicket] Market miss — dumping:")
            self.engine.find_market(
                game_events, bet_type="wicket_under",
                line_value=line_value, verbose=True,
            )
        print(f"⚠️ [Wicket] Fall in Wicket Under {line_value} market নেই")