# RailTwin - Digital Twin Software to Explain Block Scheduling of Railway Maintenance

Backend: Flask (`app.py`) simulates a demo 75 km corridor, scores 15-minute candidate block windows and explains the choice.
Frontend: `static/index.html` - live twin, time-distance chart, cost chart, explanation, glossary and Q&A.

    pip install -r requirements.txt
    python app.py     # open http://127.0.0.1:5000

API: GET /api/network, POST /api/plan {section,duration,work}, POST /api/ask {q}.
Data is synthetic demo data; replace STATIONS and build_trains() with a real timetable.

## New in v2
- **Login & roles** (SQLite): register as planner or controller; demo admin `admin` / `admin123` (change it!). Planners save blocks, controllers approve or reject.
- **Weekly calendar** of saved blocks, with **PDF export** (`/api/pdf`).
- **CSV timetable import** (`id,type,dir,start,speed`; sample at `/api/sample.csv`).
- **Multi-section optimiser** (`/api/optimise`): adjacent sections are never closed together (30 min buffer).
- **"Why not this time?"** (`/api/whynot`): explains any rejected window.
