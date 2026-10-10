# Did Germany really save electricity in the 2022 energy crisis?

<!-- HEADLINE:START -->
## Germany used 8.8% less grid electricity in winter 2022/23 than the weather predicted (90% range 6.2% to 11.2%).

In winter 2022/23 (October to March), Germany's grid delivered 8.8% less electricity than the weather and calendar predicted (90% range 6.2% to 11.2%), about 23.1 TWh. Demand has NOT fully come back: in 2025/26 it was still 4.2% below the pre-crisis pattern (range 1.5% to 6.8%). Counting only dark hours (when rooftop solar can't hide demand), the 2022/23 saving was 9.2%.

*Updated automatically on 2026-10-10. Full report: [reports/REPORT.md](reports/REPORT.md)*
<!-- HEADLINE:END -->

In winter 2022/23 Germany was asked to save energy, and electricity use did fall. But was that a real
saving, or just a mild winter and factories that had shut down? To answer it you need to know what
demand **would have been** without the crisis. This project builds that forecast, checks it on normal
winters where the answer is known, and measures the gap, with an honest range around it.

It also runs every day: it forecasts tomorrow's German electricity demand hour by hour, scores
yesterday's forecast, and publishes everything on a small web page.

---

## What you get

| Part | What it does |
|---|---|
| **Savings analysis** | How much less electricity Germany used in each winter since 2022 than a normal winter with the same weather and holidays, with a 90% range. |
| **Proof it works** | The same method run on four normal winters (2016/17 to 2019/20) as if they were unknown. There the "saving" should be about zero, and it tells us how wide the ranges must be. |
| **Robustness views** | The answer under different choices: other training cut-off dates, only December to February, only dark hours (no rooftop solar), leaving out the lockdowns, correcting for the model's known drift, and a simple no-weather comparison. |
| **Who saved?** | When in the day and week the saving happened, compared with what energy-hungry industries produced. |
| **Daily forecast** | Tomorrow, hour by hour, with a 90% range, compared with "same week last year". |
| **Web page** | Tomorrow's forecast, the last 30 days of forecast vs. reality, the running error compared with the baseline, the savings result, and the date of the last retrain. |
| **Monthly retrain** | The daily model retrains on all data each month, and only replaces the old one if it beats the baseline on the last 4 weeks. |
| **Tests** | 71 tests: data checks, clock changes, holidays, every file format, no peeking at the future, and a full run on stand-in data where a known saving was planted and must be found. |

Full results with all charts: **[reports/REPORT.md](reports/REPORT.md)** (written automatically).
Method in detail: **[docs/METHOD.md](docs/METHOD.md)**.

---

## How it works, in plain words

1. **Download** hourly electricity use (SMARD, 2015 to today) and hourly temperature and sunshine
   from 19 weather stations where Germans live (DWD). Everything is stored in UTC time, so clock
   changes can't cause mistakes.
2. **Check the data**: every day has 24 hours (23 or 25 on clock-change days), no negative values,
   no gap longer than 2 hours.
3. **Learn what normal looks like**, using only data from before September 2022: how demand depends
   on the hour, the weekday, holidays in each state, school holidays, Christmas, temperature, sunshine
   and the 2020 lockdowns.
4. **Test it** on normal winters it has never seen. It must get them right before we trust it.
5. **Forecast the crisis winters as if nothing had happened**, using the real weather and calendar.
   The gap between this forecast and what really happened is the saving.
6. **Put a range on it**, based on how far off the method was in the normal winters, from the same
   distance ahead. Winters further from the training data get wider ranges.
7. **Repeat for every winter since**, to see if demand came back.
8. **Look for other explanations** and say clearly what the data can't tell apart.

---

## Quick start

You need Python 3.10 or newer.

```bash
git clone <your-repo-url> de-power-savings
cd de-power-savings
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

desave check-sources               # step 0: can every source be downloaded automatically?
desave all                         # download since 2015, analyse, train, build the page (20-60 min)
```

Then open `site/index.html` and `reports/REPORT.md`. The headline at the top of this README is
filled in with your real number.

**Try it without internet first:**

```bash
desave demo                         # runs everything on realistic stand-in data (about 3 minutes)
```

The demo starts a small local server that serves fake data in exactly the SMARD and DWD formats,
with a known saving planted in it, so you can see every output and check the method finds the
saving.

### Everyday commands

| Command | When |
|---|---|
| `desave daily` | every morning: download, forecast tomorrow, score, rebuild the page |
| `desave retrain` | once a month: retrain the daily model and refresh the savings analysis |
| `desave analyse` | rerun the savings analysis and report only |
| `desave check` | run the data checks and print what they found |
| `desave site` | rebuild the web page only |
| `pytest -m "not slow"` | fast tests (seconds) |
| `pytest` | all tests, including the planted-saving run (a few minutes) |
| `pytest -m realdata` | extra tests on your downloaded real data |

---

## Running it automatically (free, on GitHub)

1. Push this folder to a GitHub repository.
2. **Settings → Pages → Source: GitHub Actions.**
3. Nothing else to set up: if SMARD ever misses hours, the project fills them automatically from
   **Energy-Charts** (Fraunhofer ISE), which needs no key. *(Optional extra backup: an ENTSO-E key
   saved as the secret `ENTSOE_API_KEY`. You don't need it.)*
4. **Actions → "first run" → Run workflow.** This downloads everything, runs the analysis and
   publishes the page.
5. From then on, **"daily forecast"** runs every morning and **"monthly retrain"** on the 1st of each
   month. Data, forecasts and scores are saved back into the repository, one small file per month.

Prefer your own computer or server? See `scripts/cron_example.txt`.

---

## Project layout

```
config/settings.yaml          every choice in one place (dates, stations, model size...)
src/desave/
  sources/                    one downloader per source: smard, dwd, weather_forecast,
                              energy_charts (backup, no key), entsoe (optional backup),
                              opsd (cross-check), eurostat (industry)
  timeutils.py                UTC storage, German calendar days, clock-change-safe shifting
  validate.py                 data checks used by both the tests and the daily job
  features.py                 calendar, holidays by state, weather and lag inputs
  models/baseline.py          "same week last year" and "average of 2017-19"
  models/gbm.py               LightGBM model (3 seeds averaged, optional range models)
  models/counterfactual.py    "no crisis" model and the tests on normal winters
  models/operational.py       daily model with the 4-week safety check
  analysis/savings.py         savings per winter, ranges, robustness views, who-saved hints
  analysis/plots.py           all charts
  analysis/report.py          writes reports/REPORT.md and the README headline
  pipeline/daily.py           the daily job (+ backfill of the last 30 days)
  pipeline/retrain.py         the monthly job
  web/                        the static web page
  demo/                       stand-in data and a local fake server
tests/                        71 tests
.github/workflows/            tests, daily, monthly, first run
```

---

## What the data can and can't tell you

- **It measures grid electricity, not all electricity.** Power made and used on the spot by rooftop
  solar never appears. The *dark hours only* view removes this effect.
- **It can't say who saved.** Timing (workdays vs. weekends, day vs. evening) and industry output are
  hints, not proof.
- **A closed factory and a thriftier household look the same.** Part of the "saving" may be lost
  production.
- **Prices and appeals happened at the same time**, so their effects can't be separated.
- **New heat pumps and electric cars add demand**, so the real effort to save may be bigger than the
  net figure shown.
- **Later winters are less certain**, because they are further from the training data. The ranges
  grow to show this.

Details and numbers: [reports/REPORT.md](reports/REPORT.md#what-this-analysis-can-and-cant-tell-apart).

---

## Data sources and licences

- Electricity: **Bundesnetzagentur | SMARD.de**, licence CC BY 4.0.
- Weather observations: **Deutscher Wetterdienst (DWD)**, opendata.dwd.de.
- Weather forecasts: DWD MOSMIX via **Bright Sky** (brightsky.dev); backup DWD ICON via **Open-Meteo**.
- Cross-check: **Open Power System Data**, time series package 2020-10-06.
- Industry output: **Eurostat**, table sts_inpr_m.
- Backup demand: **Energy-Charts, Fraunhofer ISE** (energy-charts.info); optional **ENTSO-E Transparency Platform**.
- Holidays and school holidays: **python-holidays**.

Code: MIT licence.
