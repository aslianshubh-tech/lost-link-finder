# LostLink — College Lost & Found System

LostLink is a Python + Flask + SQLite web application for reporting lost and found belongings on a college campus.

## Run in VS Code

Open this folder in VS Code and run:

```bash
python -m pip install -r requirements.txt
python app.py
```

Windows alternative:

```bash
py -m pip install -r requirements.txt
py app.py
```

macOS/Linux alternative:

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

Then open http://127.0.0.1:5000.

## Included features

- Account registration with hashed passwords
- Login, logout, and session protection
- Lost and found reports
- Optional local image uploads
- Search and filters
- Transparent rule-based matching
- Match score explanations
- Dashboard and personal reports
- Resolve your own reports
- Responsive design

## Fresh database

The first run creates `instance/lostlink.sqlite3`. It contains no sample data. Uploaded images go into `uploads/`.

To reset the application, stop the server and delete `instance/lostlink.sqlite3`.

## Matching algorithm

- Category: 20 points
- Item name: 20 points
- Color: 15 points
- Location: 15 points
- Date: 10 points
- Approximate time: 10 points
- Description: 10 points

80–100 is High Match. 60–79 is Possible Match. The application explains why each match received its score.
