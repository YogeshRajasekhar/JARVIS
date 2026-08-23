# Complete Beginner Walkthrough — GitHub Setup Through Running the Program

Follow this top to bottom. Don't skip ahead — each phase needs the previous one working.

---

## Phase 1: One-time setup (git + GitHub account)

**1. Check if git is already installed.** Open a terminal (Mac: Terminal app; Windows: search
"Command Prompt" or better, install "Git Bash" in the next step; Linux: your terminal) and run:
```bash
git --version
```
If you see a version number, skip to step 3. If you see "command not found," install it:
- **Windows:** download and run the installer from https://git-scm.com/download/win — this
  also installs "Git Bash," which is the terminal you should use for all the commands below.
- **Mac:** run `git --version` — macOS will usually offer to install it for you automatically.
  If not, install via https://git-scm.com/download/mac.
- **Linux:** `sudo apt install git` (Ubuntu/Debian) or `sudo dnf install git` (Fedora).

**2. Create a GitHub account** at https://github.com/join if you don't have one.

**3. Tell git who you are** (one-time, only needs doing once ever on this computer):
```bash
git config --global user.name "Your Name"
git config --global user.email "the-email-you-used-for-github@example.com"
```

---

## Phase 2: Get the code onto your computer, then onto GitHub

**4. Extract the zip file** I gave you (`jarvis-assistant.zip`) into a folder — anywhere you like,
e.g. your Desktop or Documents. You should end up with a folder called `jarvis-assistant`
containing `README.md`, `src/`, `tests/`, etc.

**5. Open a terminal inside that folder.** Easiest way: on Mac, right-click the folder →
"New Terminal at Folder" (or drag the folder into the Terminal app icon). On Windows with Git
Bash, right-click inside the folder → "Git Bash Here." Or just `cd` into it manually:
```bash
cd path/to/jarvis-assistant
```

**6. Turn this folder into a git repository and make your first commit:**
```bash
git init
git add .
git commit -m "Initial commit: guardrail + graph memory modules, tests, build spec"
```

**7. Create a new (empty) repository on GitHub's website:**
- Go to https://github.com/new
- Repository name: `jarvis-assistant`
- Leave it **Private** unless you want it public
- **Do NOT check** "Add a README" or any other initialize option — you already have files,
  and checking these will create a conflict
- Click "Create repository"

**8. Connect your local folder to that GitHub repository and push your code up:**
GitHub will show you a page with commands after creating the repo — use the ones under
"…or push an existing repository from the command line," which will look like this
(replace `YOUR_USERNAME` with your actual GitHub username):
```bash
git remote add origin https://github.com/YOUR_USERNAME/jarvis-assistant.git
git branch -M main
git push -u origin main
```
The first time you push, it'll ask you to log in — a browser window should pop up for you to
authenticate. Once done, **refresh the GitHub page in your browser — you should now see all
your files there.** Confirm this before moving on.

---

## Phase 3: Set up Python on your computer (needed later, to actually run the finished code)

**9. Check Python is installed:**
```bash
python3 --version
```
Need version 3.10 or higher. If missing, install from https://www.python.org/downloads/
(Windows/Mac) or `sudo apt install python3 python3-pip` (Linux).

**10. Create a virtual environment inside the project folder** (keeps this project's packages
separate from everything else on your computer — good practice, not optional):
```bash
cd path/to/jarvis-assistant
python3 -m venv venv
```
Activate it (**you'll need to do this activation step every time you open a new terminal to
work on this project**):
- Mac/Linux: `source venv/bin/activate`
- Windows (Git Bash): `source venv/Scripts/activate`

You'll know it worked because your terminal prompt now shows `(venv)` at the start.

---

## Phase 4: Use Claude Code on the web to build the rest of the agents

**11. Go to https://claude.ai/code** and sign in.

**12. Connect GitHub:** it will prompt you to connect — follow the prompt to install the
"Claude GitHub App" and grant it access. When asked which repositories to grant access to,
select `jarvis-assistant` (you don't need to grant access to your whole account).

**13. Start a new session and select `jarvis-assistant`** as the repository.

**14. Paste the entire contents of `CLAUDE_CODE_PROMPT.md`** (also in your zip) as your task
description, and submit it.

**15. Wait for it to finish.** You can watch it work in real time, or close the tab and come
back later — it keeps running either way. When done, it pushes a new branch to GitHub and
shows you a summary of what it built and the real test output (per the prompt's instructions).

**16. Review what it built.** Read through the diff it shows you. If something looks off or
you don't understand a part, ask it directly in that same session before merging — that's
the "learning it" step, don't skip it.

**17. Merge the branch.** Once you're satisfied, use the merge button in the Claude Code web
interface (or on GitHub directly — go to your repo, "Pull requests" tab, review, and merge).

---

## Phase 5: Get the finished code onto your computer and run it

**18. Pull the newly merged code down to your local folder:**
```bash
cd path/to/jarvis-assistant
git pull origin main
```

**19. Make sure your virtual environment is active** (you'll see `(venv)` in your prompt —
if not, redo the activation command from step 10), then install all dependencies:
```bash
pip install -r requirements.txt
```

**20. Set up your API key.** Copy the example env file and fill in your real key:
```bash
cp .env.example .env
```
Open `.env` in any text editor and paste your Anthropic API key after `ANTHROPIC_API_KEY=`.

**21. Run the full test suite to confirm everything actually works on your machine:**
```bash
pytest tests/ -v
```
You should see all tests pass (green). If anything fails, that's worth pasting back to me or
to Claude Code before moving on — don't ignore a red test.

**22. Run the program:**
```bash
python main.py
```

---

## If you get stuck at any step

Tell me exactly which step number and paste the exact error message you see — not a summary
of it, the actual text — and I'll help you debug it from there.
