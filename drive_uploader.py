"""
Google Drive uploader using OAuth2 (personal account).
Handles:
- Uploading tailored resumes and cover letters (PDF or DOCX)
- Persisting jobs.db between runs (download/upload)
"""

import io
import os
import re
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


def _get_credentials() -> Credentials:
    """Build OAuth2 credentials from environment variables."""
    client_id = os.environ.get("GDRIVE_CLIENT_ID", "")
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN", "")

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError(
            "Missing Google Drive OAuth credentials. "
            "Set GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, and GDRIVE_REFRESH_TOKEN."
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )

    return creds


def _get_service():
    """Build and return Drive API service."""
    creds = _get_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _sanitize_name(name: str) -> str:
    return re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')[:80]


def _find_or_create_folder(service, folder_name: str, parent_id: str) -> str:
    """Find an existing folder by name under parent, or create it."""
    query = (
        f"name = '{folder_name}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents "
        f"and trashed = false"
    )
    results = service.files().list(
        q=query, spaces='drive', fields='files(id, name)', pageSize=1
    ).execute()

    files = results.get('files', [])
    if files:
        return files[0]['id']

    folder_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id],
    }
    folder = service.files().create(body=folder_metadata, fields='id').execute()
    return folder['id']


def _upload_file(service, local_path: str, drive_filename: str, parent_folder_id: str) -> str:
    """Upload a single file to Google Drive. Returns the file ID."""
    file_metadata = {
        'name': drive_filename,
        'parents': [parent_folder_id],
    }

    # Auto-detect mimetype from file extension
    ext = Path(local_path).suffix.lower()
    mimetype_map = {
        '.pdf': 'application/pdf',
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.doc': 'application/msword',
    }
    mimetype = mimetype_map.get(ext, 'application/octet-stream')

    media = MediaFileUpload(local_path, mimetype=mimetype, resumable=True)

    file = service.files().create(
        body=file_metadata, media_body=media, fields='id'
    ).execute()

    print(f"       ✅ Uploaded: {drive_filename}")
    return file['id']


# ═══════════════════════════════════════════════════════════════════════════════
# JOB DOCUMENT UPLOADS
# ═══════════════════════════════════════════════════════════════════════════════

def upload_to_drive(
    parent_folder_id: str,
    company: str,
    job_id: str,
    resume_path: str,
    cover_letter_path: str,
    credentials_path: str = "",
) -> dict:
    """
    Upload tailored resume and cover letter to Google Drive.
    Creates a subfolder named <company>_<job_id> and uploads both files.
    Uses the actual filename from the path (supports PDF and DOCX).
    """
    try:
        service = _get_service()

        safe_company = _sanitize_name(company)
        folder_name = f"{safe_company}_{job_id}"

        folder_id = _find_or_create_folder(service, folder_name, parent_folder_id)

        # Upload using actual filenames from path
        resume_filename = Path(resume_path).name
        resume_file_id = _upload_file(service, resume_path, resume_filename, folder_id)

        cl_filename = Path(cover_letter_path).name
        cl_file_id = _upload_file(service, cover_letter_path, cl_filename, folder_id)

        folder_link = f"https://drive.google.com/drive/folders/{folder_id}"

        return {
            "folder_id": folder_id,
            "folder_name": folder_name,
            "resume_file_id": resume_file_id,
            "cover_letter_file_id": cl_file_id,
            "folder_link": folder_link,
            "error": None,
        }

    except Exception as e:
        return {
            "folder_id": None,
            "folder_name": None,
            "resume_file_id": None,
            "cover_letter_file_id": None,
            "folder_link": None,
            "error": str(e),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE PERSISTENCE (jobs.db on Google Drive)
# ═══════════════════════════════════════════════════════════════════════════════

def _find_db_file(service, folder_id: str, filename: str = "jobs.db") -> str | None:
    """Find the jobs.db file in the Drive folder. Returns file ID or None."""
    query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id, name, modifiedTime)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    return None


def download_db(folder_id: str, local_path: str = "jobs.db", remote_name: str = "jobs.db") -> bool:
    """Download jobs.db from Google Drive to local path. Returns True if found."""
    try:
        service = _get_service()
        file_id = _find_db_file(service, folder_id, filename=remote_name)

        if not file_id:
            print(f"  📦 No {remote_name} found in Drive — starting fresh")
            return False

        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)

        done = False
        while not done:
            _, done = downloader.next_chunk()

        with open(local_path, "wb") as f:
            f.write(fh.getvalue())

        size = Path(local_path).stat().st_size
        print(f"  📦 Downloaded {remote_name} from Drive ({size:,} bytes)")
        return True

    except Exception as e:
        print(f"  ⚠️  Failed to download {remote_name} from Drive: {e}")
        return False


def upload_db(folder_id: str, local_path: str = "jobs.db", remote_name: str = "jobs.db") -> bool:
    """Upload jobs.db to Google Drive (creates or overwrites)."""
    try:
        if not Path(local_path).exists():
            print(f"  ⚠️  No local {local_path} to upload")
            return False

        service = _get_service()
        file_id = _find_db_file(service, folder_id, filename=remote_name)

        media = MediaFileUpload(local_path, mimetype="application/x-sqlite3")

        if file_id:
            service.files().update(fileId=file_id, media_body=media).execute()
            action = "Updated"
        else:
            file_meta = {"name": remote_name, "parents": [folder_id]}
            service.files().create(body=file_meta, media_body=media, fields="id").execute()
            action = "Created"

        size = Path(local_path).stat().st_size
        print(f"  📦 {action} {remote_name} in Drive ({size:,} bytes)")
        return True

    except Exception as e:
        print(f"  ⚠️  Failed to upload {remote_name} to Drive: {e}")
        return False