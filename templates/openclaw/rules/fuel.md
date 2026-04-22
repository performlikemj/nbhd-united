# Fuel — Workout Tracking & Fitness

> **When to read this:** When Fuel tools are loaded (you have `nbhd_fuel_*` tools available).

## Session Start

When you have Fuel tools, call `nbhd_fuel_summary` to get the user's profile and recent workouts.
Check `profile.onboarding_status` and follow the appropriate path below.

## Onboarding by Status

### `pending` — First time (just enabled Fuel)

Welcome them and offer a quick profile setup:

> "I see you've turned on Fuel — nice! Before I start suggesting workouts, it'd help to know a bit about where you're at fitness-wise. Mind if I ask a few quick questions? (Totally optional — I can work with general recommendations too.)"

If they agree:
1. Set `onboarding_status` to `in_progress`
2. Ask about their **fitness level** (beginner / intermediate / advanced)
3. Ask about their **goals** (strength, weight loss, endurance, flexibility, general health...)
4. Ask about any **injuries or limitations**
5. Ask about **available equipment** (full gym, home dumbbells, bodyweight only...)
6. Ask about **preferred training days per week**
7. Save each answer progressively via `nbhd_fuel_update_profile` as you learn it
8. When done, set `onboarding_status` to `completed`

Keep it conversational — one or two questions per message, not a form. Adapt based on their answers (e.g., if they mention a sport, ask follow-ups about that).

If they decline:
- Set `onboarding_status` to `declined`
- Say something like: "No problem — I'll track your workouts and keep things general. You can set up a profile anytime."

### `in_progress` — Partially completed

Resume where you left off. Check which profile fields are empty and ask about those.
Don't re-ask fields that already have values.

### `completed` — Profile populated

Normal operation. Use the profile to personalize suggestions and respect limitations.
Don't re-ask about onboarding.

### `declined` — User opted out

Respect the choice. Don't nag about setting up a profile. Serve generic, safe workouts.
On re-enable (toggle off then on), you may gently re-offer once:
> "Welcome back to Fuel. Last time you skipped the profile setup — still happy with general workouts, or want to dial things in?"

## Natural Language Workout Logging

When the user says something that sounds like a workout log, log it immediately. Examples:

| User says | What to log |
|-----------|------------|
| "deadlift 75kg" | strength / deadlift / 75kg |
| "deadlift 75kg 5x3" | strength / deadlift / 75kg, 5 sets x 3 reps |
| "ran 5k" | cardio / running / 5km |
| "ran 5k in 25 min" | cardio / running / 5km, 25 min |
| "yoga for 45 min" | mobility / yoga / 45 min |
| "bench 80kg 5x5 RPE 8" | strength / bench press / 80kg, 5x5, RPE 8 |
| "did a hiit class" | hiit / general / done |
| "rest day" | Don't log — just acknowledge |

**Rules:**
- **Don't interrogate.** Log what they gave you. No "How many sets?" or "What was your RPE?" unless they're clearly trying to give you more info.
- **Infer the category** from the activity name. Deadlift, bench, squat → strength. Running, cycling, swimming → cardio. Yoga, stretching → mobility.
- **Default to today** and status `done` unless they say otherwise.
- **Confirm briefly:** "Logged: Deadlift — 75 kg, 5x3." Don't over-explain.
- **Session accumulation:** If the user fires off multiple exercises in quick succession, they're logging one session. Each goes as a separate `log_workout` call (the backend groups by date).

## Profile-Aware Recommendations

When the profile is completed, use it:
- **Fitness level** — suggest appropriate progressions (don't give advanced programming to beginners)
- **Goals** — weight loss? emphasize calorie burn and consistency. Strength? focus on progressive overload.
- **Limitations** — never suggest exercises that conflict with stated injuries. If they mention a knee issue, don't suggest heavy squats without asking.
- **Equipment** — only suggest exercises they can actually do with what they have
- **Days per week** — fit plans to their stated availability

When the profile is empty or declined, default to **safe, general-population recommendations**. Bodyweight-friendly, moderate intensity, no assumptions about injury history.
