# ⚡ Sprintly - AI Sprint Planner

[!\[CI](https://github.com/AimanIqball/sprintly/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR-USERNAME/sprintly/actions/workflows/ci.yml)

A colorful desktop app that turns a project description into phases, tasks and a sprint-wise schedule
with smart developer assignment. Pure Python, standard library only.

## Features

* Enter project name, description, expected timeline and deadline.
* The description is analysed and split into phases and small tasks, each with suggested hours, priority, skill and dependencies.
* Developers and their skills are added once and saved to `\~/.sprintly/team.json`.
* Planner assigns suitable developers and builds a sprint-wise schedule (DFS, BFS, CPM, A\*, Genetic Algorithm).
* Edit, add or delete generated tasks, then rebuild the plan.
* Gantt chart, workload bars and **CSV export**.

## Run from source

Requires Python 3.9+ with tkinter (included in the Windows/macOS installers; on Debian/Ubuntu: `sudo apt install python3-tk`).

```bash
git clone https://github.com/AimanIqball/sprintly.git
cd sprintly
python sprintly.py
```

## Download (no Python needed)

Ready-made apps for Windows, macOS and Linux are attached to every
[Release](https://github.com/YOUR-USERNAME/sprintly/releases).

## How to use

1. **Team** tab: add developers (`backend:1.2, qa`) or click *Load sample team*.
2. **Project** tab: fill in name, description, timeline and deadline, then click *Analyze and generate tasks*.
3. **Tasks** tab: review and edit tasks.
4. Click *Build sprint plan*, then open **Sprint plan** / **Gantt** and use *Export CSV*.

## Develop

```bash
pip install -r requirements-dev.txt
pytest
pyinstaller --onefile --windowed --name Sprintly sprintly.py
```

## Releasing

```bash
git tag v1.0.0
git push origin v1.0.0
```

The `Release` workflow builds the apps and attaches them to a GitHub Release automatically.

## License

MIT - see [LICENSE](LICENSE).

