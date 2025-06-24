import os, json, threading, time
import requests
from flask import Flask, request, jsonify

# Selenium imports for headless Chrome
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# Google Drive API imports
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Initialize Flask app
app = Flask(__name__)

# Configure Chrome options for headless browser in Docker
chrome_options = Options()
chrome_options.add_argument('--headless')  # run Chrome in headless mode (no GUI)
chrome_options.add_argument('--no-sandbox')  # required when running as root in container:contentReference[oaicite:4]{index=4}
chrome_options.add_argument('--disable-setuid-sandbox')  # disable Linux sandbox security mode
chrome_options.add_argument('--disable-dev-shm-usage')  # use /tmp for shared memory to avoid /dev/shm issues:contentReference[oaicite:5]{index=5}
chrome_options.add_argument('--disable-gpu')  # disable GPU (not needed in headless, but just in case)
chrome_options.add_argument('--disable-extensions')  # disable any Chrome extensions for consistency
chrome_options.add_argument('--ignore-certificate-errors')  # ignore SSL errors
chrome_options.add_argument('--window-size=1920,1080')  # set a large window size to avoid default small size issues:contentReference[oaicite:6]{index=6}

# Load Google Drive service account credentials from environment
SCOPES = ['https://www.googleapis.com/auth/drive']
service_account_info = None
service_account_key = os.environ.get('SERVICE_ACCOUNT_KEY')  # JSON key string for service account
if service_account_key:
    try:
        service_account_info = json.loads(service_account_key)
    except Exception as e:
        print(f"Error parsing service account JSON: {e}", flush=True)
else:
    print("ERROR: Service account key not found in environment variables.", flush=True)

drive_service = None
if service_account_info:
    try:
        creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
        drive_service = build('drive', 'v3', credentials=creds)
        print("Google Drive API client initialized with service account credentials.", flush=True)
    except Exception as e:
        print(f"Error initializing Google Drive service: {e}", flush=True)

# Target folder ID for uploads (e.g., a folder in a Shared Drive) from env, if provided
DRIVE_FOLDER_ID = os.environ.get('SHARED_DRIVE_FOLDER_ID')
if DRIVE_FOLDER_ID:
    print(f"Configured target Drive folder: {DRIVE_FOLDER_ID}", flush=True)
else:
    print("No specific Drive folder configured; uploads will go to the service account's Drive (root or default folder).", flush=True)

def get_videos_from_profile(username: str):
    """Scrape a TikTok user profile page to retrieve video post URLs (keeping logic unchanged, just added debug logging)."""
    profile_url = f"https://www.tiktok.com/@{username}"
    print(f"[Main] Gathering videos for user profile: {username} (URL: {profile_url})", flush=True)
    driver = None
    video_links = []
    try:
        driver = webdriver.Chrome(options=chrome_options)
        print(f"[Main] Chrome driver started for profile scraping: {username}", flush=True)
        driver.get(profile_url)
        print(f"[Main] Opened profile page for {username}", flush=True)
        # Scroll the page to load more videos (if any). We scroll multiple times to load content.
        last_height = 0
        scroll_round = 0
        # Attempt up to 5 scroll increments (tune as needed; original logic may vary)
        for _ in range(5):
            scroll_round += 1
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)  # wait for new videos to load
            new_height = driver.execute_script("return document.body.scrollHeight")
            print(f"[Main] Scroll round {scroll_round}, new height: {new_height}", flush=True)
            if new_height == last_height:
                break  # no more content loaded
            last_height = new_height
        # After scrolling, collect video links from the profile page
        elems = driver.find_elements(By.XPATH, "//a[contains(@href, '/video/')]")
        for elem in elems:
            url = elem.get_attribute('href')
            if url:
                video_links.append(url)
        # Remove duplicates while preserving order
        seen = set()
        video_links = [x for x in video_links if not (x in seen or seen.add(x))]
        print(f"[Main] Found {len(video_links)} video URLs for user @{username}.", flush=True)
    except Exception as e:
        print(f"[Main] Error scraping profile @{username}: {e}", flush=True)
    finally:
        if driver:
            driver.quit()
            print(f"[Main] Closed Chrome driver for profile @{username}", flush=True)
    return video_links

def scrape_and_upload(video_url: str):
    """Scrape a single TikTok video page and upload the video to Google Drive. (Original logic preserved; only added debug prints.)"""
    print(f"[Thread] Starting processing for: {video_url}", flush=True)
    driver = None
    try:
        driver = webdriver.Chrome(options=chrome_options)
        print(f"[Thread] Chrome driver launched for {video_url}", flush=True)
        driver.get(video_url)
        print(f"[Thread] Page loaded for video: {video_url}", flush=True)
        # Give the page a moment to ensure video element is loaded (if needed)
        time.sleep(2)
        # Locate the video element on the page and extract its direct URL
        video_elem = driver.find_element(By.TAG_NAME, "video")
        vid_src = video_elem.get_attribute("src")
        print(f"[Thread] Retrieved video source URL: {vid_src}", flush=True)
        # Download the video file to a temporary location
        local_filename = f"video_{int(time.time()*1000)}.mp4"  # unique name based on timestamp
        if vid_src:
            print(f"[Thread] Downloading video to file: {local_filename}", flush=True)
            try:
                # Stream download to avoid loading the entire file in memory
                with requests.get(vid_src, stream=True) as r, open(local_filename, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                print(f"[Thread] Video downloaded successfully: {local_filename}", flush=True)
            except Exception as dl_err:
                print(f"[Thread] Error downloading video from {vid_src}: {dl_err}", flush=True)
                raise  # propagate exception to be handled by caller
        else:
            raise RuntimeError("No video source URL found on page")
        # Upload the downloaded video file to Google Drive
        if drive_service is None:
            raise RuntimeError("Google Drive service not initialized")
        file_metadata = {"name": os.path.basename(local_filename)}
        if DRIVE_FOLDER_ID:
            file_metadata["parents"] = [DRIVE_FOLDER_ID]
        media = MediaFileUpload(local_filename, resumable=False)
        print(f"[Thread] Uploading {local_filename} to Google Drive...", flush=True)
        # Include supportsAllDrives to allow uploading to a Shared Drive:contentReference[oaicite:7]{index=7}
        file = drive_service.files().create(body=file_metadata, media_body=media, supportsAllDrives=True, fields="id").execute()
        file_id = file.get("id")
        print(f"[Thread] Upload successful. File ID on Drive: {file_id}", flush=True)
    finally:
        # Cleanup: quit the browser
        if driver:
            driver.quit()
            print(f"[Thread] Closed Chrome driver for {video_url}", flush=True)

def thread_worker(target, exceptions_list, lock):
    """Wrapper for threading that calls scrape_and_upload and captures exceptions."""
    try:
        scrape_and_upload(target)
    except Exception as e:
        # Record any exception raised during scraping/upload
        error_msg = str(e)
        with lock:
            exceptions_list.append((target, error_msg))
        print(f"[Thread] Exception in thread for {target}: {e}", flush=True)
    # (No re-raise of exception so that thread can terminate gracefully)

@app.route("/start", methods=["POST"])
def start_scrape():
    """Endpoint to start the TikTok scraping process. Expects JSON input with 'username' or 'urls'."""
    print("[Main] /start endpoint called.", flush=True)
    data = None
    try:
        data = request.get_json(force=True)
    except Exception as e:
        print(f"[Main] Error parsing JSON request: {e}", flush=True)
    if not data:
        # If JSON parsing failed or empty payload
        return jsonify({"status": "error", "error": "Invalid or missing JSON in request"}), 400

    # Determine targets to scrape from input
    targets = []
    if 'username' in data and data['username']:
        username = data['username']
        print(f"[Main] Received request to scrape user profile: @{username}", flush=True)
        targets = get_videos_from_profile(username)
        if not targets:
            # No videos found or profile scraping failed
            return jsonify({"status": "error", "error": f"No videos found for user @{username}"}), 404
    elif 'urls' in data and data['urls']:
        # 'urls' can be a list of video URLs or a single URL
        if isinstance(data['urls'], list):
            targets = data['urls']
        else:
            # if a single URL is provided as a string, wrap it in a list
            targets = [data['urls']]
        print(f"[Main] Received request to scrape {len(targets)} video URL(s).", flush=True)
    else:
        return jsonify({"status": "error", "error": "Request JSON must include 'username' or 'urls'"}), 400

    # If there are no targets determined (e.g., user had no videos)
    if not targets:
        return jsonify({"status": "error", "error": "No targets to scrape"}), 400

    # Launch threads to handle each target (video URL)
    exceptions = []
    lock = threading.Lock()
    threads = []
    print(f"[Main] Starting scraping threads for {len(targets)} target(s)...", flush=True)
    for url in targets:
        t = threading.Thread(target=thread_worker, args=(url, exceptions, lock))
        t.daemon = True  # daemon threads will not block program exit
        t.start()
        threads.append(t)
        print(f"[Main] Thread started for {url}", flush=True)
    # Wait for all threads to finish
    for t in threads:
        t.join()
        print(f"[Main] Thread {t.name} joined (completed).", flush=True)

    # Prepare JSON response after processing all targets
    if exceptions:
        # If any threads encountered errors, report them
        if len(exceptions) == len(targets):
            status = "error"
            message = "All requests failed."
        else:
            status = "partial"
            message = "Some videos processed, some failed."
        # Format error details for response
        error_details = [{"target": tgt, "error": err} for (tgt, err) in exceptions]
        response = {"status": status, "message": message, "errors": error_details}
    else:
        # All succeeded
        response = {"status": "success", "message": f"Successfully scraped and uploaded {len(targets)} video(s)."}
    print(f"[Main] Returning response: {response}", flush=True)
    return jsonify(response), 200

# If running this app.py directly (not via Gunicorn), enable this block.
# In production (Render), Gunicorn will be used to serve the app.
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
