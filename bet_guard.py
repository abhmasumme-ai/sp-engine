"""Bet safety guard — সব বেট place_bet-এর আগে এখান দিয়ে যায়।

- kill-switch: ON থাকলে কোনো বেট যাবে না
- ম্যাচ প্রতি সর্বোচ্চ বেট সংখ্যা ও মোট স্টেক (0 = সীমা নেই)
- ন্যূনতম odds (null / অবৈধ / খুব কম odds-এ বেট নয়)

asyncio একটাই থ্রেডে চলে, আর reserve() কোনো await ছাড়াই কোটা কেটে নেয়,
তাই একসাথে অনেক বেট এলেও সীমা পার হয় না।
"""
from config import MAX_BETS_PER_MATCH, MAX_STAKE_PER_MATCH, MIN_ODDS


class BetGuard:
    def __init__(self, max_bets, max_stake, min_odds):
        self.max_bets = max_bets
        self.max_stake = max_stake
        self.min_odds = min_odds
        self.killed = False
        self._bets = {}
        self._staked = {}

    @staticmethod
    def _key(match_id):
        return str(match_id) if match_id else "global"

    def reserve(self, match_id, stake, coef):
        """বেটের আগে কোটা কাটে। (ok, কারণ) ফেরত দেয়।"""
        if self.killed:
            return False, "kill-switch চালু আছে"
        try:
            cf = float(coef)
        except (TypeError, ValueError):
            return False, f"odds অবৈধ ({coef!r})"
        if not cf >= self.min_odds:  # NaN-ও এখানে আটকায়
            return False, f"odds {coef} সর্বনিম্ন {self.min_odds}-এর কম"

        k = self._key(match_id)
        n = self._bets.get(k, 0)
        s = self._staked.get(k, 0)
        if self.max_bets and n >= self.max_bets:
            return False, f"ম্যাচ প্রতি সর্বোচ্চ বেট ({self.max_bets}) পূর্ণ"
        if self.max_stake and s + stake > self.max_stake:
            return False, f"ম্যাচ প্রতি সর্বোচ্চ স্টেক ({self.max_stake}) ছাড়িয়ে যাবে"

        self._bets[k] = n + 1
        self._staked[k] = s + stake
        return True, ""

    def release(self, match_id, stake):
        """নিশ্চিত ব্যর্থ বেটের কোটা ফেরত দেয়।"""
        k = self._key(match_id)
        self._bets[k] = max(0, self._bets.get(k, 0) - 1)
        self._staked[k] = max(0, self._staked.get(k, 0) - stake)

    def kill(self):
        self.killed = True

    def resume(self):
        self.killed = False

    def status(self):
        return {
            "killed": self.killed,
            "max_bets": self.max_bets,
            "max_stake": self.max_stake,
            "min_odds": self.min_odds,
            "bets": dict(self._bets),
            "staked": dict(self._staked),
        }


guard = BetGuard(MAX_BETS_PER_MATCH, MAX_STAKE_PER_MATCH, MIN_ODDS)