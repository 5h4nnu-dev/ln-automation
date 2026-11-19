Project: LinkedIn Learning Automation Tool

Act as a senior Python automation engineer. Build a reliable, modular, reusable automation application that helps users complete LinkedIn Learning courses efficiently.

The application should automate repetitive course navigation and video playback while providing an intelligent assistant for multiple-choice assessments.

The goal is to minimize manual interaction without bypassing LinkedIn Learning's legitimate course-progress tracking or assessment requirements.

1. Technology Stack

Use the following technologies:

- Python 3.12+
- Playwright for browser automation
- Chromium browser
- JavaScript injection for HTML5 video control
- SQLite or JSON for progress persistence
- Rich for terminal UI and logging
- Pydantic for configuration validation
- Pytest for testing

Keep dependencies minimal.

2. Core Features

A. Browser Session Management

Implement a browser manager that:

1. Launches Chromium using Playwright.
2. Supports persistent browser profiles.
3. Allows the user to log into LinkedIn manually.
4. Preserves authentication cookies and sessions.
5. Opens a user-provided LinkedIn Learning course URL.
6. Detects authentication expiration.
7. Pauses execution and requests manual authentication when necessary.

Never request or store LinkedIn passwords or OTPs.

B. Video Playback Automation

Create a dedicated video player controller.

Requirements:

1. Detect HTML5 video elements.
2. Retrieve video duration and current playback position.
3. Allow configurable playback speeds.
4. Support platform-approved playback speeds, up to the maximum offered by the player.
5. Start and pause videos.
6. Detect video completion.
7. Wait for the platform to register legitimate lesson completion.
8. Detect player errors and retry appropriately.
9. Log completed lessons.

Important:

- Do not manipulate completion flags.
- Do not fake video progress.
- Do not mark videos as completed without actually watching them.
- Do not interfere with platform progress-tracking mechanisms.

Implement a configurable playback speed.

Example configuration:

PLAYBACK_SPEED = 2.0

C. Course Navigation

Implement an intelligent course navigator.

Responsibilities:

1. Detect the course curriculum.
2. Identify sections and individual lessons.
3. Identify the currently active lesson.
4. Navigate to the next available lesson.
5. Detect completed lessons.
6. Avoid repeatedly processing completed lessons.
7. Handle unavailable or locked lessons.
8. Maintain a record of course progress.

The navigator should use Playwright locators and DOM inspection rather than relying on fixed screen coordinates.

Do not bypass course restrictions or locked content.

D. Progress Management

Implement persistent progress tracking.

Store:

- Course URL
- Course title
- Total lessons discovered
- Completed lessons
- Pending lessons
- Current lesson
- Playback duration
- Session start time
- Last execution timestamp
- Execution status

The application must support resuming an interrupted session.

Use SQLite for persistent storage.

E. MCQ Assessment Assistant

Create a separate assessment assistant.

It should:

1. Detect when a lesson or course presents a multiple-choice question.
2. Extract the question text.
3. Extract all available answer options.
4. Display the question in the terminal.
5. Analyze the question and available options.
6. Provide a suggested answer with an explanation.
7. Clearly indicate uncertainty when the answer cannot be established confidently.
8. Wait for the user to select and submit their answer.

Example terminal interface:

---

ASSESSMENT DETECTED

Question:
Which protocol is commonly used for secure web communication?

A. HTTP
B. HTTPS
C. FTP
D. SMTP

Suggested answer: B

Explanation:
HTTPS uses TLS to secure HTTP communication.

Confidence: High

Choose your answer manually.

---

The assessment assistant must never automatically submit answers or manipulate assessment results.

Make the question extraction logic reusable across different course layouts.

F. Terminal Interface

Create a clean terminal dashboard using Rich.

Example:

================================================
LINKEDIN LEARNING AUTOMATION

Course: Python Essential Training

[STATUS] Browser initialized
[STATUS] LinkedIn session authenticated
[STATUS] Course curriculum detected

Total Lessons: 32
Completed: 0
Remaining: 32

Current Lesson:
Introduction to Python

Playback Speed: 2.0x

[RUNNING] Video playback
[WAITING] Lesson completion

Progress: 0/32

================================================

Provide the following commands:

1. Start course automation
2. Pause execution
3. Resume execution
4. View course progress
5. Change playback speed
6. Exit application

G. Error Handling

Handle:

- Browser crashes
- Network interruptions
- Authentication expiration
- Missing video elements
- Unexpected page navigation
- Video playback errors
- Course layout changes
- Assessment detection failures

Use structured logging.

Do not allow a single failed lesson to crash the entire application.

3. Project Architecture

Use the following structure:

linkedin-learning-automation/

main.py
config.py
requirements.txt
README.md

browser/
    __init__.py
    session.py
    manager.py

player/
    __init__.py
    video_controller.py

course/
    __init__.py
    curriculum.py
    navigator.py
    progress.py

assessment/
    __init__.py
    detector.py
    extractor.py
    assistant.py

database/
    __init__.py
    models.py
    storage.py

ui/
    __init__.py
    dashboard.py
    terminal.py

utils/
    __init__.py
    logger.py
    exceptions.py

tests/
    test_player.py
    test_navigator.py
    test_progress.py
    test_assessment.py

logs/

4. Configuration

Create a centralized config.py.

Include:

- COURSE_URL
- BROWSER_PROFILE
- HEADLESS
- PLAYBACK_SPEED
- AUTO_NAVIGATE
- AUTO_RESUME
- DATABASE_PATH
- LOG_LEVEL

Allow the user to configure these values without modifying the application logic.

Default to a persistent, non-headless browser session for reliability.

5. Execution Workflow

The complete application should work as follows:

STEP 1:
User launches main.py.

STEP 2:
Application initializes Chromium.

STEP 3:
User authenticates manually if required.

STEP 4:
Application opens the configured course URL.

STEP 5:
Application discovers the course curriculum.

STEP 6:
Application checks previously completed lessons.

STEP 7:
Application starts the current lesson using the configured playback speed.

STEP 8:
Application monitors actual playback and waits for legitimate lesson completion.

STEP 9:
Application navigates to the next available lesson.

STEP 10:
If an MCQ assessment appears, the assessment assistant extracts the question and provides an explanation and suggested answer.

STEP 11:
User reviews and submits their answer.

STEP 12:
Application resumes the course workflow.

STEP 13:
Application continues until all accessible lessons and required assessments have been completed.

STEP 14:
Application displays the final course progress and completion status.

6. Development Requirements

Follow these engineering principles:

- Use asynchronous Playwright wherever appropriate.
- Use modular architecture.
- Avoid monolithic scripts.
- Implement proper type hints.
- Use meaningful exception classes.
- Keep browser interaction separate from business logic.
- Write unit tests.
- Avoid hardcoding LinkedIn DOM selectors throughout the application.
- Centralize selectors for easier maintenance.
- Implement graceful shutdown.
- Persist progress after every completed lesson.
- Never fabricate course completion status.

7. Development Strategy

Do not attempt to implement everything in one giant script.

Develop in phases:

Phase 1: Project structure and configuration.

Phase 2: Persistent browser session and authentication.

Phase 3: Video detection and playback controller.

Phase 4: Course curriculum discovery and navigation.

Phase 5: SQLite progress management and resume functionality.

Phase 6: MCQ detection and assessment assistant.

Phase 7: Rich terminal dashboard.

Phase 8: Integration testing and documentation.

After each phase:

1. Run relevant tests.
2. Fix implementation errors.
3. Verify existing functionality.
4. Avoid unnecessary dependencies.
5. Update README documentation.

8. Final Deliverable

Deliver a fully functional, locally executable Python application.

The application should be reusable across different LinkedIn Learning courses.

Provide:

- Complete source code.
- requirements.txt.
- README with installation instructions.
- Configuration instructions.
- Execution instructions.
- Test suite.
- Example terminal output.

Before declaring the project complete, verify that the browser session, video playback, course navigation, progress persistence, assessment assistance and resume functionality work together.

Do not claim that a feature works unless it has been implemented and tested.

Start by inspecting the current working directory. If it is empty, initialize the project. If an existing project is present, inspect it before modifying anything.
