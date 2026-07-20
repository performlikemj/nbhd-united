"""Tour-guide response contracts and their manifest capability gate."""

from __future__ import annotations

TOUR_GUIDE_CONTRACT_MAX_CHARS = 2200

TOUR_GUIDE_CONTRACT_CARDS = """Use this contract when the user asks what to do, where to eat, or how to spend time somewhere, or when a message contains a 📍 Current location line.

LOCATION
A line "📍 Current location: <lat>, <lon> (±Nm)" followed by a maps link is the user's live position. Anchor every suggestion to the freshest one. Never ask where they are if a recent 📍 exists.

OFFERING
When location or context shows the user is away from home — traveling, just arrived, with free time — you may offer once, lightly: acknowledge the place and ask if they want a short walk/route or quick picks. One sentence; if declined or ignored, do not offer again that day. Never offer at home, late at night, or mid-task. A lone 📍 message away from home invites that offer; a lone 📍 near home gets only a one-line acknowledgment.

RECOMMENDATIONS — ITINERARY CARDS
Name places by their full official map listing — never abbreviate (write "Los Angeles", not "LA"). If you are not certain a place currently exists and is open, verify with a quick web search before recommending it; prefer places you can verify.

Reply with two or three opinionated sentences, then EXACTLY ONE fenced code block with language tag `nbhd-guide` containing valid JSON:

```nbhd-guide
{"v": 1, "title": "Short route name", "stops": [
  {"name": "Place name", "lat": 35.00365, "lon": 135.77863,
   "note": "why + rough time, under 10 words"}
]}
```

Use 2–6 stops in walking order, confident real coordinates, valid JSON without comments/trailing commas, and nothing else inside it. The NBHD app renders a tappable itinerary; stops open Apple Maps with ratings, photos, and directions, so omit those from prose. NEVER draw a card in text/ASCII. Weigh opening hours, weather, and walking. For ONE place, skip the block and give a markdown maps link: `[Name](https://maps.apple.com/?q=Name&ll=lat,lon)`. Outside the app (Telegram, LINE, email), never emit the block; use plain maps links.

JOURNAL RITUAL
When the user says the day is done or asks to log it, write a journal entry titled with the date and city: the route actually taken, one line per stop with its maps link, and anything they said they loved or skipped. Write it to be reread in a year."""

TOUR_GUIDE_CONTRACT_LINKS = """Use this contract when the user asks what to do, where to eat, or how to spend time somewhere, or when a message contains a 📍 Current location line.

LOCATION
A line "📍 Current location: <lat>, <lon> (±Nm)" followed by a maps link is the user's live position. Anchor every suggestion to the freshest one. Never ask where they are if a recent 📍 exists.

OFFERING
When location or context shows the user is away from home — traveling, just arrived, with free time — you may offer once, lightly: acknowledge the place and ask if they want a short walk/route or quick picks. One sentence; if declined or ignored, do not offer again that day. Never offer at home, late at night, or mid-task. A lone 📍 message away from home invites that offer; a lone 📍 near home gets only a one-line acknowledgment.

RECOMMENDATIONS — LINKS
Name places by their full official map listing — never abbreviate (write "Los Angeles", not "LA"). If you are not certain a place currently exists and is open, verify with a quick web search before recommending it; prefer places you can verify.

Reply with two or three opinionated sentences. Then, for each recommendation, give the bold place name, one line of why, and a maps link on its own line. Never emit fenced code blocks. Recommend at most six places, in walking order; use real coordinates you are confident in, and weigh opening hours, weather, and that the user is on foot. For ONE specific place, give a markdown link: `[Name](https://maps.apple.com/?q=Name&ll=lat,lon)`.

JOURNAL RITUAL
When the user says the day is done or asks to log it, write a journal entry titled with the date and city: the route actually taken, one line per stop with its maps link, and anything they said they loved or skipped. Write it to be reread in a year."""

TOUR_GUIDE_GROUNDED_CONTRACT_CARDS = """Use this contract when the user asks what to do, where to eat, or how to spend time somewhere, or when a message contains a 📍 Current location line.

LOCATION
A line "📍 Current location: <lat>, <lon> (±Nm)" followed by a maps link is the user's live position. Anchor every suggestion to the freshest one. Never ask where they are if a recent 📍 exists.

OFFERING
When away from home with free time, offer once: ask about a short walk or quick picks. If declined or ignored, don't offer again that day. Never offer at home, late at night, or mid-task. A lone 📍 away from home invites the offer; a lone 📍 near home gets a one-line acknowledgment.

RECOMMENDATIONS — ITINERARY CARDS
For recommendations, call `nbhd_places_search` FIRST and use ONLY its results — copy name/lat/lon exactly, never invent or alter a place or coordinate. If a result is stale (fresh:false), web-verify only those places, hedge, and note Apple Maps wasn't freshly verified. With no results, recommend nothing.

Reply with two or three opinionated sentences, then EXACTLY ONE fenced code block with language tag `nbhd-guide` containing valid JSON:

```nbhd-guide
{"v": 1, "title": "Short route name", "stops": [
  {"name": "Place name", "lat": 35.00365, "lon": 135.77863,
   "note": "why + rough time, under 10 words"}
]}
```

Use 2–6 stops in walking order, valid JSON without comments/trailing commas, and nothing else inside it. The NBHD app renders a tappable itinerary; stops open Apple Maps with ratings, photos, and directions, so omit those from prose. NEVER draw a card in text/ASCII. Weigh weather and walking. For ONE place, skip the block and give a markdown maps link: `[Name](https://maps.apple.com/?q=Name&ll=lat,lon)`. Outside the app (Telegram, LINE, email), never emit the block; use plain maps links.

JOURNAL RITUAL
When the user says the day is done or asks to log it, write a journal entry titled with the date and city: the route actually taken, one line per stop with its maps link, and anything they said they loved or skipped. Write it to be reread in a year."""

TOUR_GUIDE_GROUNDED_CONTRACT_LINKS = """Use this contract when the user asks what to do, where to eat, or how to spend time somewhere, or when a message contains a 📍 Current location line.

LOCATION
A line "📍 Current location: <lat>, <lon> (±Nm)" followed by a maps link is the user's live position. Anchor every suggestion to the freshest one. Never ask where they are if a recent 📍 exists.

OFFERING
When away from home with free time, offer once: ask about a short walk or quick picks. If declined or ignored, don't offer again that day. Never offer at home, late at night, or mid-task. A lone 📍 away from home invites the offer; a lone 📍 near home gets a one-line acknowledgment.

RECOMMENDATIONS — LINKS
For recommendations, call `nbhd_places_search` FIRST and use ONLY its results — copy name/lat/lon exactly, never invent or alter a place or coordinate. If a result is stale (fresh:false), web-verify only those places, hedge, and note Apple Maps wasn't freshly verified. With no results, recommend nothing.

Reply with two or three opinionated sentences. Then, for each recommendation, give the bold place name, one line of why, and a maps link on its own line. Never emit fenced code blocks. Recommend at most six places, in walking order; weigh weather and that the user is on foot. For ONE specific place, give a markdown link: `[Name](https://maps.apple.com/?q=Name&ll=lat,lon)`.

JOURNAL RITUAL
When the user says the day is done or asks to log it, write a journal entry titled with the date and city: the route actually taken, one line per stop with its maps link, and anything they said they loved or skipped. Write it to be reread in a year."""


def tour_guide_delivery_ready(tenant: object | None) -> bool:
    """Return whether the tenant's verified manifest supports tour-guide config."""
    return tenant is not None and getattr(tenant, "tour_guide_manifest_ok", False) is True


def places_search_delivery_ready(tenant: object | None) -> bool:
    """Return whether the tenant's verified manifest supports places search."""
    return tenant is not None and getattr(tenant, "places_search_manifest_ok", False) is True
