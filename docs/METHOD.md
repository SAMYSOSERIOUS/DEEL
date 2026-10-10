# Method, choices and lessons

This page explains every choice in plain words, so a reader can check it or disagree with it.

## 1. Data

| What | Source | Detail |
|---|---|---|
| Electricity demand | SMARD (Bundesnetzagentur), series 410 "Netzlast" | MWh per hour, 2015 to today. Downloaded through the same data feed the SMARD website uses (weekly files, times in milliseconds UTC). The last 3 weeks are downloaded again every day because SMARD corrects recent values. |
| Backup demand | Energy-Charts (Fraunhofer ISE) "Load", no key needed; optionally ENTSO-E "actual total load" | Only used to fill missing SMARD hours. The definitions differ a little, so the backup is scaled to SMARD with hour-of-day factors from recent hours where both exist. Filled hours are marked in the data. |
| Temperature, sunshine | DWD hourly station data | 19 points where people live, weighted by population (big states get two points). For each point the closest station with data from 2015 until now is chosen once and saved, so the inputs don't change from day to day. If a station is missing for an hour, the others are reweighted. |
| Weather forecast | Bright Sky (DWD MOSMIX), backup Open-Meteo (DWD ICON) | Asked for the exact station coordinates, so forecast and history describe the same places. |
| Holidays | python-holidays | Public holidays per state, weighted by state population; partial holidays (Assumption Day in Bavaria, Corpus Christi in a few Saxon and Thuringian towns); school holidays per state. |
| Cross-check | Open Power System Data, 2015 to Sept 2020 | Same shape expected, not the same level (different definition of load). |
| Industry | Eurostat sts_inpr_m | Monthly production index, 2021 = 100, for paper, chemicals, glass/cement and basic metals. |

**Time rule:** everything is stored in UTC. German local time is only used for calendar inputs and
charts. "Same time last week/year" is always matched on German wall-clock time, not on UTC, otherwise
it is one hour off for a few days each spring and autumn (tested in `tests/test_timezone.py`).

**Hour convention:** each value sits at the start of the hour it describes, like SMARD. DWD and the
forecast services report sunshine at the end of the hour it was counted over, so sunshine is moved
back by one hour.

## 2. Inputs the models see

- **Calendar:** German local hour, weekday, weekend, day of year, month, summer time.
- **Holidays:** share of Germans with a public holiday today, yesterday and tomorrow; nationwide
  holiday; share on a bridge day; share on school holidays; 24 Dec, 31 Dec, days from Christmas,
  the working days between Christmas and New Year.
- **Weather:** population-weighted temperature now, average of the last 24 hours, a slow 3-day
  average (buildings take time to cool down), daily average, heating degrees (below 15 °C), cooling
  degrees (above 20 °C), sunshine now and per day, sun angle.
- **Lockdown strength:** 1 for hard lockdowns, 0.5 for partial ones, 0 otherwise. Set to 0 for every
  "no crisis" forecast.
- **Level year:** which September-to-August year it is. See section 6.
- **Daily model only:** demand a week ago and two weeks ago (same German wall-clock hour), and the
  average of recent days that are at least 3 days old (newer days are not fully published when the
  forecast is made).

The "no crisis" model never sees past demand. If it did, it would learn the savings from the recent
past and hide them.

## 3. Models

- **Same week last year:** the value 364 days earlier (same weekday). The bar any model must clear.
- **Average of 2017-2019:** the same week averaged over three normal years. Used to show what you'd
  conclude without adjusting for weather.
- **LightGBM:** gradient-boosted trees on log(demand), so effects act as percentages. Three copies
  with different random seeds are averaged. The daily model adds two "range" models (5% and 95%).

## 4. Testing on normal winters (step 4)

Each of 2016/17, 2017/18, 2018/19 and 2019/20 is forecast using only data from before the September
before it (and also from 1, 2 and 3 years before), exactly like the crisis winters. Ten tests in
total. For each we record the whole-winter error, the daily and hourly error, and the error of the
two baselines.

## 5. The saving and its range (steps 5-7)

For each winter (October to March):

    saving = 1 − (real demand ÷ "no crisis" forecast)

In a normal winter, real ÷ forecast = 1 + e, where e is the method's error. The tests in step 4 show
that the typical size of e grows with distance from the training data. We fit

    e² ≈ a + b × (years ahead)

through the test errors (never below zero), so the typical error at h years ahead is √(a + b·h).
The 90% range uses a t-distribution with (number of tests − 2) degrees of freedom, because ten tests
is a small sample. That makes the ranges honest about how little we know.

## 6. Lessons from the planted-saving test

The demo data contains a saving we chose (6% at its peak in winter 2022/23, settling at 3.5%), plus
the things that make real data hard: rooftop solar growing every year, a slow downward trend,
lockdowns, clock changes. `tests/test_end_to_end.py` requires every winter's range to contain the
planted value. Building against this test caught two real problems:

1. **The first model read about 1.5% "saving" even in normal winters.** Without any sense of time it
   predicted the *average* level of 2015-2022, while demand had slowly drifted down. Fix: the
   "level year" input, so the model holds the most recent year's level.
2. **A smooth time input made it worse in a different way.** With a continuous date the trees
   memorised the last few summer weeks before the cut-off and carried them into winter, so 2022/23
   came out 1% too low, outside its range. Fix: count whole September-to-August years instead.

After both fixes, the estimates landed inside every range (2022/23: estimate 6.4%, range 4.9-7.8%,
planted 6.1%). The normal test winters still show a small positive "saving" (0.4-1.3%) because
demand kept falling slowly; the *bias corrected* view adjusts for exactly that.

A third bug was caught by the daily scores: the "recent level" input was empty for the day being
forecast, so live forecasts were missing an input the model was trained with (errors of 3-5%
instead of about 2%). Fixed, with a regression test.

## 7. Robustness views (step 8)

| View | Why |
|---|---|
| Cut-off 2021-09-01 / 2022-03-01 / 2022-09-01 | Prices rose from autumn 2021, so a September 2022 cut-off may already include early crisis behaviour. |
| December to February only | The core of winter, where heating matters most. |
| Dark hours only | Rooftop solar can't hide demand at night. |
| Lockdown months left out | Instead of marking them, drop them. |
| Corrected for known model bias | Removes the average error seen in the normal-winter tests (mostly the pre-crisis downward trend). |
| Dark hours + bias corrected | The most cautious estimate. |
| Simple 2017-19 average | Shows how much the weather adjustment changes the answer. |

## 8. Daily operation

- Forecasts are issued at 05:00 German time for the next German calendar day and saved as issued;
  they are never edited.
- A day is scored only once all its hours (23, 24 or 25) are published.
- The web page shows the running hourly error of the model and of "same week last year", and the
  share of hours inside the 90% range (should be about 90%).
- The monthly retrain trains on everything except the last 4 weeks, checks the result against the
  baseline on those 4 weeks, and only then trains on everything and replaces the model.
- The first run fills the last 30 days with "backfilled" forecasts, made with a model trained only
  on earlier data but using measured weather (old forecasts aren't available). They are marked on
  the page because they look slightly better than real forecasts.
