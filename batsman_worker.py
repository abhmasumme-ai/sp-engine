
class BatsmanWorker:
    def __init__(self, engine, match_id):
        self.engine = engine
        self.match_id = match_id
        self._dumped = False

    async def on_batsman_runs(self, striker_id=None, striker_name=None, line_value="31.5"):
        return await self.process_wicket(
            striker_name=striker_name, striker_id=striker_id, line_value=line_value,
        )

    async def process_wicket(self, striker_name=None, striker_id=None, line_value="31.5"):
        print(f"\n🏏 [Batsman Worker] Striker: {striker_name} (id={striker_id}) "
              f"| target Under {line_value}")

        game_events = await self.engine.get_latest_odds(self.match_id)
        if not game_events:
            print("⚠️ [Batsman] ওডস পাওয়া যায়নি।")
            return

        # requested line then common alternatives
        candidates = [line_value]
        try:
            v = float(line_value)
            for delta in (-5.5, -10.5, 5.5, 10.5):
                nv = v + delta
                if nv > 0:
                    candidates.append(f"{nv:g}")
        except (TypeError, ValueError):
            pass

        for line in candidates:
            m = self.engine.find_market(
                game_events, bet_type="under", line_value=line,
                player_name=striker_name,
                player_id=None if striker_name else striker_id,
            )
            if m:
                label = striker_name or f"pid={striker_id}"
                print(f"🎯 [Batsman] {label} → Under {line} | "
                      f"sg={m['subgame']} coef={m['coef']}")
                await self.engine.place_bet(
                    m["game_id"], m["type"], m["coef"], m["param"],
                    player_id=m.get("player_id", 0),
                )
                return

        if not self._dumped:
            self._dumped = True
            print("🔍 [Batsman] Market miss — dumping:")
            self.engine.find_market(
                game_events, bet_type="under", line_value=line_value,
                player_name=striker_name,
                player_id=None if striker_name else striker_id,
                verbose=True,
            )
        print(f"⚠️ [Batsman] {striker_name or striker_id} Under {line_value} market নেই")