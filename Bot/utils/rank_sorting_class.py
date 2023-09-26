import functools


@functools.total_ordering
class Ranker:
    LEAGUES = {
        "Iron": 0,
        "Bronze": 1,
        "Silver": 2,
        "Gold": 3,
        "Platinum": 4,
        "Emerald": 5,
        "Diamond": 6,
        "Master": 7,
    }
    SUBLEAGUE = {
        "IV": 0,
        "III": 1,
        "II": 2,
        "I": 3,
    }  # reverse the value

    def __init__(self, league: str, subleague: str, leaguePoints: str):
        self.league = league.title()
        self.subleague = subleague
        self.leaguePoints = leaguePoints
        self._score = self._rankToScore()

    def _rankToScore(self):

        actual_score = 1000 * Ranker.LEAGUES[self.league] + 100 * Ranker.SUBLEAGUE[self.subleague] + int(self.leaguePoints)
        adjustment_factor = Ranker.LEAGUES[self.league] * 599
        return actual_score - adjustment_factor

    def __repr__(self):
        # prints "GoldIV - 6LP
        if self.league == "Master":
            return f"{self.league} - {self.leaguePoints}LP"
        else:
            return f"{self.league} {self.subleague} - {self.leaguePoints}LP"

    def _is_valid_operand(self, other):
        return hasattr(other, "_score")

    def __lt__(self, other):
        if not self._is_valid_operand(other):
            return NotImplemented
        return self._score < other._score

    def __eq__(self, other):
        if not self._is_valid_operand(other):
            return NotImplemented
        return self._score == other._score