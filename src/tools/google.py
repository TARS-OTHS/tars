"""Google API tools — Gmail, Drive, Calendar.

Direct API calls using OAuth2 tokens from vault.
No auth proxy needed. HITL-gated for sensitive operations.
"""

import json
import logging
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote
from urllib.error import URLError, HTTPError

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

# Lazy-init Google auth (needs vault)
_google_auth = None


def _get_auth(ctx: ToolContext):
    global _google_auth
    if _google_auth is None:
        from src.auth.oauth2 import GoogleAuth
        _google_auth = GoogleAuth(ctx.vault)
    return _google_auth


async def _google_api(ctx: ToolContext, url: str, method: str = "GET",
                      data: dict | None = None) -> dict:
    """Make an authenticated Google API call."""
    auth = _get_auth(ctx)
    headers = await auth.get_headers()
    headers["Content-Type"] = "application/json"

    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        logger.error(f"Google API error: {e.code} {error_body[:200]}")
        return {"error": f"HTTP {e.code}: {error_body[:200]}"}
    except URLError as e:
        return {"error": str(e)}


# === Gmail ===

@tool(name="gmail_search", description="Search Gmail messages", category="google")
async def gmail_search(ctx: ToolContext, query: str, max_results: int = 5) -> str:
    """Search Gmail for messages matching a query."""
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={quote(query)}&maxResults={max_results}"
    result = await _google_api(ctx, url)

    if "error" in result:
        return f"Gmail search failed: {result['error']}"

    messages = result.get("messages", [])
    if not messages:
        return f"No emails found for: {query}"

    # Fetch message details
    lines = [f"Found {len(messages)} emails for '{query}':"]
    for msg in messages[:max_results]:
        detail = await _google_api(ctx, f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date")
        if "error" in detail:
            continue
        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        lines.append(f"  - {headers.get('Subject', '(no subject)')} from {headers.get('From', '?')} ({headers.get('Date', '')})")

    return "\n".join(lines)


@tool(name="gmail_read", description="Read a Gmail message by ID", category="google")
async def gmail_read(ctx: ToolContext, message_id: str) -> str:
    """Read the full content of a Gmail message."""
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}?format=full"
    result = await _google_api(ctx, url)

    if "error" in result:
        return f"Failed to read email: {result['error']}"

    headers = {h["name"]: h["value"] for h in result.get("payload", {}).get("headers", [])}
    snippet = result.get("snippet", "")

    return (
        f"Subject: {headers.get('Subject', '(none)')}\n"
        f"From: {headers.get('From', '?')}\n"
        f"Date: {headers.get('Date', '?')}\n"
        f"To: {headers.get('To', '?')}\n\n"
        f"{snippet}"
    )


@tool(name="send_email", description="Send an email via Gmail", category="google", hitl=True)
async def send_email(ctx: ToolContext, to: str, subject: str, body: str) -> str:
    """Send an email. Requires HITL approval."""
    import base64
    raw_msg = f"To: {to}\nSubject: {subject}\nContent-Type: text/plain; charset=utf-8\n\n{body}"
    encoded = base64.urlsafe_b64encode(raw_msg.encode()).decode()

    result = await _google_api(
        ctx,
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        method="POST",
        data={"raw": encoded},
    )

    if "error" in result:
        return f"Failed to send email: {result['error']}"
    return f"Email sent to {to}: {subject}"


# === Google Calendar ===

@tool(name="calendar_list", description="List upcoming calendar events", category="google")
async def calendar_list(ctx: ToolContext, max_results: int = 10) -> str:
    """List upcoming events from primary calendar."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    url = (
        f"https://www.googleapis.com/calendar/v3/calendars/primary/events"
        f"?timeMin={quote(now)}&maxResults={max_results}&singleEvents=true&orderBy=startTime"
    )
    result = await _google_api(ctx, url)

    if "error" in result:
        return f"Calendar error: {result['error']}"

    events = result.get("items", [])
    if not events:
        return "No upcoming events."

    lines = ["**Upcoming Events:**"]
    for event in events:
        start = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
        summary = event.get("summary", "(no title)")
        lines.append(f"  - {start}: {summary}")

    return "\n".join(lines)


@tool(name="calendar_create", description="Create a calendar event", category="google", hitl=True)
async def calendar_create(
    ctx: ToolContext, summary: str, start: str, end: str,
    description: str = "", location: str = ""
) -> str:
    """Create a new calendar event. Start/end in ISO 8601 format."""
    event = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
    }
    if description:
        event["description"] = description
    if location:
        event["location"] = location

    result = await _google_api(
        ctx,
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        method="POST",
        data=event,
    )

    if "error" in result:
        return f"Failed to create event: {result['error']}"
    return f"Event created: {summary} ({result.get('htmlLink', '')})"


# === Google Drive ===

@tool(name="drive_search", description="Search Google Drive files", category="google")
async def drive_search(ctx: ToolContext, query: str, max_results: int = 10, folder_id: str = "") -> str:
    """Search Drive files by name or content.

    Args:
        query: Search term (matches file name or content)
        max_results: Max results to return
        folder_id: Optional — limit search to this folder ID
    """
    safe_query = query.replace("\\", "\\\\").replace("'", "\\'")
    q_parts = [f"(name contains '{safe_query}' or fullText contains '{safe_query}')"]
    if folder_id:
        q_parts.append(f"'{folder_id}' in parents")
    q = quote(" and ".join(q_parts))
    url = f"https://www.googleapis.com/drive/v3/files?q={q}&pageSize={max_results}&fields=files(id,name,mimeType,modifiedTime,webViewLink)"
    result = await _google_api(ctx, url)

    if "error" in result:
        return f"Drive search failed: {result['error']}"

    files = result.get("files", [])
    if not files:
        return f"No files found for: {query}"

    lines = [f"Found {len(files)} files:"]
    for f in files:
        lines.append(f"  - {f['name']} ({f.get('mimeType', '?')}) — {f.get('webViewLink', '')}")

    return "\n".join(lines)


@tool(name="drive_list_folder", description="List files and subfolders inside a Google Drive folder", category="google")
async def drive_list_folder(ctx: ToolContext, folder_id: str, max_results: int = 50) -> str:
    """List contents of a Drive folder by folder ID.

    Args:
        folder_id: The Drive folder ID (from the URL: drive.google.com/drive/folders/XXXXX)
        max_results: Max files to return (default 50)
    """
    q = quote(f"'{folder_id}' in parents and trashed = false")
    url = (
        f"https://www.googleapis.com/drive/v3/files"
        f"?q={q}&pageSize={max_results}"
        f"&fields=files(id,name,mimeType,modifiedTime,webViewLink,size)"
        f"&orderBy=folder,name"
    )
    result = await _google_api(ctx, url)

    if "error" in result:
        return f"Failed to list folder: {result['error']}"

    files = result.get("files", [])
    if not files:
        return f"Folder {folder_id} is empty or not accessible."

    lines = [f"**{len(files)} items in folder:**"]
    for f in files:
        mime = f.get("mimeType", "")
        icon = "\U0001f4c1" if mime == "application/vnd.google-apps.folder" else "\U0001f4c4"
        size = ""
        if f.get("size"):
            size_mb = int(f["size"]) / (1024 * 1024)
            size = f" ({size_mb:.1f}MB)" if size_mb >= 0.1 else f" ({int(f['size'])}B)"
        lines.append(f"  {icon} {f['name']}{size} — `{f['id']}`")

    return "\n".join(lines)


@tool(name="share_drive_file", description="Share a Drive file", category="google", hitl=True)
async def share_drive_file(ctx: ToolContext, file_id: str, email: str,
                           role: str = "reader") -> str:
    """Share a Google Drive file with someone. Requires HITL approval."""
    result = await _google_api(
        ctx,
        f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
        method="POST",
        data={"type": "user", "role": role, "emailAddress": email},
    )

    if "error" in result:
        return f"Failed to share: {result['error']}"
    return f"Shared file {file_id} with {email} as {role}"


@tool(name="drive_download", description="Download a file from Google Drive by file ID or URL", category="google")
async def drive_download(ctx: ToolContext, file_id_or_url: str, output_dir: str = "/tmp") -> str:
    """Download a Google Drive file to local disk.

    Args:
        file_id_or_url: Drive file ID or share URL (e.g. https://drive.google.com/file/d/xxx/view)
        output_dir: Where to save the file (default: /tmp)
    """
    import re
    import aiohttp

    from src.tools.ingest import validate_file_path
    path_err = validate_file_path(output_dir)
    if path_err:
        return path_err

    # Extract file ID from URL if needed
    file_id = file_id_or_url
    m = re.search(r'/d/([a-zA-Z0-9_-]+)', file_id_or_url)
    if m:
        file_id = m.group(1)
    m = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', file_id_or_url)
    if m:
        file_id = m.group(1)

    # Get file metadata first
    meta = await _google_api(ctx, f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=name,mimeType,size")
    if "error" in meta:
        return f"Failed to get file info: {meta['error']}"

    filename = meta.get("name", f"{file_id}.bin")
    mime = meta.get("mimeType", "")

    # For Google Docs/Sheets/Slides, export instead of download
    export_map = {
        "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
        "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    }
    if mime in export_map:
        export_mime, ext = export_map[mime]
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType={quote(export_mime)}"
        filename = filename.rsplit(".", 1)[0] + ext if "." in filename else filename + ext
    else:
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

    # Download with auth
    auth = _get_auth(ctx)
    headers = await auth.get_headers()

    out_path = Path(output_dir) / filename
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                return f"Download failed: HTTP {resp.status}"
            out_path.write_bytes(await resp.read())

    size_mb = out_path.stat().st_size / (1024 * 1024)
    return f"Downloaded: {out_path} ({size_mb:.1f}MB, {mime})"


@tool(name="drive_create_doc", description="Create or update a Google Doc from a local file or text content", category="google", hitl=True)
async def drive_create_doc(ctx: ToolContext, title: str, content: str = "",
                           file_path: str = "", folder_id: str = "",
                           update_id: str = "") -> str:
    """Create a Google Doc in Drive, or update an existing one.

    Args:
        title: Document title
        content: Markdown/text content (used if file_path is empty)
        file_path: Local .md file to upload (takes priority over content)
        folder_id: Drive folder ID to create in (optional)
        update_id: If set, replaces content of this existing doc instead of creating new
    """
    import io

    # Get content from file if path given
    if file_path:
        from src.tools.ingest import validate_file_path
        path_err = validate_file_path(file_path)
        if path_err:
            return path_err
        p = Path(file_path)
        if not p.exists():
            return f"File not found: {file_path}"
        content = p.read_text(encoding="utf-8")

    if not content:
        return "No content provided (set content or file_path)"

    # Strip YAML frontmatter if present
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2].strip()

    auth = _get_auth(ctx)
    headers = await auth.get_headers()

    if update_id:
        # Update existing doc: clear and replace content
        # First, get current doc to find end index
        doc_url = f"https://docs.googleapis.com/v1/documents/{update_id}"
        req = Request(doc_url, headers={**headers, "Content-Type": "application/json"}, method="GET")
        try:
            with urlopen(req, timeout=30) as resp:
                doc = json.loads(resp.read().decode())
        except (HTTPError, URLError) as e:
            return f"Failed to read existing doc: {e}"

        end_index = doc.get("body", {}).get("content", [{}])[-1].get("endIndex", 1)

        # Build batch update: delete all content then insert new
        requests_body = []
        if end_index > 2:
            requests_body.append({
                "deleteContentRange": {
                    "range": {"startIndex": 1, "endIndex": end_index - 1}
                }
            })
        requests_body.append({
            "insertText": {"location": {"index": 1}, "text": content}
        })

        update_url = f"https://docs.googleapis.com/v1/documents/{update_id}:batchUpdate"
        body = json.dumps({"requests": requests_body}).encode()
        req = Request(update_url, data=body, headers={**headers, "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=30) as resp:
                json.loads(resp.read().decode())
        except (HTTPError, URLError) as e:
            return f"Failed to update doc: {e}"

        return f"Updated doc: https://docs.google.com/document/d/{update_id}/edit"

    else:
        # Create new doc via multipart upload (Drive API)
        import uuid
        boundary = f"boundary_{uuid.uuid4().hex}"

        metadata = {"name": title, "mimeType": "application/vnd.google-apps.document"}
        if folder_id:
            metadata["parents"] = [folder_id]

        # Build multipart body
        body_parts = []
        body_parts.append(f"--{boundary}")
        body_parts.append("Content-Type: application/json; charset=UTF-8")
        body_parts.append("")
        body_parts.append(json.dumps(metadata))
        body_parts.append(f"--{boundary}")
        body_parts.append("Content-Type: text/plain; charset=UTF-8")
        body_parts.append("")
        body_parts.append(content)
        body_parts.append(f"--{boundary}--")

        body_bytes = "\r\n".join(body_parts).encode("utf-8")

        upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink"
        upload_headers = {**headers, "Content-Type": f"multipart/related; boundary={boundary}"}

        req = Request(upload_url, data=body_bytes, headers=upload_headers, method="POST")
        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
        except HTTPError as e:
            error_body = e.read().decode() if e.fp else str(e)
            return f"Failed to create doc: HTTP {e.code}: {error_body[:300]}"
        except URLError as e:
            return f"Failed to create doc: {e}"

        return f"Created: {result.get('name', title)} — {result.get('webViewLink', result.get('id', '?'))}"


@tool(name="drive_upload", description="Upload a local file to Google Drive", category="google", hitl=True)
async def drive_upload(ctx: ToolContext, file_path: str, folder_id: str = "",
                       name: str = "") -> str:
    """Upload a file from local disk to Google Drive.

    Args:
        file_path: Local path to the file to upload
        folder_id: Drive folder ID to upload into (optional, defaults to root)
        name: Override filename (optional, defaults to local filename)
    """
    import mimetypes
    import uuid

    from src.tools.ingest import validate_file_path
    path_err = validate_file_path(file_path)
    if path_err:
        return path_err
    p = Path(file_path)
    if not p.exists():
        return f"File not found: {file_path}"

    filename = name or p.name
    content_type = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    file_bytes = p.read_bytes()

    auth = _get_auth(ctx)
    headers = await auth.get_headers()

    boundary = f"boundary_{uuid.uuid4().hex}"
    metadata = {"name": filename}
    if folder_id:
        metadata["parents"] = [folder_id]

    # Build multipart body
    parts = []
    parts.append(f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}")
    parts.append(f"--{boundary}\r\nContent-Type: {content_type}\r\n\r\n")

    body = parts[0].encode("utf-8") + b"\r\n" + parts[1].encode("utf-8") + file_bytes + f"\r\n--{boundary}--".encode("utf-8")

    upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink"
    req = Request(upload_url, data=body, headers={**headers, "Content-Type": f"multipart/related; boundary={boundary}"}, method="POST")

    try:
        with urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        return f"Upload failed: HTTP {e.code}: {error_body[:300]}"
    except URLError as e:
        return f"Upload failed: {e}"

    size_mb = len(file_bytes) / (1024 * 1024)
    return f"Uploaded: {result.get('name', filename)} ({size_mb:.1f}MB) — {result.get('webViewLink', result.get('id', '?'))}"


@tool(name="drive_upload_url", description="Download a file from a URL and upload it to Google Drive", category="google", hitl=True)
async def drive_upload_url(ctx: ToolContext, url: str, folder_id: str = "",
                           name: str = "") -> str:
    """Download a file from a URL and upload it directly to Google Drive.

    No Bash/shell access needed — handles the download internally.

    Args:
        url: URL to download the file from
        folder_id: Drive folder ID to upload into (optional)
        name: Filename to use in Drive (optional, inferred from URL if not set)
    """
    import mimetypes
    import uuid
    import aiohttp

    from src.tools.ingest import _validate_url
    err, _resolved_ip = _validate_url(url)
    if err:
        return err

    # Download the file
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    return f"Download failed: HTTP {resp.status} from {url}"
                file_bytes = await resp.read()
                content_type = resp.content_type or "application/octet-stream"
                # Infer filename from URL or Content-Disposition
                if not name:
                    cd = resp.headers.get("Content-Disposition", "")
                    if "filename=" in cd:
                        name = cd.split("filename=")[-1].strip('" ')
                    else:
                        name = url.rstrip("/").split("/")[-1].split("?")[0]
                    if not name or name == "":
                        name = "downloaded_file"
    except Exception as e:
        return f"Download failed: {e}"

    # Guess mime type from filename if response didn't provide one
    if content_type == "application/octet-stream":
        guessed = mimetypes.guess_type(name)[0]
        if guessed:
            content_type = guessed

    auth = _get_auth(ctx)
    headers = await auth.get_headers()

    boundary = f"boundary_{uuid.uuid4().hex}"
    metadata = {"name": name}
    if folder_id:
        metadata["parents"] = [folder_id]

    parts = []
    parts.append(f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}")
    parts.append(f"--{boundary}\r\nContent-Type: {content_type}\r\n\r\n")

    body = parts[0].encode("utf-8") + b"\r\n" + parts[1].encode("utf-8") + file_bytes + f"\r\n--{boundary}--".encode("utf-8")

    upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink"
    req = Request(upload_url, data=body, headers={**headers, "Content-Type": f"multipart/related; boundary={boundary}"}, method="POST")

    try:
        with urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        return f"Upload failed: HTTP {e.code}: {error_body[:300]}"
    except URLError as e:
        return f"Upload failed: {e}"

    size_mb = len(file_bytes) / (1024 * 1024)
    return f"Uploaded: {result.get('name', name)} ({size_mb:.1f}MB) — {result.get('webViewLink', result.get('id', '?'))}"


@tool(name="drive_delete", description="Delete a file from Google Drive", category="google", hitl=True)
async def drive_delete(ctx: ToolContext, file_id: str) -> str:
    """Delete a Google Drive file by ID. Requires HITL approval."""
    auth = _get_auth(ctx)
    headers = await auth.get_headers()

    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    req = Request(url, headers=headers, method="DELETE")
    try:
        with urlopen(req, timeout=30) as resp:
            resp.read()
        return f"Deleted file {file_id}"
    except HTTPError as e:
        if e.code == 204:
            return f"Deleted file {file_id}"
        error_body = e.read().decode() if e.fp else str(e)
        return f"Failed to delete: HTTP {e.code}: {error_body[:200]}"
    except URLError as e:
        return f"Failed to delete: {e}"
