import argparse
import base64
from datetime import datetime
from html import escape
import json
from pathlib import Path
import sys
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.parse import unquote
from urllib.request import Request, urlopen


PARTICIPANT_COLORS = ["#ff5454", "#64d8ff", "#ffd66b", "#b5a2ff", "#62deb0", "#f5a1ce", "#ffffff", "#a6b26d"]
COUNTRY_CODES = {
	"Australia": "AU", "China": "CN", "Japan": "JP", "United States": "US", "Canada": "CA",
	"Monaco": "MC", "Spain": "ES", "Austria": "AT", "United Kingdom": "GB", "Belgium": "BE",
	"Hungary": "HU", "Netherlands": "NL", "Italy": "IT", "Azerbaijan": "AZ", "Bahrain": "BH",
	"Singapore": "SG", "Mexico": "MX", "Brazil": "BR", "Qatar": "QA", "United Arab Emirates": "AE",
}


def get_members(league_id: int, gameday_id: int = 0, phase_id: int = 1) -> list[dict]:
	leaderboard_type = 2 if gameday_id else 1
	url = (
		"https://fantasy.formula1.com/feeds/leaderboard/privateleague/"
		f"list_{leaderboard_type}_{league_id}_{gameday_id}_{phase_id}.json"
	)
	request = Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
	with urlopen(request, timeout=30) as response:
		payload = json.load(response)
	members = payload["Value"]["leaderboard"]
	if not isinstance(members, list):
		raise ValueError("API did not return a leaderboard. Check the league ID.")
	return members


def member_key(member: dict) -> tuple[str, int]:
	return str(member.get("social_id", member["user_name"])), int(member.get("team_no", 0))


def get_schedule_fixtures() -> list[dict]:
	request = Request(
		"https://fantasy.formula1.com/feeds/v2/schedule/raceday_en.json",
		headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
	)
	with urlopen(request, timeout=30) as response:
		return json.load(response)["Data"]["fixtures"]


def get_weekend_schedule(fixtures: list[dict]) -> list[dict]:
	race_fixtures = sorted(
		(fixture for fixture in fixtures if fixture["SessionType"] == "Race"),
		key=lambda fixture: int(fixture["GamedayId"]),
	)
	current_index = next(
		(index for index, fixture in enumerate(race_fixtures) if int(fixture.get("GDIsCurrent", 0)) == 1),
		next((index for index, fixture in enumerate(race_fixtures) if str(fixture.get("MatchStatus", "0")) != "4"), len(race_fixtures) - 1),
	)
	weekends = []
	for offset, label in ((-1, "LAST GP"), (0, "THIS WEEKEND"), (1, "NEXT GP")):
		index = current_index + offset
		if not 0 <= index < len(race_fixtures):
			continue
		race = race_fixtures[index]
		meeting_fixtures = [fixture for fixture in fixtures if fixture["MeetingId"] == race["MeetingId"]]
		dates = []
		for fixture in meeting_fixtures:
			for key in ("SessionStartDateISO8601", "SessionEndDateISO8601"):
				value = fixture.get(key)
				if value:
					dates.append(datetime.fromisoformat(value).date())
				elif fixture.get("SessionStartDate"):
					dates.append(datetime.strptime(fixture["SessionStartDate"], "%m/%d/%Y %H:%M:%S").date())
		if not dates:
			continue
		start, end = min(dates), max(dates)
		start_label = start.strftime("%b %d").replace(" 0", " ")
		end_label = end.strftime("%b %d").replace(" 0", " ")
		date_label = start_label if start == end else f"{start_label} - {end_label}"
		weekends.append({"label": label, "name": race["MeetingName"], "country": race.get("CountryName", ""),
			"dates": date_label, "current": offset == 0})
	return weekends


def get_history(league_id: int, fixtures: list[dict] | None = None) -> list[dict]:
	fixtures = get_schedule_fixtures() if fixtures is None else fixtures
	rounds = {}
	for fixture in fixtures:
		if fixture["SessionType"] != "Race" or str(fixture["MatchStatus"]) != "4":
			continue
		gameday_id = int(fixture["GamedayId"])
		rounds[gameday_id] = {
			"id": gameday_id,
			"phase": int(fixture["PhaseId"]),
			"name": fixture["MeetingName"],
			"label": fixture["CircuitLocation"],
		}
	history = []
	for round_id in sorted(rounds):
		round_data = rounds[round_id]
		try:
			members = get_members(league_id, round_id, round_data["phase"])
		except HTTPError as error:
			if error.code not in (403, 404):
				raise
			members = None
			print(f"Historical results unavailable: {round_data['name']} (HTTP {error.code})", file=sys.stderr)
		round_data["available"] = members is not None
		round_data["ranks"] = {} if members is None else {
			member_key(member): int(member["cur_rank"]) for member in members
		}
		round_data["points"] = {} if members is None else {
			member_key(member): float(member["cur_points"]) for member in members
		}
		history.append(round_data)
	return history


def render_weekend_schedule(weekends: list[dict]) -> str:
	if not weekends:
		return ""
	cards = []
	for weekend in weekends:
		modifier = "weekend-current" if weekend["current"] else "weekend-adjacent"
		checkered_flag = '<span class="weekend-flag" aria-hidden="true"></span>' if weekend["label"] == "LAST GP" else ""
		country = weekend.get("country", "")
		country_code = COUNTRY_CODES.get(country)
		country_flag = ""
		if country_code:
			country_flag = f'<img class="country-flag" src="https://flagcdn.com/w40/{country_code.lower()}.png" alt="{escape(country)} flag" width="24" height="16">'
		cards.append(
			f'<article class="weekend-card {modifier}"><p class="weekend-label">{weekend["label"]}{checkered_flag}</p>'
			f'<h2>{country_flag}<span>{escape(weekend["name"])}</span></h2>'
			f'<p class="weekend-dates">{escape(weekend["dates"])}</p></article>'
		)
	return f'<section class="weekend-schedule" aria-label="Grand Prix weekends">{"".join(cards)}</section>'


def build_chart_data(standings: list[dict], history: list[dict]) -> dict:
	series = [{"name": standing["name"], "code": standing["code"],
		"race": {metric: [] for metric in ("points", "interval", "gap")},
		"total": {metric: [] for metric in ("points", "interval", "gap")}}
		for standing in standings]
	totals = {}
	complete = True
	for round_data in history:
		points = round_data.get("points", {})
		complete = complete and round_data["available"]
		for key, value in points.items():
			totals[key] = totals.get(key, 0) + value
		for mode, values in (("race", points), ("total", totals if complete else {})):
			ordered = sorted(values, key=lambda key: values[key], reverse=True)
			metrics = {}
			for index, key in enumerate(ordered):
				metrics[key] = {"points": values[key],
					"interval": 0 if index == 0 else values[ordered[index - 1]] - values[key],
					"gap": values[ordered[0]] - values[key]}
			for standing, entry in zip(standings, series):
				key = standing["key"]
				for metric in ("points", "interval", "gap"):
					value = metrics.get(key, {}).get(metric)
					if key not in points:
						value = None
					entry[mode][metric].append(value)
	return {"labels": [f"R{round_data['id']:02} {round_data['label']}" for round_data in history],
		"series": series}


def render_latest_race_table(standings: list[dict], history: list[dict], chart_data: dict) -> str:
	latest_index = next((index for index in range(len(history) - 1, -1, -1) if history[index]["available"]), None)
	if latest_index is None:
		return '<section class="race-section" aria-labelledby="race-title"><h2 id="race-title">LATEST GRAND PRIX</h2><p class="empty">No Grand Prix results available</p></section>'
	if not standings:
		return '<section class="race-section" aria-labelledby="race-title"><h2 id="race-title">LATEST GRAND PRIX</h2><p class="empty">No participants</p></section>'
	round_data = history[latest_index]
	points = round_data["points"]
	highest_points = max(points.values(), default=None)
	has_team_names = any(standing.get("team_name") for standing in standings)
	series_by_key = {standing["key"]: entry for standing, entry in zip(standings, chart_data["series"])}
	participant_colors = {
		standing["key"]: PARTICIPANT_COLORS[index % len(PARTICIPANT_COLORS)]
		for index, standing in enumerate(standings)
	}
	ordered = sorted(standings, key=lambda standing: points.get(standing["key"], float("-inf")), reverse=True)
	rows = []
	for position, standing in enumerate(ordered, start=1):
		key = standing["key"]
		score = points.get(key)
		gaps = series_by_key[key]["total"]["gap"]
		current_gap = gaps[latest_index]
		previous_gap = gaps[latest_index - 1] if latest_index > 0 else None
		delta = current_gap - previous_gap if current_gap is not None and previous_gap is not None else None
		score_class = "race-points"
		if score is not None and score == highest_points:
			score_class += " race-best"
		elif delta is not None and delta <= 0:
			score_class += " race-improving"
		elif delta is not None and delta > 0:
			score_class += " race-worsening"
		team_cell = f'<td class="race-team">{escape(standing["team_name"])}</td>' if has_team_names and standing.get("team_name") else ('<td class="race-team missing">&mdash;</td>' if has_team_names else "")
		score_text = f"{score:g}" if score is not None else "&mdash;"
		delta_text = (f"+{delta:g}" if delta > 0 else f"{delta:g}") if delta is not None else "&mdash;"
		rows.append(
			f'<tr><td class="position">{position:02}</td>'
			f'<th scope="row" title="{escape(standing["name"], quote=True)}">'
			f'<span class="driver-name" style="--participant-color: {participant_colors[key]}">'
			f'{escape(standing["code"])}</span></th>{team_cell}'
			f'<td class="{score_class}">{score_text}</td><td class="race-delta">{delta_text}</td></tr>'
		)
	team_header = '<th scope="col">TEAM</th>' if has_team_names else ""
	team_column = '<col class="race-team-column">' if has_team_names else ""
	round_label = f"R{round_data['id']:02} {round_data['label']}"
	return (
		'<section class="race-section" aria-labelledby="race-title">'
		'<h2 id="race-title">LATEST GRAND PRIX</h2>'
		f'<p class="race-round">{escape(round_label)}</p>'
		'<table class="race-table" aria-label="Points earned at the latest Grand Prix">'
		f'<colgroup><col class="rank"><col class="driver">{team_column}<col class="score"><col class="delta"></colgroup>'
		f'<thead><tr><th scope="col">POS</th><th scope="col">DRIVER</th>{team_header}<th scope="col">POINTS</th><th scope="col">DELTA</th></tr></thead>'
		f'<tbody>{"".join(rows)}</tbody></table></section>'
	)


def get_player_prices() -> dict[str, float]:
	headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
	request = Request("https://fantasy.formula1.com/feeds/v2/apps/web_config.json", headers=headers)
	with urlopen(request, timeout=30) as response:
		gameday_id = int(json.load(response)["Data"]["constraints"]["GamedayId"])
	request = Request(f"https://fantasy.formula1.com/feeds/drivers/{gameday_id}_en.json", headers=headers)
	with urlopen(request, timeout=30) as response:
		players = json.load(response)["Data"]["Value"]
	return {str(player["PlayerId"]): float(player["Value"]) for player in players}


def build_standings(members: list[dict], prices: dict[str, float] | None = None) -> list[dict]:
	ordered = sorted(members, key=lambda member: float(member["cur_points"]), reverse=True)
	standings = []
	if not ordered:
		return standings
	leader_points = float(ordered[0]["cur_points"])
	previous_points = leader_points
	for position, member in enumerate(ordered, start=1):
		name = member["user_name"].strip()
		points = float(member["cur_points"])
		team = member.get("user_team", [])
		team_value = None
		if prices is not None and len(team) == 7 and all(str(player_id) in prices for player_id in team):
			team_value = sum(prices[str(player_id)] for player_id in team)
		standings.append({
			"key": member_key(member),
			"position": position,
			"name": name,
			"code": name.split()[-1][:3].upper() if name else "---",
			"team_name": unquote(str(member.get("team_name", ""))).strip(),
			"points": points,
			"team_value": team_value,
			"interval": previous_points - points,
			"gap": leader_points - points,
		})
		previous_points = points
	return standings


def render_standings(league_name: str, standings: list[dict], history: list[dict] | None = None,
		weekends: list[dict] | None = None) -> str:
	history = history or []
	chart_values = build_chart_data(standings, history)
	chart_data = json.dumps(chart_values, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
	race_section = render_latest_race_table(standings, history, chart_values)
	weekend_section = render_weekend_schedule(weekends or [])
	poster_asset = Path(__file__).with_name("current-standings.B8mHwqMi.png")
	poster_background = "data:image/png;base64," + base64.b64encode(poster_asset.read_bytes()).decode("ascii") if poster_asset.exists() else None
	poster_data = json.dumps({"background": poster_background, "league": league_name,
		"entries": [{"code": standing["code"], "name": standing["name"], "points": standing["points"]}
			for standing in standings]}, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
	rows = []
	for index, standing in enumerate(standings):
		color = PARTICIPANT_COLORS[index % len(PARTICIPANT_COLORS)]
		position = standing["position"]
		interval = "&mdash;" if position == 1 else f"+{standing['interval']:g}"
		gap = "&mdash;" if position == 1 else f"+{standing['gap']:g}"
		team_value = standing.get("team_value")
		value_text = "&mdash;" if team_value is None else f"${team_value:.1f}M"
		rows.append(
			f'<tr class="{"leader" if position == 1 else ""}">'
			f'<td class="position">{position:02}</td>'
			f'<th scope="row" title="{escape(standing["name"], quote=True)}">'
			f'<span class="driver-name" style="--participant-color: {color}">'
			f'<abbr title="{escape(standing["name"], quote=True)}">{escape(standing["code"])}</abbr></span></th>'
			f'<td class="points">{standing["points"]:g}</td>'
			f'<td class="team-value">{value_text}</td>'
			f'<td class="interval">{interval}</td><td class="gap">{gap}</td></tr>'
		)
	if not rows:
		rows.append('<tr><td colspan="6" class="empty">No participants</td></tr>')
	history_headers = []
	for round_data in history:
		title = round_data["name"] + ("" if round_data["available"] else " - Results unavailable")
		history_headers.append(
			f'<th scope="col" title="{escape(title, quote=True)}">'
			f'<span class="round-number">R{round_data["id"]:02}</span>{escape(round_data["label"])}</th>'
		)
	history_rows = []
	for index, standing in enumerate(standings):
		color = PARTICIPANT_COLORS[index % len(PARTICIPANT_COLORS)]
		cells = []
		for round_data in history:
			rank = round_data["ranks"].get(standing["key"])
			if rank is None:
				title = "No result for this team" if round_data["available"] else "Results unavailable"
				cells.append(f'<td class="missing" title="{title}">&mdash;</td>')
			else:
				podium_class = {1: "gold", 2: "silver", 3: "bronze"}.get(rank, "")
				cells.append(f'<td class="{podium_class}">{rank}</td>')
		history_rows.append(
			f'<tr><th scope="row" title="{escape(standing["name"], quote=True)}">'
			f'<span class="driver-name" style="--participant-color: {color}">'
			f'{escape(standing["code"])}</span></th>{"".join(cells)}</tr>'
		)
	if not standings:
		history_rows.append(f'<tr><td colspan="{len(history) + 1}" class="empty">No participants</td></tr>')
	history_section = (
		'<section class="history-section" aria-labelledby="history-title">'
		'<h2 id="history-title">GRAND PRIX POSITIONS</h2>'
		'<div class="history-scroll" tabindex="0" role="region" aria-label="Grand Prix positions">'
		f'<table class="history" style="min-width: {80 * (len(history) + 1)}px" aria-label="Positions by points earned at each Grand Prix">'
		f'<thead><tr><th scope="col">DRIVER</th>{"".join(history_headers)}</tr></thead>'
		f'<tbody>{"".join(history_rows)}</tbody></table></div></section>'
	) if history else '<section class="history-section"><h2>GRAND PRIX POSITIONS</h2><p class="empty">No completed Grand Prix</p></section>'
	return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>F1 Fantasy | League standings</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600;700&family=Barlow+Condensed:wght@600;700;800&display=swap" rel="stylesheet">
<style>
:root { --red: #ff302b; --bg: #101011; --text: #f5f5f5; --muted: #a4a4aa; --line: #333337; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font-family: 'Barlow', sans-serif; letter-spacing: 0; }
body::before { content: ''; display: block; height: 6px; background: var(--red); }
main { max-width: 780px; padding: 52px 32px; margin: 0 auto; }
header { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-bottom: 28px; }
.eyebrow { margin: 0 0 8px; color: var(--red); font-size: 12px; font-weight: 700; }
h1 { margin: 0; font: italic 800 36px/1 'Barlow Condensed', sans-serif; }
.flag { width: 48px; height: 32px; flex-shrink: 0; background: linear-gradient(to bottom, #0057b7 0 50%, #ffd700 50% 100%); box-shadow: 0 0 0 1px #ffffff30; transform: skew(-10deg); }
.header-actions { display: flex; align-items: center; gap: 14px; }
.form-trigger { width: 48px; height: 48px; padding: 0; overflow: hidden; border: 1px solid var(--line); border-radius: 4px; background: #fff; cursor: pointer; }
.form-trigger img { display: block; width: 100%; height: 100%; object-fit: cover; }
.form-trigger:focus-visible, .form-close:focus-visible { outline: 2px solid var(--red); outline-offset: 3px; }
.form-dialog { width: min(640px, calc(100% - 24px)); max-width: none; max-height: calc(100% - 24px); padding: 12px; border: 1px solid var(--line); border-radius: 4px; background: var(--bg); color: var(--text); }
.form-dialog::backdrop { background: #000c; }
.form-dialog-head { display: flex; justify-content: flex-end; margin-bottom: 8px; }
.form-close { width: 36px; height: 36px; border: 1px solid var(--line); border-radius: 4px; background: #242426; color: var(--text); font: 600 22px/1 'Barlow', sans-serif; cursor: pointer; }
.form-dialog iframe { display: block; width: 100%; height: min(418px, calc(100dvh - 88px)); border: 0; background: #fff; }
.meta { display: flex; justify-content: space-between; gap: 12px; padding: 12px 0; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; font-weight: 600; }
.weekend-schedule { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.3fr) minmax(0, 1fr); align-items: stretch; gap: 10px; margin: 0 0 20px; }
.weekend-card { position: relative; min-width: 0; padding: 12px 14px; border: 1px solid var(--line); overflow: hidden; }
.weekend-card h2 { display: flex; align-items: center; gap: 8px; margin: 8px 0 4px; font: 600 17px/1.1 'Barlow Condensed', sans-serif; }
.weekend-card h2 > span:last-child { min-width: 0; }
.country-flag { display: block; width: 24px; height: 16px; flex: 0 0 auto; object-fit: cover; border-radius: 2px; }
.weekend-label { display: flex; align-items: center; gap: 8px; margin: 0; color: var(--muted); font-size: 10px; font-weight: 700; }
.weekend-dates { margin: 0; color: var(--muted); font-size: 12px; font-weight: 500; }
.weekend-adjacent { background: #171719; }
.weekend-adjacent h2, .weekend-adjacent .weekend-dates { color: #77777e; }
.weekend-flag { width: 18px; height: 12px; flex: 0 0 auto; background: conic-gradient(#bdbdc2 25%, #242426 0 50%, #bdbdc2 0 75%, #242426 0) 0 0 / 6px 6px; transform: skew(-12deg); }
.weekend-current { padding: 16px 18px; border-color: var(--red); background: #242426; }
.weekend-current .weekend-label { color: var(--red); }
.weekend-current h2 { font-size: 22px; }
.weekend-current .weekend-dates { color: var(--text); font-weight: 600; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; font-variant-numeric: tabular-nums; }
col.rank { width: 8%; } col.driver { width: 14%; } col.score { width: 18%; } col.value { width: 22%; } col.delta { width: 19%; }
thead th { padding: 14px 12px; color: var(--muted); font-size: 11px; font-weight: 600; text-align: right; border-bottom: 2px solid var(--red); }
thead th:first-child, thead th:nth-child(2) { text-align: left; }
tbody tr { border-bottom: 1px solid var(--line); animation: arrive .35s ease-out both; }
tbody tr:nth-child(2) { animation-delay: .04s; } tbody tr:nth-child(3) { animation-delay: .08s; }
tbody tr:nth-child(4) { animation-delay: .12s; } tbody tr:nth-child(5) { animation-delay: .16s; }
tbody tr:nth-child(6) { animation-delay: .2s; }
tbody td, tbody th { height: 72px; padding: 12px; text-align: right; overflow-wrap: anywhere; }
tbody th { text-align: left; font: 700 30px/1 'Barlow Condensed', sans-serif; }
abbr { text-decoration: none; cursor: default; }
.driver-name { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
.driver-name::before { content: ''; width: 3px; height: 1em; flex-shrink: 0; background: var(--participant-color); }
.position { text-align: left; color: var(--muted); font: 600 20px 'Barlow Condensed', sans-serif; }
.points { font: 700 28px 'Barlow Condensed', sans-serif; }
.interval, .gap { font-size: 18px; font-weight: 500; }
.interval { color: var(--muted); }
.team-value { font: 600 20px 'Barlow Condensed', sans-serif; color: #c8d5df; white-space: nowrap; }
.leader { background: #242426; }
.leader th { color: var(--red); }
.leader .position { box-shadow: inset 3px 0 var(--red); color: var(--text); }
.empty { text-align: center; color: var(--muted); }
.history-section { margin-top: 44px; }
h2 { margin: 0 0 20px; font: italic 700 24px/1.2 'Barlow Condensed', sans-serif; }
.history-scroll { overflow-x: auto; scrollbar-color: var(--red) #242426; }
.history-scroll:focus-visible { outline: 2px solid var(--red); outline-offset: 4px; }
.history { border-collapse: separate; border-spacing: 0; }
.history th, .history td { width: 80px; text-align: center; padding: 10px 6px; border-bottom: 1px solid var(--line); }
.history thead th { vertical-align: top; height: 68px; border-bottom: 2px solid var(--red); }
.history thead th:nth-child(2) { text-align: center; }
.history th:first-child { width: 80px; position: sticky; left: 0; z-index: 1; background: var(--bg); text-align: left; box-shadow: 1px 0 var(--line); }
.history tbody th { font-size: 25px; }
.history tbody td { font: 600 24px 'Barlow Condensed', sans-serif; }
.history .gold { color: #ffd66b; background: #332a16; }
.history .silver { color: #e0e4eb; background: #2a2d33; }
.history .bronze { color: #e5a575; background: #332319; }
.history .missing { color: var(--muted); }
.round-number { display: block; margin-bottom: 6px; color: var(--red); font-size: 10px; }
.race-section { margin-top: 44px; }
.race-round { margin: -12px 0 14px; color: var(--muted); font-size: 13px; }
.race-table col.rank { width: 8%; } .race-table col.driver { width: 18%; } .race-table col.race-team-column { width: 34%; } .race-table col.score { width: 20%; } .race-table col.delta { width: 20%; }
.race-table thead th:nth-child(3), .race-table tbody td:nth-child(3) { text-align: left; }
.race-table .race-team { font-size: 14px; font-weight: 500; overflow-wrap: anywhere; }
.race-table .race-points { font: 700 24px 'Barlow Condensed', sans-serif; }
.race-table .race-best { color: #C017BD; }
.race-table .race-improving { color: #27B933; }
.race-table .race-worsening { color: #E4EB57; }
.race-table .race-delta { color: var(--muted); font-size: 16px; font-weight: 600; }
@media (min-width: 768px) {
 .history-section { width: calc(100vw - 64px); margin-left: calc((100% - (100vw - 64px)) / 2); }
 .history { min-width: 0 !important; }
 .history th, .history td { width: auto; }
 .history tbody th, .history tbody td { font-size: 20px; }
 .history thead th { font-size: 10px; }
}
.chart-section { margin-top: 44px; }
.chart-controls { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; margin-bottom: 20px; }
.chart-controls fieldset { border: 1px solid var(--line); padding: 8px; margin: 0; display: flex; flex-wrap: wrap; gap: 10px; }
.chart-controls legend { font-size: 11px; color: var(--muted); padding: 0 4px; }
.chart-controls label { font-size: 13px; display: flex; align-items: center; gap: 4px; cursor: pointer; }
.chart-controls input { accent-color: var(--red); }
.chart-frame { position: relative; height: 360px; width: 100%; }
.chart-status { color: var(--muted); font-size: 13px; }
.poster-section { margin-top: 44px; }
.poster-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 20px; }
.poster-head h2 { margin: 0; }
.download { background: var(--red); color: #fff; border: 0; border-radius: 4px; padding: 10px 14px; font: 600 13px 'Barlow', sans-serif; cursor: pointer; white-space: nowrap; }
.download:disabled { opacity: .5; cursor: default; }
.download:focus-visible { outline: 2px solid #fff; outline-offset: 3px; }
#standings-poster { display: block; width: 100%; height: auto; aspect-ratio: 1122 / 1402; }
.site-footer { display: flex; justify-content: space-between; gap: 12px; margin-top: 32px; padding: 14px 0 4px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; font-weight: 500; }
@keyframes arrive { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
@media (prefers-reduced-motion: reduce) { tbody tr { animation: none; } }
@media (max-width: 480px) {
 main { padding: 28px 12px; }
 h1 { font-size: 30px; }
 header { padding-bottom: 24px; }
 .weekend-schedule { gap: 6px; }
 .weekend-card { padding: 10px 8px; }
 .weekend-current { padding: 12px 10px; }
 .weekend-card h2 { gap: 5px; font-size: 14px; }
 .weekend-current h2 { font-size: 18px; }
 .country-flag { width: 21px; height: 14px; }
 .weekend-label { gap: 4px; font-size: 9px; }
 .weekend-flag { width: 15px; height: 10px; background-size: 5px 5px; }
 .weekend-dates { font-size: 10px; }
 thead th { padding: 12px 4px; font-size: 10px; }
 tbody td, tbody th { padding: 10px 4px; height: 64px; }
 tbody th { font-size: 25px; }
 .points { font-size: 24px; }
 .position { font-size: 17px; }
 .interval, .gap { font-size: 14px; }
 .team-value { font-size: 16px; }
 main > table tbody th { font-size: 18px; white-space: nowrap; }
 main > table .points { font-size: 20px; white-space: nowrap; }
 main > table .position { font-size: 14px; white-space: nowrap; }
}
</style>
</head>
<body>
<main>
<header><div><p class="eyebrow">F1 FANTASY</p><h1>LEAGUE STANDINGS</h1></div><div class="header-actions"><button class="form-trigger" type="button" id="open-form" aria-label="Open feedback form" title="Open feedback form"><img src="max%20nerd.png" alt=""></button><div class="flag" aria-hidden="true"></div></div></header>
__WEEKENDS__
<div class="meta"><span>LEAGUE __LEAGUE__</span><span>__COUNT__ ENTRIES &middot; OVERALL</span></div>
<table aria-label="League standings. Intervals and leader gaps in points.">
<colgroup><col class="rank"><col class="driver"><col class="score"><col class="value"><col class="delta"><col class="delta"></colgroup>
<thead><tr><th scope="col">POS</th><th scope="col">DRIVER</th><th scope="col">POINTS</th><th scope="col" title="Current prices of the lineup shown in the leaderboard, excluding cash remaining">TEAM VALUE</th><th scope="col">INTERVAL</th><th scope="col">TO LEADER</th></tr></thead>
<tbody>__ROWS__</tbody>
</table>
__RACE_TABLE__
<section class="chart-section" aria-labelledby="chart-title">
<h2 id="chart-title">GAP TO LEADER</h2>
<div class="meta"><span>Completed GP total</span><span>To leader</span></div>
<div class="chart-frame"><canvas id="evolution" role="img" aria-label="Cumulative gap to leader after each completed Grand Prix"></canvas></div>
<p class="chart-status" id="chart-status" role="status" hidden></p>
</section>
__HISTORY__
<section class="poster-section" aria-labelledby="poster-title">
<div class="poster-head"><h2 id="poster-title">CURRENT STANDINGS</h2><button type="button" class="download" id="download-poster" disabled>Download PNG</button></div>
<canvas id="standings-poster" width="1122" height="1402" role="img" aria-label="League participants represented by racing cars on a track, with surname abbreviations and total points"></canvas>
<p class="chart-status" id="poster-status" role="status" hidden></p>
</section>
<footer class="site-footer"><span>developed by Директор дощової гонки</span><span>2026</span></footer>
</main>
<dialog class="form-dialog" id="form-dialog" aria-label="Feedback form">
<div class="form-dialog-head"><button class="form-close" type="button" id="close-form" aria-label="Close form">&times;</button></div>
<iframe src="https://docs.google.com/forms/d/e/1FAIpQLSd-sQ6Hy8gti1nbIHb5oL9iCGivyJRbbuo4MBXFjtz9sfYyqQ/viewform?embedded=true" width="640" height="418" frameborder="0" marginheight="0" marginwidth="0" title="Feedback form">Loading...</iframe>
</dialog>
<script id="chart-data" type="application/json">__CHART_DATA__</script>
<script id="poster-data" type="application/json">__POSTER_DATA__</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.8/dist/chart.umd.min.js"></script>
<script>
const data = JSON.parse(document.getElementById('chart-data').textContent);
const colors = __COLORS__;
const numberFormat = new Intl.NumberFormat('en', {maximumFractionDigits: 2});
const formDialog = document.getElementById('form-dialog');
document.getElementById('open-form').addEventListener('click', () => formDialog.showModal());
document.getElementById('close-form').addEventListener('click', () => formDialog.close());
formDialog.addEventListener('click', event => {
 if (event.target === formDialog) formDialog.close();
});
if (typeof Chart === 'undefined' || !data.labels.length) {
 const status = document.getElementById('chart-status');
 status.hidden = false;
 status.textContent = data.labels.length ? 'Chart could not load. Check your internet connection.' : 'No completed Grand Prix';
} else {
 new Chart(document.getElementById('evolution'), {
	type: 'line', data: {labels: data.labels, datasets: data.series.map((entry, index) => ({
	 label: entry.code, fullName: entry.name, data: entry.total.gap,
	 borderColor: colors[index % colors.length], backgroundColor: colors[index % colors.length],
	 borderWidth: 2, pointRadius: 3, pointHoverRadius: 6, spanGaps: false, tension: 0
	}))},
	options: {responsive: true, maintainAspectRatio: false,
	 interaction: {mode: 'index', intersect: false},
	 plugins: {legend: {position: 'bottom', labels: {color: '#f5f5f5', usePointStyle: true, boxWidth: 8}},
		tooltip: {callbacks: {label: context => {
		 const previous = context.dataset.data[context.dataIndex - 1];
		 let delta = 'n/a';
		 if (context.dataIndex > 0 && previous !== null && Number.isFinite(previous)) {
			const difference = context.parsed.y - previous;
			delta = (difference > 0 ? '+' : '') + numberFormat.format(difference);
		 }
		 return context.dataset.fullName + ': ' + context.formattedValue + ' pts (' + delta + ')';
		}}}},
	 scales: {x: {ticks: {color: '#a4a4aa', maxRotation: 0, autoSkip: true, maxTicksLimit: 6}, grid: {color: '#242426'}},
		y: {reverse: true, min: 0, ticks: {color: '#a4a4aa'}, grid: {color: '#333337'}, title: {display: true, color: '#a4a4aa', text: 'Gap to leader (pts)'}}}}
 });
}
const posterData = JSON.parse(document.getElementById('poster-data').textContent);
const posterCanvas = document.getElementById('standings-poster');
const downloadPoster = document.getElementById('download-poster');
function posterError() {
 const status = document.getElementById('poster-status');
 status.hidden = false;
 status.textContent = 'Poster background unavailable.';
 posterCanvas.hidden = true;
}
function drawCar(context, centerX, centerY, color, position) {
 context.save();
 context.translate(centerX, centerY);
 context.shadowColor = '#0009'; context.shadowBlur = 6; context.shadowOffsetX = 3;
 context.fillStyle = '#101014';
 for (const wheelX of [-27, 16]) {
  for (const wheelY of [-34, 22]) context.fillRect(wheelX, wheelY, 11, 24);
 }
 context.fillRect(-24, -49, 48, 9);
 context.fillRect(-27, 44, 54, 8);
 context.fillStyle = color;
 context.beginPath();
 context.moveTo(-10, -43); context.lineTo(10, -43); context.lineTo(18, -12);
 context.lineTo(13, 14); context.lineTo(5, 47); context.lineTo(-5, 47);
 context.lineTo(-13, 14); context.lineTo(-18, -12); context.closePath(); context.fill();
 context.shadowBlur = 0; context.shadowOffsetX = 0;
 context.strokeStyle = '#ffffff99'; context.lineWidth = 2;
 context.beginPath(); context.moveTo(0, 18); context.lineTo(0, 41); context.stroke();
 context.fillStyle = '#121216'; context.beginPath(); context.ellipse(0, 0, 8, 14, 0, 0, Math.PI * 2); context.fill();
 context.strokeStyle = '#dddddd'; context.beginPath(); context.arc(0, 2, 10, 0, Math.PI); context.stroke();
 context.fillStyle = '#fff'; context.font = "bold 13px 'Barlow', sans-serif";
 context.textAlign = 'center'; context.fillText(String(position), 0, -24);
 context.restore();
}
if (!posterData.background) {
 posterError();
} else {
 const background = new Image();
 background.onerror = posterError;
 background.onload = async () => {
  await document.fonts.ready;
  const context = posterCanvas.getContext('2d');
  context.drawImage(background, 0, 0, 1122, 1402);
	context.drawImage(background, 380, 340, 340, 370, 380, 180, 385, 970);
	for (let row = 0; row < 2; row++) {
	 for (let column = 0; column < 39; column++) {
		context.fillStyle = (row + column) % 2 ? '#a6a6a6' : '#f2f2f2';
		context.fillRect(338 + column * 11, 1118 + row * 10, 11, 10);
	 }
	}
  const entries = posterData.entries;
	const carBodyLength = 102;
	let centerY = 1053;
  entries.forEach((entry, index) => {
	 if (index > 0) {
		const pointDifference = Math.max(0, entries[index - 1].points - entry.points);
		centerY -= Math.max(carBodyLength / 2, pointDifference / 200 * carBodyLength);
	 }
   const color = colors[index % colors.length];
   const leftSide = index % 2 === 0;
   const carX = leftSide ? 485 : 660;
   drawCar(context, carX, centerY, color, index + 1);
   const labelX = leftSide ? 285 : 700;
   context.fillStyle = '#0b0b0de8'; context.fillRect(labelX, centerY - 38, 150, 72);
   context.fillStyle = color; context.fillRect(leftSide ? labelX + 145 : labelX, centerY - 38, 5, 72);
   context.textAlign = leftSide ? 'right' : 'left'; context.fillStyle = '#fff';
   context.font = "800 38px 'Barlow Condensed', sans-serif";
   context.fillText(entry.code, leftSide ? labelX + 135 : labelX + 15, centerY - 8, 125);
   context.font = "600 30px 'Barlow Condensed', sans-serif";
   context.fillText(numberFormat.format(entry.points), leftSide ? labelX + 135 : labelX + 15, centerY + 23, 125);
  });
  context.fillStyle = '#101011'; context.fillRect(0, 1190, 1122, 212);
  context.fillStyle = '#ff302b'; context.fillRect(48, 1220, 88, 6);
  context.fillStyle = '#fff'; context.textAlign = 'center';
  context.font = "800 86px 'Barlow Condensed', sans-serif";
  context.fillText('CURRENT STANDINGS', 561, 1310, 1030);
  context.fillStyle = '#bcbcc2'; context.font = "600 28px 'Barlow', sans-serif";
  context.fillText('F1 FANTASY / ' + posterData.league + ' / OVERALL', 561, 1364, 1000);
  downloadPoster.disabled = false;
 };
 background.src = posterData.background;
 downloadPoster.addEventListener('click', () => {
  const link = document.createElement('a');
  link.download = 'f1-standings-' + posterData.league.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') + '.png';
  link.href = posterCanvas.toDataURL('image/png');
  link.click();
 });
}
</script>
</body>
</html>
""".replace("__WEEKENDS__", weekend_section).replace("__LEAGUE__", escape(league_name)).replace("__COUNT__", str(len(standings))).replace("__ROWS__", "\n".join(rows)).replace("__RACE_TABLE__", race_section).replace("__HISTORY__", history_section).replace("__CHART_DATA__", chart_data).replace("__POSTER_DATA__", poster_data).replace("__COLORS__", json.dumps(PARTICIPANT_COLORS))


def main() -> None:
	parser = argparse.ArgumentParser(description="F1 Fantasy private league members and points.")
	parser.add_argument("league_id", type=int)
	parser.add_argument("--league-name", default="Private League", help="Display name used in the report instead of the league ID.")
	args = parser.parse_args()
	try:
		members = get_members(args.league_id)
		try:
			prices = get_player_prices()
		except (URLError, TimeoutError, ValueError, KeyError, TypeError, OSError) as error:
			prices = None
			print(f"Team values unavailable: {error}", file=sys.stderr)
		standings = build_standings(members, prices)
		fixtures = get_schedule_fixtures()
		weekends = get_weekend_schedule(fixtures)
		history = get_history(args.league_id, fixtures)
		output = Path(__file__).with_name("index.html")
		output.write_text(render_standings(args.league_name, standings, history, weekends), encoding="utf-8")
	except HTTPError as error:
		sys.exit(f"F1 Fantasy HTTP error: {error.code}")
	except (URLError, TimeoutError, ValueError, KeyError, TypeError, OSError) as error:
		sys.exit(f"Could not retrieve league members: {error}")
	print(output)
	webbrowser.open(output.as_uri())


if __name__ == "__main__":
	main()
