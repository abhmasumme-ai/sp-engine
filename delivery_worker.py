from event_utils import classify_entity_event


class DeliveryWorker:
    def __init__(self, engine, match_id):
        self.engine = engine
        self.match_id = match_id

    async def on_dot_ball(self, over_num, ball_num):
        """Called by api_server on dot-ball events."""
        return await self.process_ball(over_num, ball_num, "0")

    async def process_ball(self, over, ball, ev, event_kind=None):
        """Full event handler (used by api_server & on_dot_ball).

        NOTE: `ball` এখন শুধু লগে ব্যবহার হয়; মার্কেট খোঁজা হয় ওভার নম্বর দিয়ে।
        """
        print(f"\n🏏 [Delivery Worker] Entity: {over}.{ball} - {ev}")
        game_events = await self.engine.get_latest_odds(self.match_id)
        if not game_events:
            print("⚠️ [Delivery] ওডস পাওয়া যায়নি।")
            return False

        kind = event_kind or classify_entity_event(ev)
        is_wicket = kind == "wicket"
        is_dot = kind == "dot"
        is_four = kind == "four"

        # Logic 1: Dot / Wicket → 0.5 Under on current over
        if is_dot or is_wicket:
            market = self.engine.find_market(
                game_events, bet_type="under", line_value="0.5", over_num=over
            )
            if not market:
                print(f"⚠️ [Delivery] Over {over} 0.5 Under মার্কেট পাওয়া যায়নি।")
                return False
            print(f"🎯 [Delivery] Dot/Wicket! Over {over} 0.5 Under বেট হচ্ছে...")
            ok = await self.engine.place_bet(
                market['game_id'], market['type'], market['coef'], market['param'],
                match_id=self.match_id,
            )
            print(f"{'✅' if ok else '❌'} [Delivery] বেট ফলাফল: {'সফল' if ok else 'ব্যর্থ'}")
            return ok

        # Logic 2: 4 runs → next over 3.5 Over
        if is_four:
            target_over = over + 1
            market = self.engine.find_market(
                game_events, bet_type="over", line_value="3.5", over_num=target_over
            )
            if not market:
                print(f"⚠️ [Delivery] Over {target_over} 3.5 Over মার্কেট পাওয়া যায়নি।")
                return False
            print(f"🎯 [Delivery] 4 Runs! Over {target_over} 3.5 Over বেট হচ্ছে...")
            ok = await self.engine.place_bet(
                market['game_id'], market['type'], market['coef'], market['param'],
                match_id=self.match_id,
            )
            print(f"{'✅' if ok else '❌'} [Delivery] বেট ফলাফল: {'সফল' if ok else 'ব্যর্থ'}")
            return ok

        return False