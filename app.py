from flask import Flask, request, jsonify, render_template, stream_with_context, Response
import os, json, threading, queue, time, uuid, requests, undetected_chromedriver as uc
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from urllib.parse import urljoin

# ID of the folder in the shared drive where you want to upload
SHARED_FOLDER_ID = "1nwBKcEvBLjbQbw0LuCY940FSCt9nHfH6"

app = Flask(__name__)

# Global state
message_queue = queue.Queue()

# ───────────────────────────────────────────────────────────────
# Service account auth from environment (for Render compatibility)
# ───────────────────────────────────────────────────────────────
raw = os.getenv("GOOGLE_DRIVE_CREDENTIALS_JSON")
if not raw:
    raise RuntimeError("Missing env var: GOOGLE_DRIVE_CREDENTIALS_JSON")
info = json.loads(raw)
# Replace literal "\\n" sequences with real newlines for the PEM
info["private_key"] = info["private_key"].replace('\\n', '\n')

scope = ["https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scopes=scope)

# Configure PyDrive2 to use only service-account creds—no client_secrets.json or file writes
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
    # If PyDrive2 tries to load client_secrets, ignore it
    drive = GoogleDrive(GoogleAuth())  # fallback, but creds set manually below

print("⚙️ Authenticated as service account:", creds.service_account_email, flush=True)


def download_and_upload(username, count, session_id):
    options = uc.ChromeOptions()
    options.headless = True
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = uc.Chrome(options=options)

    profile_url = f"https://www.tiktok.com/@{username}"
    driver.get(profile_url)
    time.sleep(5)

    # Scroll to load videos
    for _ in range(5):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)

    html = driver.page_source
    driver.quit()

    # Extract & normalize links
    soup = BeautifulSoup(html, 'html.parser')
    raw_hrefs = [a['href'] for a in soup.find_all('a', href=True) if '/video/' in a['href']]
    normalized = [urljoin("https://www.tiktok.com", href) for href in raw_hrefs]
    links = list(dict.fromkeys(normalized))[:count]

    total = len(links)
    for idx, link in enumerate(links, start=1):
        message_queue.put((session_id, f"Processing video {idx}/{total}: {link}"))

        try:
            r = requests.post(
                "https://lovetik.com/api/ajax/search",
                data={"query": link},
                headers={"x-requested-with": "XMLHttpRequest"}
            )
            data = r.json()
            # Only use entries that have a valid URL under 'a'
            download_links = [
                item['a'] for item in data.get('links', [])
                if 'a' in item and ('HD Original' in item.get('t', '') or '1080' in item.get('s', ''))
            ]
            if not download_links:
                raise RuntimeError("No HD link found")

            best_url = download_links[0]
            filename = f"{username}_{idx}.mp4"
            with open(filename, 'wb') as f:
                f.write(requests.get(best_url, timeout=30).content)

            # Upload to the shared drive folder
            file_drive = drive.CreateFile({
                'title': filename,
                'parents': [{'id': SHARED_FOLDER_ID}]
            })
            file_drive.SetContentFile(filename)
            file_drive.Upload(param={'supportsTeamDrives': True})

            # Cleanup local file
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
    username = request.form['username']
    count = int(request.form['count'])
    session_id = str(uuid.uuid4())
    threading.Thread(
        target=download_and_upload,
        args=(username, count, session_id),
        daemon=True
    ).start()
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