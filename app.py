from flask import Flask, request, jsonify, render_template, stream_with_context, Response
import os, json, threading, queue, time, uuid, requests, undetected_chromedriver as uc
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from urllib.parse import urljoin
import re

# ID of the folder in the shared drive where you want to upload
SHARED_FOLDER_ID = "1nwBKcEvBLjbQbw0LuCY940FSCt9nHfH6"

app = Flask(__name__)

# Global queue for streaming progress
message_queue = queue.Queue()

# ─── Service account auth from environment (Render) ───
raw = os.getenv("GOOGLE_DRIVE_CREDENTIALS_JSON")
if not raw:
    raise RuntimeError("Missing env var: GOOGLE_DRIVE_CREDENTIALS_JSON")
info = json.loads(raw)
# Restore PEM newlines
info["private_key"] = info["private_key"].replace('\\n', '\n')

creds = ServiceAccountCredentials.from_json_keyfile_dict(
    info,
    scopes=["https://www.googleapis.com/auth/drive"]
)

# Configure PyDrive2 to use service-account only
from pydrive2.settings import InvalidConfigError
try:
    ga = GoogleAuth()
    ga.settings['client_config_file'] = None
    ga.settings['save_credentials'] = False
    ga.settings['get_refresh_token'] = False
    ga.settings['save_credentials_backend'] = None
    ga.credentials = creds
    drive = GoogleDrive(ga)
except InvalidConfigError:
    ga = GoogleAuth()
    ga.credentials = creds
    drive = GoogleDrive(ga)

print("⚙️ Authenticated as service account:", creds.service_account_email, flush=True)


def download_and_upload(username, count, session_id):
    # Launch headless Chrome
    options = uc.ChromeOptions()
    options.headless = True
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = uc.Chrome(options=options)

    # Load profile
    url = f"https://www.tiktok.com/@{username}"
    message_queue.put((session_id, f"🚀 Navigating to {url}"))
    driver.get(url)
    time.sleep(5)

    # Scroll to load more videos
    for i in range(5):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        message_queue.put((session_id, f"⏳ Scrolling {i+1}/5"))
        time.sleep(2)

    html = driver.page_source
    driver.quit()

    # Parse video links from JSON or anchors
    raw_hrefs = []
    try:
        m = re.search(r'<script id="SIGI_STATE" type="application/json">(.*?)</script>', html, re.S)
        if m:
            state = json.loads(m.group(1))
            vids = state.get('ItemModule', {}).keys()
            raw_hrefs = [f"/@{username}/video/{vid}" for vid in vids]
    except Exception:
        raw_hrefs = []
    if not raw_hrefs:
        soup = BeautifulSoup(html, 'html.parser')
        raw_hrefs = [a['href'] for a in soup.find_all('a', href=True) if '/video/' in a['href']]

    links = list(dict.fromkeys([urljoin("https://www.tiktok.com", href) for href in raw_hrefs]))[:count]
    total = len(links)
    message_queue.put((session_id, f"🔍 Found {total} video links"))
    if total == 0:
        message_queue.put((session_id, "❌ No videos found; check username."))
        message_queue.put((session_id, "✅ Done!"))
        return

    # Download & upload each video
    for idx, link in enumerate(links, start=1):
        message_queue.put((session_id, f"Processing video {idx}/{total}: {link}"))
        try:
            r = requests.post(
                "https://lovetik.com/api/ajax/search",
                data={"query": link},
                headers={"x-requested-with": "XMLHttpRequest"},
                timeout=15
            )
            data = r.json()
            message_queue.put((session_id, f"ℹ️ API returned {len(data.get('links', []))} link entries"))

            # Prefer HD, else fallback
            hd_links = [item.get('a') for item in data.get('links', [])
                        if item.get('a') and ("HD Original" in item.get('t','') or '1080' in item.get('s',''))]
            if hd_links:
                best_url = hd_links[0]
            else:
                fallback_links = [item.get('a') for item in data.get('links', []) if item.get('a')]
                if fallback_links:
                    best_url = fallback_links[0]
                    message_queue.put((session_id, "⚠️ No HD link; using fallback resolution"))
                else:
                    best_url = None

            message_queue.put((session_id, f"ℹ️ Selected URL: {best_url}"))
            if not best_url:
                raise RuntimeError("No download URL could be selected")

            filename = f"{username}_{idx}.mp4"
            message_queue.put((session_id, f"⬇️ Downloading to {filename}"))

            # Fetch binary with proper headers (cookies/UA) for Lovetik
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            response = requests.get(best_url, headers=headers, timeout=30, allow_redirects=True)
            if response.status_code != 200 or not response.content:
                raise RuntimeError(f"Download failed, status {response.status_code}")
            content = response.content

            with open(filename, 'wb') as f:
                f.write(content)

            # Upload
            file_drive = drive.CreateFile({
                'title': filename,
                'parents': [{'id': SHARED_FOLDER_ID}]
            })
            file_drive.SetContentFile(filename)
            file_drive.Upload(param={'supportsTeamDrives': True})
            os.remove(filename)
            message_queue.put((session_id, f"✅ Uploaded {filename}"))

        except Exception as e:
            message_queue.put((session_id, f"❌ Failed on video {idx}: {e}"))

    message_queue.put((session_id, "✅ Done!"))


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/start', methods=['POST'])
def start():
    session_id = str(uuid.uuid4())
    username = request.form['username']
    count = int(request.form['count'])
    threading.Thread(target=download_and_upload, args=(username, count, session_id), daemon=True).start()
    return jsonify({"session_id": session_id})


@app.route('/progress/<session_id>')
def stream(session_id):
    def event_stream():
        while True:
            try:
                sid, msg = message_queue.get(timeout=60)
                if sid == session_id:
                    yield f"data: {msg}\n\n"
            except queue.Empty:
                break
    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')


if __name__ == '__main__':
    app.run(debug=True)