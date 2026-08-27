"""YouTube MCP Server — main entry point."""

from __future__ import annotations

import hmac
import json
import sys

from fastmcp import FastMCP

from youtube_mcp_server import config, youtube, transcripts

mcp = FastMCP("YouTube MCP Server")


def _error(message: str) -> str:
    return json.dumps({"error": message})


@mcp.tool()
def search_videos(query: str, limit: int = 10) -> str:
    """Search YouTube for videos.

    Args:
        query: Search query (e.g. "how to price a SaaS")
        limit: Maximum number of results (default 10, max 30)
    """
    try:
        limit = min(limit, 30)
        videos = youtube.search_videos(query, limit)
        return json.dumps([v.to_dict() for v in videos], ensure_ascii=False, indent=2)
    except Exception as e:
        return _error(f"Failed to search videos: {e}")


@mcp.tool()
def get_video_info(video_url: str) -> str:
    """Get detailed information about a YouTube video including title, description, stats, tags, and chapters.

    Args:
        video_url: YouTube video URL (e.g. "https://youtube.com/watch?v=xxx")
    """
    try:
        video = youtube.get_video_info(video_url)
        return json.dumps(video.to_dict(), ensure_ascii=False, indent=2)
    except Exception as e:
        return _error(f"Failed to get video info: {e}")


@mcp.tool()
def get_channel_info(channel: str) -> str:
    """Get information about a YouTube channel.

    Args:
        channel: Channel URL or @handle (e.g. "@mkbhd" or "https://youtube.com/@mkbhd")
    """
    try:
        info = youtube.get_channel_info(channel)
        return json.dumps(info.to_dict(), ensure_ascii=False, indent=2)
    except Exception as e:
        return _error(f"Failed to get channel info: {e}")


@mcp.tool()
def get_channel_videos(channel: str, limit: int = 20, sort: str = "date") -> str:
    """Get videos from a YouTube channel.

    Args:
        channel: Channel URL or @handle (e.g. "@mkbhd")
        limit: Maximum number of videos (default 20, max 100)
        sort: Sort order — "date" (newest first) or "popular" (most viewed)
    """
    try:
        limit = min(limit, 100)
        videos = youtube.get_channel_videos(channel, limit, sort)
        return json.dumps([v.to_dict() for v in videos], ensure_ascii=False, indent=2)
    except Exception as e:
        return _error(f"Failed to get channel videos: {e}")


@mcp.tool()
def get_transcript(video_url: str, language: str = "en") -> str:
    """Get the full transcript of a YouTube video with timestamps.

    Args:
        video_url: YouTube video URL
        language: Preferred language code (default "en", tries auto-generated fallback)
    """
    try:
        segments = transcripts.get_transcript(video_url, language)
        return json.dumps([s.to_dict() for s in segments], ensure_ascii=False, indent=2)
    except Exception as e:
        error_str = str(e)
        if "IpBlocked" in error_str or "IP" in error_str:
            return _error("YouTube is temporarily blocking transcript requests from your IP. This happens after too many requests. Try again in a few hours.")
        if "No transcript" in error_str or "TranscriptsDisabled" in error_str:
            return _error("No transcript available for this video. The creator may have disabled subtitles.")
        return _error(f"Failed to get transcript: {e}")


@mcp.tool()
def get_comments(video_url: str, limit: int = 20) -> str:
    """Get comments from a YouTube video.

    Args:
        video_url: YouTube video URL
        limit: Maximum number of comments (default 20, max 100)
    """
    try:
        limit = min(limit, 100)
        comments = youtube.get_comments(video_url, limit)
        return json.dumps([c.to_dict() for c in comments], ensure_ascii=False, indent=2)
    except Exception as e:
        return _error(f"Failed to get comments: {e}")


@mcp.tool()
def search_transcript(video_url: str, query: str, language: str = "en") -> str:
    """Search for specific content within a video's transcript.
    Returns matching passages with timestamps and direct links.

    Args:
        video_url: YouTube video URL
        query: Text to search for in the transcript
        language: Preferred language code (default "en")
    """
    try:
        matches = transcripts.search_transcript(video_url, query, language)
        if not matches:
            return json.dumps({"message": f"No matches found for '{query}' in this video."})
        return json.dumps([m.to_dict() for m in matches], ensure_ascii=False, indent=2)
    except Exception as e:
        error_str = str(e)
        if "IpBlocked" in error_str or "IP" in error_str:
            return _error("YouTube is temporarily blocking transcript requests from your IP. Try again in a few hours.")
        if "No transcript" in error_str or "TranscriptsDisabled" in error_str:
            return _error("No transcript available for this video.")
        return _error(f"Failed to search transcript: {e}")


@mcp.tool()
def search_channel_transcripts(
    channel: str,
    query: str,
    language: str = "en",
    max_videos: int = 10,
) -> str:
    """Search for specific content across all videos of a YouTube channel.
    This is the power feature: find what a creator said about any topic.

    Args:
        channel: Channel URL or @handle (e.g. "@hormozi")
        query: Text to search for across all transcripts
        language: Preferred language code (default "en")
        max_videos: Maximum number of recent videos to search (default 10, max 50)
    """
    try:
        max_videos = min(max_videos, 50)
        videos = youtube.get_channel_videos(channel, max_videos, "date")
        if not videos:
            return _error(f"No videos found for channel '{channel}'. Check the channel handle is correct (e.g. @hormozi).")
        video_list = [{"url": v.url, "title": v.title} for v in videos]
        matches = transcripts.search_channel_transcripts(video_list, query, language, max_videos)
        if not matches:
            return json.dumps({"message": f"No matches found for '{query}' across {len(video_list)} videos. Try different keywords or check if transcripts are available."})
        return json.dumps([m.to_dict() for m in matches], ensure_ascii=False, indent=2)
    except Exception as e:
        error_str = str(e)
        if "IpBlocked" in error_str or "IP" in error_str:
            return _error("YouTube is temporarily blocking transcript requests from your IP. Try again in a few hours.")
        return _error(f"Failed to search channel transcripts: {e}")


class _TokenGate:
    """ASGI middleware that requires a shared secret on every HTTP request.

    The server is reachable at a public URL with no OAuth, so without this any
    caller who learns the URL can use it — including the cookie jar it holds.
    Set MCP_AUTH_TOKEN to turn it on. Unset, the server stays open and says so
    at startup.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        presented = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                presented = value.decode("latin-1").removeprefix("Bearer ").strip()
                break
            if name == b"x-auth-token":
                presented = value.decode("latin-1").strip()
                break

        # compare_digest keeps the check constant-time.
        if not hmac.compare_digest(presented, self.token):
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


def main():
    """Run the MCP server."""
    transport = "stdio"
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            transport = sys.argv[idx + 1]

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    import uvicorn

    host = "0.0.0.0"
    port = 8000
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    print(config.cookie_status(), file=sys.stderr, flush=True)

    app = mcp.http_app(path="/mcp")
    token = config.auth_token()
    if token:
        print("auth: token required", file=sys.stderr, flush=True)
        app = _TokenGate(app, token)
    else:
        print(
            f"auth: OPEN — anyone with the URL can call this server. "
            f"Set {config.TOKEN_ENV} to require a token.",
            file=sys.stderr,
            flush=True,
        )

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
