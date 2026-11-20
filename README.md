# LinkedIn Learning Automation

A local Python application that automates repetitive LinkedIn Learning course
navigation and playback while keeping the platform in control of progress. It
uses a visible Chromium window, watches the real HTML5 media element, and only
records a lesson as completed after LinkedIn Learning's own table of contents
shows a completion marker.

It also detects multiple-choice assessments, explains a **suggested** answer,
and then waits for you to select and submit the answer manually in the browser.

> This project does not request or store LinkedIn credentials, fake watch time,
> set completion flags, bypass locked content, or submit assessments. Use it in
> accordance with your LinkedIn Learning account terms and your organization's
> policies.

---

## Features

- Persistent Chromium profile with manual LinkedIn login.
- Batch input from CSV or text: run several courses sequentially in one browser
  session and one progress database.
- Authentication-expiry detection and a manual re-login wait.
- Real HTML5 video detection, play/pause, configurable playback speed, and
  stall/error monitoring.
- Table-of-contents discovery and lesson-by-lesson navigation using DOM
  locators (never fixed screen coordinates).
- Platform-verified completion: a lesson is only persisted as completed when
  the platform's curriculum marks it completed or navigates past it and the
  marker appears.
- SQLite progress storage with interrupted-session resume.
- MCQ detection (native radio inputs, ARIA widgets, and quiz containers), local
  heuristic suggestions, explanations, confidence, and a manual-submit wait.
- Rich live dashboard and command console.
- Structured file logging plus an in-memory activity feed.
- Graceful shutdown on `Ctrl+C`, `exit`, or `SIGTERM`.
- Typed exceptions and a test suite that uses real Chromium and a real short
  WebM clip.

---

## Requirements

- Python 3.12+
- Chromium installed through Playwright
- `ffmpeg` only for running the video-based tests
- A desktop session for the default non-headless browser

---

## Installation

```bash
cd /path/to/ln-automation

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium
```

Optional, for the test media fixture:

```bash
# Debian/Ubuntu
sudo apt-get install ffmpeg
```

---

## Configuration

Copy the example file and edit it:

```bash
cp .env.example .env
```

Configuration is resolved in this order, with each source overriding the
previous one:

1. Built-in defaults
2. `.env`
3. JSON config (`CONFIG_FILE` environment variable or `--config`)
4. Environment variables
5. CLI flags (`--course-url`, `--speed`, `--headless`, ...)

A JSON config can use `UPPER_CASE` or `snake_case` keys. Environment variables
must use the upper-case names.

### Core settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `COURSE_URL` | *(empty)* | Single LinkedIn Learning course URL, or a local test URL |
| `COURSES_FILE` | *(none)* | CSV/text file of course URLs; overrides `COURSE_URL` for the run |
| `BROWSER_PROFILE` | `./browser_profile` | Persistent Chromium profile (contains cookies; keep private) |
| `HEADLESS` | `false` | Keep `false` so you can log in manually |
| `PLAYBACK_SPEED` | `2.0` | Requested playback speed; validated between 0.25 and 16.0 |
| `MAX_PLAYBACK_SPEED` | `2.0` | Ceiling applied by the video controller (max 16.0) |
| `AUTO_START` | `true` | When false, wait for the `start` command before playing |
| `AUTO_NAVIGATE` | `true` | When false, pause after each lesson until you type `resume` |
| `AUTO_RESUME` | `true` | Resume from the last lesson recorded in SQLite |
| `DATABASE_PATH` | `./data/progress.db` | SQLite progress database |
| `LOG_DIR` | `./logs` | Rotating log directory |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `LOG_JSON` | `false` | One JSON object per log line |
| `ASSESSMENT_ENABLED` | `true` | Detect quizzes and show suggestions |
| `ASSESSMENT_WAIT_TIMEOUT` | `300` | Seconds to wait for manual quiz submission |
| `AUTH_TIMEOUT` | `300` | Seconds to wait for manual login |
| `MAX_RETRIES` | `3` | Attempts per lesson before it is left failed |
| `MAX_CONSECUTIVE_FAILURES` | `3` | Safety stop when many different lessons fail |
| `SELECTORS_PATH` | *(none)* | Optional JSON selector overrides |
| `VERIFY_VIA_COURSE_PAGE` | `true` | Re-check the course page if the lesson page hides the TOC |

Playback-related settings such as `POLL_INTERVAL`, `STALL_TIMEOUT`,
`PLAYBACK_TIMEOUT`, `COMPLETION_TIMEOUT`, `VIEWPORT_WIDTH`, and
`VIEWPORT_HEIGHT` are also available; see `config.py` for defaults and
validation.

Chromium's HTML5 media element accepts rates up to **16x**; 32x raises
`NotSupportedError`. The controller clamps to the configured ceiling and that
16x hard limit, then logs the **actual** `playbackRate` the player accepts.
LinkedIn may reset or ignore a high rate; when it does, the tool continues at
the accepted rate and still requires the platform's own completion marker.
The shipped `.env.example` opts into 16x; the built-in default remains 2x.

### Selector overrides

All DOM selectors live in `utils/selectors.py`. If LinkedIn changes its markup,
create a JSON file and point `SELECTORS_PATH` at it:

```json
{
  "toc_root": [".classroom-toc", "[data-test-id='course-toc']"],
  "toc_item_container": ["[data-test-id='toc-item']", "li"],
  "video": ["video.vjs-tech", "video"]
}
```

Only the groups you list are replaced; the rest keep their defaults.

---

## Running

```bash
# Uses COURSE_URL from .env or config.json
.venv/bin/python main.py

# Explicit URL and speed
.venv/bin/python main.py \
  --course-url "https://www.linkedin.com/learning/python-essential-training" \
  --speed 2.0

# Batch mode: read course URLs from a text file
.venv/bin/python main.py --courses-file examples/courses.txt

# Batch mode from CSV (same database, courses run one after another)
.venv/bin/python main.py --courses-file examples/courses.csv --speed 2.0

# Resolved configuration without opening a browser
.venv/bin/python main.py --show-config

# Saved progress; if --course-url is omitted, all saved courses are listed
.venv/bin/python main.py --progress --course-url "https://www.linkedin.com/learning/..."

# Clear local progress for one course (LinkedIn is not modified)
.venv/bin/python main.py --reset --course-url "https://www.linkedin.com/learning/..."

# Plain log output instead of the live dashboard (also useful for CI/debugging)
.venv/bin/python main.py --no-dashboard
```

### Batch course files

`--courses-file` (or `COURSES_FILE`) makes the app process multiple courses
sequentially. It takes precedence over `--course-url` / `COURSE_URL`; a warning
is printed if both are supplied.

**Text format** — one URL per line. Blank lines, `#` comments, bullets (`-`,
`*`, `•`), and trailing `# notes` are ignored:

```text
# courses.txt
https://www.linkedin.com/learning/python-essential-training
- https://www.linkedin.com/learning/learning-sql  # do this next
https://www.linkedin.com/learning/git-github
```

**CSV format** — a header is optional. The first URL-like cell in each row is
used, so extra title/note columns are fine. A header containing `url`,
`course_url`, `link`, or `course` is skipped:

```csv
course_url,title,note
https://www.linkedin.com/learning/python-essential-training,Python Essential Training,start here
https://www.linkedin.com/learning/learning-sql,Learning SQL,next
```

URLs are de-duplicated (ignoring a trailing `/`) and validated before the
browser opens. Invalid lines stop startup with a file-and-line error instead of
silently skipping a course. See `examples/courses.txt` and
`examples/courses.csv`.

In batch mode the browser opens once, each course gets its own curriculum sync
and session record in the same SQLite database, and a **BATCH SUMMARY** table is
printed at the end. A failed course does not stop later courses; `Ctrl+C` /
`exit` stops the whole batch after the current lesson.

CLI overrides:

```text
--config PATH          JSON config file
--course-url URL       Course URL (ignored when --courses-file is set)
--courses-file PATH    CSV/text list of course URLs (batch mode)
--speed FLOAT          Playback speed (0.25-16.0; Chromium HTML5 max)
--headless / --no-headless
--profile PATH         Chromium profile directory
--database PATH        SQLite database path
--log-level LEVEL
--show-config          Print resolved settings and exit
--progress             Print saved progress and exit
--reset                Reset local progress for --course-url
--no-dashboard         Disable the Rich live region
```

On first use, Chromium opens. Log in to LinkedIn manually if prompted. The
application polls for authentication, then opens the course and begins
discovering the curriculum. Credentials are never requested by this tool.

---

## Terminal commands

The automation starts automatically when `AUTO_START=true`. Type a command in
the terminal while it runs:

| Command | Action |
| --- | --- |
| `start` | Start/resume the automation (or report that it is already running) |
| `pause` | Pause playback at the next poll |
| `resume` | Resume a paused run |
| `progress` | Show persisted course progress |
| `speed 2.5` | Change playback speed for the active player |
| `exit` | Graceful shutdown; progress is saved |
| `help` | Show the command list |

`stop` is accepted as an alias for `pause`; `quit` is an alias for `exit`.
With `AUTO_NAVIGATE=false`, the tool pauses after each lesson so you can inspect
the browser before typing `resume`.

If stdin is not a TTY (for example a pipe), the command listener is disabled;
the automation still runs and logs normally.

---

## Workflow

1. Launch Chromium once with the persistent profile (one run, optionally many
   courses from `--courses-file`).
2. Wait for manual login if the session is expired.
3. Open the first/next configured course and discover sections/lessons from the TOC.
4. Synchronize discovered lessons with SQLite and reconcile the platform's
   completion markers.
5. Resume at the first pending, unlocked lesson.
6. Open the lesson by its DOM/URL target and find the real `<video>` element.
7. Start playback at the configured or changed speed.
8. Monitor the media clock, buffering, stalls, pause requests, and player
   errors. Never seek or alter progress.
9. Wait for the platform to register completion (TOC marker, or a course-page
   recheck for layouts that hide the TOC).
10. Persist the verified completion immediately, then select the next lesson.
11. If an assessment appears, show the question, options, suggested answer,
    explanation and confidence. Wait for **you** to submit it in the browser.
12. Repeat until no accessible pending lessons remain in the current course,
    then move to the next course (if any) and finally print the course/batch
    summaries.

Locked, unavailable, or repeatedly failing lessons are recorded with their
status and do not crash the run. A single lesson failure is retried up to
`MAX_RETRIES`; three different immediate failures trigger the safety stop so a
course-wide layout change does not spin forever.

---

## Assessment assistant

Detection is read-only. It finds native radio/checkbox groups, ARIA
radio/option widgets, and explicit quiz containers, then normalizes the question
and options.

The suggestion engine is a **local keyword/context heuristic**:

- No network requests, LLM calls, or answer-key scraping.
- It is explainable: matched question/lesson terms are shown.
- It reports `Low` confidence and says "no confident suggestion" when the
  signal is weak.
- Negated questions ("which is NOT...") are explicitly downgraded because the
  heuristic does not invert them.
- It has no browser control: it cannot click, select, type, or submit.

Example panel:

```text
╭──────────────────────────── ASSESSMENT DETECTED — 1/1 ────────────────────────────╮
│ Question                                                                          │
│ Which protocol is commonly used for secure web communication?                     │
│                                                                                   │
│ Options                                                                           │
│ A. HTTP                                                                           │
│ B. HTTPS                                                                          │
│ C. FTP                                                                            │
│ D. SMTP                                                                           │
│                                                                                   │
│ Suggested answer: none — insufficient signal                                      │
│                                                                                   │
│ Explanation                                                                       │
│ No option has a meaningful term overlap with the question or the lesson context.  │
│ Review the lesson material and choose manually.                                   │
│                                                                                   │
│ Confidence: Low                                                                   │
│                                                                                   │
│ Heuristic suggestion only. It is not an authoritative answer and nothing is       │
│ submitted automatically — verify against the lesson and submit manually.          │
│                                                                                   │
│ Select and submit your answer manually in the browser.                            │
╰───────────────────────────────────────────────────────────────────────────────────╯
```

If the quiz does not close before `ASSESSMENT_WAIT_TIMEOUT`, the lesson is left
pending (never marked complete) and the run continues according to the retry
rules.

---

## Progress and resume

SQLite stores courses, lessons, sessions and structured events under
`DATABASE_PATH`:

- course URL/title and total lessons;
- current lesson, status, playback speed;
- per-lesson status, attempts, watched media duration, last error, completion
  timestamp;
- session start/end and lesson counts;
- recent events used for diagnostics.

Rerunning the same command resumes automatically: already-completed lessons are
skipped, and `AUTO_RESUME=true` prefers the last recorded lesson if it is still
pending. With `--courses-file`, every listed course stores its own record in the
same database, so rerunning the same file resumes each course independently.
`--reset` only clears local state; it never changes anything on LinkedIn
Learning.

A lesson's `watched_seconds` is the media position reached when playback
finished (a diagnostic value), not a claim sent to LinkedIn. Platform
completion remains the only thing that marks a lesson complete locally.

---

## Example dashboard

Representative output (`AUTO_START=true`):

```text
╭───────────────────────────── LINKEDIN LEARNING AUTOMATION ─────────────────────────────╮
│ Python Essential Training                                              ● RUNNING      │
│ https://www.linkedin.com/learning/python-essential-training                           │
│                                                                                        │
│ Total          Completed        Remaining        Skipped / Locked                      │
│ 32             12               20               0 / 0                                 │
│                                                                                        │
│ ██████████████░░░░░░░░░░░░░░░░░░░░░░░░░░          37%                                  │
│                                                                                        │
│ Current lesson                                                                         │
│ Working with Lists  (Core Python)                                                       │
│ Playback 2.00x  4:12 / 8:37   watched 4:12                                             │
│ Waiting for the platform to register completion                                         │
│                                                                                        │
│ Recent activity                                                                         │
│ 14:22:10 INFO    Lesson verified complete: Dictionaries                                 │
│ 14:22:11 INFO    Lesson started: Working with Lists                                     │
│ 14:22:12 INFO    Playback started: Working with Lists                                   │
│ 14:26:24 WAITING Waiting for the platform to register completion                        │
│                                                                                        │
│ Commands: start · pause · resume · progress · speed <x> · stop · exit                  │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

Final summary:

```text
╭──────────────────────────────── SESSION COMPLETE ─────────────────────────────────╮
│ Course: Python Essential Training                                                 │
│ Status: ● FINISHED                                                                │
│                                                                                   │
│ Lessons discovered: 32                                                            │
│ Completed: 32                                                                     │
│ Remaining: 0                                                                      │
│ Skipped: 0                                                                        │
│ Locked / unavailable: 0                                                           │
│ Watch time recorded: 1:48:12                                                      │
│ Assessments shown: 2                                                              │
╰───────────────────────────────────────────────────────────────────────────────────╯
```

Batch mode adds a final table:

```text
                                  BATCH SUMMARY
┏━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ # ┃ Course                    ┃ Status   ┃ Lessons ┃ Error                ┃
┡━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ 1 │ Python Essential Training │ FINISHED │ 32/32   │ —                    │
│ 2 │ Learning SQL              │ ERROR    │ 3/18    │ curriculum not found │
└───┴───────────────────────────┴──────────┴─────────┴──────────────────────┘
```

---

## Running the tests

```bash
.venv/bin/python -m pytest
```

The suite covers configuration, CSV/text course lists and batch sequencing,
SQLite progress/resume, real Chromium sessions, real WebM playback, curriculum
discovery/navigation, assessment detection and suggestions, terminal
UI/commands, CLI progress/reset, and end-to-end local course runs including
resume and locked-lesson behavior. Video tests require `ffmpeg`; browser tests
require `playwright install chromium`.

Useful subsets:

```bash
.venv/bin/python -m pytest tests/test_integration.py -q
.venv/bin/python -m pytest tests/test_assessment.py tests/test_ui.py -q
```

---

## Project layout

```text
main.py                     CLI + orchestration workflow
config.py                   Pydantic settings and file/env resolution
requirements.txt
README.md
browser/
    session.py              Persistent Playwright Chromium context
    manager.py              Navigation, auth detection, browser recovery
player/
    video_controller.py     HTML5 video control and playback monitoring
course/
    curriculum.py           TOC extraction, curriculum model, navigator
    progress.py             Resume planning and verified lesson transitions
assessment/
    detector.py             Read-only DOM/frame question detection
    extractor.py            Question/option normalization
    assistant.py            Local explained suggestions
database/
    models.py               Domain models and status enums
    storage.py              SQLite schema and persistence
ui/
    dashboard.py            Live Rich dashboard and summary
    terminal.py             Assessment panels and command console
utils/
    course_list.py          CSV/text course URL parser
    logger.py               Rotating/structured logging
    exceptions.py           Typed exception hierarchy
    selectors.py            Centralized selector groups
examples/                   Sample courses.txt and courses.csv
tests/                      Unit, browser, playback, UI, CLI, integration
logs/                       Runtime logs (created automatically)
data/                       Progress database (created automatically)
browser_profile/            Chromium profile (created automatically; private)
```

`data/`, `logs/`, and `browser_profile/` are ignored by git. The profile
contains LinkedIn cookies, so treat it like a credential and do not share it.

---

## Troubleshooting

**The course page shows a login wall.**
Log in manually in the opened window. The tool waits up to `AUTH_TIMEOUT`. If
the window is headless, rerun with `HEADLESS=false`.

**Chromium says the profile is already in use.**
Close the other Chromium window using the same `BROWSER_PROFILE`, or delete a
stale profile only after closing all browsers.

**No curriculum lessons are found.**
Open the course in the browser and confirm the table of contents is visible.
Increase the wait by checking `SELECTORS_PATH` overrides in
`utils/selectors.py`. A layout change usually only requires updating a selector
group, not application logic.

**No video element is found.**
The lesson may be a quiz, a locked item, or a player in an unsupported iframe.
The detector scans frames for assessments; video control looks for a real
`<video>`. Play the lesson manually once to confirm it is available.

**Playback stalls.**
The controller gives buffering time, nudges play up to three times, and then
records a retryable failure. Check the network and the browser window. The
lesson is retried on the next run.

**A quiz stays open.**
Nothing is submitted automatically. Submit it in the browser. If you leave it
open past `ASSESSMENT_WAIT_TIMEOUT`, the lesson remains pending and is retried
according to `MAX_RETRIES`.

**I want to see detailed logs.**
Run with `--no-dashboard`, set `LOG_LEVEL=DEBUG`, and inspect
`logs/automation.log`. Event records are also stored in SQLite.

---

## Limitations

- LinkedIn Learning can change its DOM at any time; selector overrides are the
  maintenance point.
- The answer assistant is a transparent heuristic, not a knowledge model. It
  will sometimes produce no confident suggestion, and you should verify every
  answer in the lesson.
- The tool cannot bypass locked content, DRM, or account restrictions, and it
  will not try to.
- Progress is accepted only when the platform's own curriculum shows
  completion. If LinkedIn does not register a play for any reason, the lesson
  stays pending.
- Non-headless mode is recommended because manual login and player interactions
  may be required.
- Assessments must be submitted by you; the automation deliberately has no
  code path that selects or submits an answer.
