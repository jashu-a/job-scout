"""
Google Drive uploader using OAuth2 (personal account).
Service accounts no longer have storage quota for regular Drive.
This uses a refresh token generated once locally.
"""

import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


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


def _sanitize_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip()[:80]


def upload_to_drive(
    parent_folder_id: str,
    company: str,
    job_id: str,
    resume_path: str,
    cover_letter_path: str = "",
    credentials_path: str = "",  # Kept for backward compat, ignored
) -> dict:
    """
    Upload tailored resume + cover letter to Google Drive.
    Creates a subfolder under parent_folder_id named "<Company>_<JobID>".
    """
    try:
        creds = _get_credentials()
        service = build("drive", "v3", credentials=creds, cache_discovery=False)

        # Create subfolder
        safe_company = _sanitize_name(company)
        folder_name = f"{safe_company}_{job_id}"

        folder_meta = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_folder_id],
        }
        folder = service.files().create(body=folder_meta, fields="id").execute()
        folder_id = folder["id"]

        uploaded_files = []

        # Upload resume
        if resume_path and Path(resume_path).exists():
            file_name = f"Tailored_Resume_{safe_company}.docx"
            file_meta = {"name": file_name, "parents": [folder_id]}
            media = MediaFileUpload(
                resume_path,
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            f = service.files().create(body=file_meta, media_body=media, fields="id").execute()
            uploaded_files.append(f["id"])
            print(f"       ✅ Uploaded: {file_name}")

        # Upload cover letter
        if cover_letter_path and Path(cover_letter_path).exists():
            file_name = f"Cover_Letter_{safe_company}.docx"
            file_meta = {"name": file_name, "parents": [folder_id]}
            media = MediaFileUpload(
                cover_letter_path,
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            f = service.files().create(body=file_meta, media_body=media, fields="id").execute()
            uploaded_files.append(f["id"])
            print(f"       ✅ Uploaded: {file_name}")

        folder_link = f"https://drive.google.com/drive/folders/{folder_id}"
        return {
            "folder_id": folder_id,
            "folder_link": folder_link,
            "file_ids": uploaded_files,
        }

    except Exception as e:
        print(f"       ⚠️  Drive upload failed: {e}")
        return {"error": str(e)}