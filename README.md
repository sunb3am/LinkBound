# LinkBound: Outbound Intelligence

We built LinkBound because I was tired of manually managing LinkedIn outreach and I refused to use tools that blindly spam people. We needed something that worked *for* us—sending personalized messages reliably, running locally from our own machines so our accounts stayed perfectly safe, and giving us the analytics we actually care about.

So we built it. It started as a scrappy script and has now evolved into a full-scale Liquid Clarity dashboard with AI personalization, collision detection, and zero-bullshit campaign management. We've used it to reach 500+ founders and operators with extremely high response rates because the messages actually sound human.

## What It Actually Does

LinkBound isn't a cloud-hosted spam cannon. It runs locally on your machine, using your own Chrome profile. It navigates LinkedIn exactly like you would, respecting safety limits and business hours.

Here's the workflow:
1. **Load Your Audience:** Drop in a CSV from Apollo or just paste raw LinkedIn URLs.
2. **AI Personalization:** Using Gemini 3.5 Flash (or 3.1 Pro if you bring your own key), LinkBound reads the prospect's profile live. It cleans up messy names and tailors your template to match their headline, company, and background.
3. **Execute:** The engine decides the smartest action. If you're connected, it sends a DM. If not, it sends a connection request. If they require an email, it gracefully skips them or inputs what you have. 
4. **Learn & Analyze:** Every interaction is logged in a local SQLite database (`outbound.db`). It automatically deduplicates contacts across campaigns so you never double-message someone. You can watch the stats update live in the Analytics dashboard.

## New in v2.0

We've completely overhauled the interface to the **Liquid Clarity** design language. It feels incredibly premium, but more importantly, it's functional.

- **Bring Your Own Gemini Key:** The server has a fallback free-tier key, but you can now plug in your own API key directly from the Settings tab. It saves securely in your browser and injects via headers.
- **Audience Manager (CRM):** A unified view of every single person you've ever contacted. You can search, filter, and export them directly to CSV.
- **Batch Tracking:** We no longer just throw messages into the void. Every campaign is tracked as a specific "Batch" so you know exactly how many succeeded, failed, or were skipped.
- **Anti-AI Review:** Before you hit send, you can pass your templates through our Anti-AI reviewer. It aggressively strips out em dashes, "not just X but Y" clichés, and sycophantic praise to keep your voice genuinely yours.

## Getting Started

You only need **Python 3.10+** and **Google Chrome**.

1. Clone or download this repository.
2. Open your terminal in this folder.
3. Run the bootstrap script:
   ```bash
   python bootstrap.py
   ```
   *(On macOS, use `python3 bootstrap.py`)*

The script builds your virtual environment, installs the dependencies (`requirements.txt`), sets up the browser context, and launches the FastAPI server.

4. Go to **http://127.0.0.1:8000** in your browser.
5. In `config.yaml`, add your name under `operators`.
6. On your first run, Chrome will open. Log into LinkedIn, clear any 2FA, and the session will be saved persistently.

## Deploying to Railway

If you want to host this for your team, it's ready for Railway right out of the box.

1. Push this repo to GitHub.
2. Connect the repo in Railway.
3. Railway will automatically detect the `Dockerfile` and `railway.json`.
4. **Crucial Step:** Attach a Volume to `/app/data` in the Railway dashboard. This ensures your SQLite database and persistent browser profiles survive restarts.

## A Note on Safety

LinkedIn is aggressive against automation. We built LinkBound to protect the sender. 
By default, the daily cap is 22 profiles. There are randomized human-like delays (45-90 seconds) between actions. Do not push this to 100+ a day. Quality outreach will always outperform raw volume.

If you have ideas or feedback, I'd genuinely love to hear it. Building this has been a blast, and I hope it helps you start the conversations that actually matter.
