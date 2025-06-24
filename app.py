from flask import Flask, request, jsonify, render_template, stream_with_context, Response
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
import os
import threading
import queue
import time
import uuid
import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
import json
from oauth2client.service_account import ServiceAccountCredentials  # ← only new import

raw = os.getenv('GOOGLE_DRIVE_CREDENTIALS_JSON')
if raw is None:
    raise RuntimeError("GOOGLE_DRIVE_CREDENTIALS_JSON is not set")
credentials_dict = json.loads(raw)

app = Flask(__name__)

# Global state
progress = {}
message_queue = queue.Queue()

# Setup Google Drive authentication (replaces credentials.json)
ga = GoogleAuth()
ga.credentials = ServiceAccountCredentials.from_json_keyfile_dict(
    credentials_dict,
    scopes=["https://www.googleapis.com/auth/drive"]
)
drive = GoogleDrive(ga)

def download_and_upload(username, count, session_id):
    options = uc.ChromeOptions()
    options.headless = True
    driver = uc.Chrome(options=options)

    profile_url = f"https://www.tiktok.com/@{username}"
    driver.get(profile_url)
    time.sleep(5)

    # Scroll to load videos
    for _ in range(5):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)

    html = driver.page_source
    soup = BeautifulSoup(html, 'html.parser')
    links = ["https://www.tiktok.com" + a['href'] for a in soup.find_all('a', href=True) if '/video/' in a['href']]
    links = list(dict.fromkeys(links))[:count]
    driver.quit()

    total = len(links)
    for idx, link in enumerate(links):
        msg = f"Processing video {idx+1}/{total}: {link}"
        message_queue.put((session_id, msg))

        try:
            r = requests.post("https://lovetik.com/api/ajax/search", data={"query": link}, headers={"x-requested-with": "XMLHttpRequest"})
            data = r.json()
            download_links = [x['a'] for x in data['links'] if 'HD Original' in x['t'] or '1080' in x['s']]
            if not download_links:
                continue

            best_url = download_links[0]
            filename = f"{username}_{idx+1}.mp4"
            with open(filename, 'wb') as f:
                f.write(requests.get(best_url).content)

            file_drive = drive.CreateFile({'title': filename})
            file_drive.SetContentFile(filename)
            file_drive.Upload()
            os.remove(filename)
            message_queue.put((session_id, f"✅ Uploaded {filename} to Drive"))
        except Exception as e:
            message_queue.put((session_id, f"❌ Failed on video {idx+1}: {str(e)}"))

    message_queue.put((session_id, "✅ Done!"))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start', methods=['POST'])
def start():
    username = request.form['username']
    count = int(request.form['count'])
    session_id = str(uuid.uuid4())
    progress[session_id] = []
    thread = threading.Thread(target=download_and_upload, args=(username, count, session_id))
    thread.start()
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
