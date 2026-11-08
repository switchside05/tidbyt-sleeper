# Sleeper matchup on Tidbyt

Shows my live Sleeper fantasy matchup (league 1313182350917537792) on my Tidbyt.

- **Screen 1:** both teams' live scores (leader in green) and projected totals.
- **Next screens:** my starters, two per screen: name, position, NFL team, game status, points.
  Yellow = playing now, white = final, gray = not started (kickoff time shown), red OUT = inactive.

Runs automatically on GitHub every ~5 minutes during NFL game windows (see `.github/workflows/update-tidbyt.yml`).
Change the team nickname or screen timing in `config.json`.

Projected totals use Sleeper's own projections with this league's scoring:
finished players count actual points, unstarted players count their projection,
and live players count actual points plus their projection for the time left in the game.
