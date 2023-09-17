import functools


@functools.total_ordering
class Ranker:
    LEAGUES = {
        "Master": 6,
        "Diamond": 5,
        "Platinum": 4,
        "Gold": 3,
        "Silver": 2,
        "Bronze": 1,
        "Iron": 0
    }
    SUBLEAGUE = {
        "I": 5,
        "II": 4,
        "III": 3,
        "IV": 2,
        "V": 1
    }  # reverse the value

    def __init__(self, league, subleague, leaguePoints):
        self.league = league.title()
        self.subleague = subleague
        self.leaguePoints = leaguePoints
        self._score = self._rankToScore()

    def _rankToScore(self):
        return 1000 * Ranker.LEAGUES[self.league] + 100 * \
            Ranker.SUBLEAGUE[self.subleague] + int(self.leaguePoints)

    def __repr__(self):
        # prints "GoldIV - 6LP
        if self.league == "Master":
            return f"{self.league} - {self.leaguePoints}LP"
        else:
            return f"{self.league}{self.subleague} - {self.leaguePoints}LP"

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