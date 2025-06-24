from flask import Flask, request, jsonify, render_template, stream_with_context, Response
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import os
import threading
import queue
import time
import uuid
import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
import json

# Load credentials from environment variable
raw = os.getenv('GOOGLE_DRIVE_CREDENTIALS_JSON')
if raw is None:
    raise RuntimeError("GOOGLE_DRIVE_CREDENTIALS_JSON is not set")
credentials_dict = json.loads(raw)

# ⚠️ Convert the literal “\n” sequences into actual newlines:
credentials_dict['private_key'] = credentials_dict['private_key'].replace('\\n', '\n')

# Now load credentials
credentials = Credentials.from_service_account_info(
    credentials_dict,
    scopes=["https://www.googleapis.com/auth/drive"]
)

# Set up Google Drive API client
drive_service = build('drive', 'v3', credentials=credentials, cache_discovery=False)

app = Flask(__name__)

# Global state
progress = {}
message_queue = queue.Queue()

# Upload a file to a specific folder in a shared drive
def upload_to_drive(filename):
    folder_id = '1nwBKcEvBLjbQbw0LuCY940FSCt9nHfH6'  # Shared folder ID
    file_metadata = {
        'name': filename,
        'parents': [folder_id]
    }
    media = MediaFileUpload(filename, mimetype='video/mp4')
    uploaded = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id',
        supportsAllDrives=True
    ).execute()
    return uploaded.get("id")

def download_and_upload(username, count, session_id):
    options = uc.ChromeOptions()
    options.headless = True
    driver = uc.Chrome(options=options)

    profile_url = f"https://www.tiktok.com/@{username}"
    driver.get(profile_url)
    time.sleep(5)

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
        message_queue.put((session_id, f"Processing video {idx+1}/{total}: {link}"))
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
            upload_to_drive(filename)
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