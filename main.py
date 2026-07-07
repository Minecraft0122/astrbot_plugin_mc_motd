from __future__ import annotations

import asyncio
import html
import json
import math
import re
import sqlite3
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:
    get_astrbot_data_path = None


PLUGIN_NAME = "astrbot_plugin_mc_motd"
COLOR_CODE_RE = re.compile(r"§.")
DISPLAY_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
MINECRAFT_COLOR_CODES = {
    "0": "#000000",
    "1": "#0000aa",
    "2": "#00aa00",
    "3": "#00aaaa",
    "4": "#aa0000",
    "5": "#aa00aa",
    "6": "#ffaa00",
    "7": "#aaaaaa",
    "8": "#555555",
    "9": "#5555ff",
    "a": "#55ff55",
    "b": "#55ffff",
    "c": "#ff5555",
    "d": "#ff55ff",
    "e": "#ffff55",
    "f": "#ffffff",
}
MINECRAFT_NAMED_COLORS = {
    "black": "#000000",
    "dark_blue": "#0000aa",
    "dark_green": "#00aa00",
    "dark_aqua": "#00aaaa",
    "dark_red": "#aa0000",
    "dark_purple": "#aa00aa",
    "gold": "#ffaa00",
    "gray": "#aaaaaa",
    "dark_gray": "#555555",
    "blue": "#5555ff",
    "green": "#55ff55",
    "aqua": "#55ffff",
    "red": "#ff5555",
    "light_purple": "#ff55ff",
    "yellow": "#ffff55",
    "white": "#ffffff",
}


@dataclass
class MinecraftStatus:
    ok: bool
    sampled_at: float
    host: str
    port: int
    online: Optional[int] = None
    max_players: Optional[int] = None
    motd_plain: str = ""
    version_name: str = ""
    protocol: Optional[int] = None
    favicon: Optional[str] = None
    latency_ms: Optional[int] = None
    error: str = ""
    raw_json: Optional[Dict[str, Any]] = None


@dataclass
class ServerTarget:
    scope_id: str
    scope_label: str
    server_name: str
    host: str
    port: int
    configured: bool = True


@dataclass
class RenderCacheEntry:
    created_at: float
    image_url: str


class HistoryStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS servers (
                    scope_id TEXT PRIMARY KEY,
                    scope_label TEXT NOT NULL,
                    server_name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    configured INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            server_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(servers)").fetchall()
            }
            if "configured" not in server_columns:
                conn.execute(
                    "ALTER TABLE servers ADD COLUMN configured INTEGER NOT NULL DEFAULT 1"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_id TEXT NOT NULL DEFAULT '__default__',
                    server_host TEXT NOT NULL DEFAULT '',
                    server_port INTEGER NOT NULL DEFAULT 0,
                    sampled_at REAL NOT NULL,
                    success INTEGER NOT NULL,
                    online INTEGER,
                    max_players INTEGER,
                    motd TEXT,
                    version_name TEXT,
                    latency_ms INTEGER,
                    error TEXT,
                    raw_json TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(samples)").fetchall()
            }
            if "scope_id" not in columns:
                conn.execute(
                    "ALTER TABLE samples ADD COLUMN scope_id TEXT NOT NULL DEFAULT '__default__'"
                )
            if "server_host" not in columns:
                conn.execute(
                    "ALTER TABLE samples ADD COLUMN server_host TEXT NOT NULL DEFAULT ''"
                )
            if "server_port" not in columns:
                conn.execute(
                    "ALTER TABLE samples ADD COLUMN server_port INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_samples_sampled_at ON samples(sampled_at)"
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_samples_scope_server_time
                ON samples(scope_id, server_host, server_port, sampled_at)
                """
            )

    async def get_server(self, scope_id: str) -> Optional[sqlite3.Row]:
        async with self._lock:
            with self._connect() as conn:
                return conn.execute(
                    """
                    SELECT scope_id, scope_label, server_name, host, port
                         , configured
                    FROM servers
                    WHERE scope_id = ?
                    """,
                    (scope_id,),
                ).fetchone()

    async def list_servers(self) -> List[sqlite3.Row]:
        async with self._lock:
            with self._connect() as conn:
                return list(
                    conn.execute(
                        """
                        SELECT scope_id, scope_label, server_name, host, port
                             , configured
                        FROM servers
                        ORDER BY updated_at DESC
                        """
                    )
                )

    async def upsert_server(self, target: ServerTarget) -> None:
        now = time.time()
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO servers (
                        scope_id, scope_label, server_name, host, port,
                        configured, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope_id) DO UPDATE SET
                        scope_label = excluded.scope_label,
                        server_name = excluded.server_name,
                        host = excluded.host,
                        port = excluded.port,
                        configured = excluded.configured,
                        updated_at = excluded.updated_at
                    """,
                    (
                        target.scope_id,
                        target.scope_label,
                        target.server_name,
                        target.host,
                        target.port,
                        1 if target.configured else 0,
                        now,
                        now,
                    ),
                )

    async def delete_server(self, scope_id: str) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM servers WHERE scope_id = ?", (scope_id,))

    async def add_sample(self, scope_id: str, status: MinecraftStatus) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO samples (
                        scope_id, server_host, server_port, sampled_at,
                        success, online, max_players, motd, version_name,
                        latency_ms, error, raw_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scope_id,
                        status.host,
                        status.port,
                        status.sampled_at,
                        1 if status.ok else 0,
                        status.online,
                        status.max_players,
                        status.motd_plain,
                        status.version_name,
                        status.latency_ms,
                        status.error,
                        json.dumps(status.raw_json, ensure_ascii=False)
                        if status.raw_json
                        else None,
                    ),
                )

    async def purge_older_than(self, cutoff_ts: float) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM samples WHERE sampled_at < ?", (cutoff_ts,))

    async def load_history(
        self,
        scope_id: str,
        host: str,
        port: int,
        hours: int,
    ) -> List[sqlite3.Row]:
        cutoff = time.time() - max(1, hours) * 3600
        async with self._lock:
            with self._connect() as conn:
                return list(
                    conn.execute(
                        """
                        SELECT sampled_at, success, online, max_players, latency_ms
                        FROM samples
                        WHERE scope_id = ?
                          AND server_host = ?
                          AND server_port = ?
                          AND sampled_at >= ?
                        ORDER BY sampled_at ASC
                        """,
                        (scope_id, host, port, cutoff),
                    )
                )

    async def clear(self, scope_id: str) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM samples WHERE scope_id = ?", (scope_id,))


def pack_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        part = value & 0x7F
        value >>= 7
        if value:
            out.append(part | 0x80)
        else:
            out.append(part)
            return bytes(out)


def unpack_varint_from(data: bytes, offset: int = 0) -> Tuple[int, int]:
    value = 0
    for i in range(5):
        if offset + i >= len(data):
            raise ValueError("VarInt 数据不完整")
        byte = data[offset + i]
        value |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return value, offset + i + 1
    raise ValueError("VarInt 长度超过 5 字节")


async def read_varint(reader: asyncio.StreamReader) -> int:
    value = 0
    for i in range(5):
        raw = await reader.readexactly(1)
        byte = raw[0]
        value |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return value
    raise ValueError("VarInt 长度超过 5 字节")


def pack_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return pack_varint(len(encoded)) + encoded


def pack_packet(packet_id: int, payload: bytes = b"") -> bytes:
    packet = pack_varint(packet_id) + payload
    return pack_varint(len(packet)) + packet


async def read_packet(reader: asyncio.StreamReader) -> Tuple[int, bytes]:
    length = await read_varint(reader)
    data = await reader.readexactly(length)
    packet_id, offset = unpack_varint_from(data, 0)
    return packet_id, data[offset:]


def parse_string_from(data: bytes, offset: int = 0) -> Tuple[str, int]:
    length, offset = unpack_varint_from(data, offset)
    end = offset + length
    if end > len(data):
        raise ValueError("字符串数据不完整")
    return data[offset:end].decode("utf-8", errors="replace"), end


def component_to_plain(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(component_to_plain(item) for item in value)
    if isinstance(value, dict):
        pieces: List[str] = []
        text = value.get("text")
        if text is not None:
            pieces.append(str(text))
        if "translate" in value and not pieces:
            pieces.append(str(value.get("translate") or ""))
        for item in value.get("with", []) or []:
            pieces.append(component_to_plain(item))
        for item in value.get("extra", []) or []:
            pieces.append(component_to_plain(item))
        return "".join(pieces)
    return str(value)


def clean_motd(value: Any) -> str:
    text = component_to_plain(value)
    text = COLOR_CODE_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip() or "Minecraft Server"


def safe_minecraft_color(value: Any) -> str:
    color = str(value or "").strip().lower()
    if color in MINECRAFT_NAMED_COLORS:
        return MINECRAFT_NAMED_COLORS[color]
    if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return color
    return ""


def style_from_state(state: Dict[str, Any]) -> str:
    styles: List[str] = []
    color = safe_minecraft_color(state.get("color"))
    if color:
        styles.append(f"color:{color}")
    if state.get("bold"):
        styles.append("font-weight:700")
    if state.get("italic"):
        styles.append("font-style:italic")
    decorations = []
    if state.get("underlined"):
        decorations.append("underline")
    if state.get("strikethrough"):
        decorations.append("line-through")
    if decorations:
        styles.append(f"text-decoration:{' '.join(decorations)}")
    return ";".join(styles)


def wrap_styled_text(text: str, state: Dict[str, Any]) -> str:
    if not text:
        return ""
    escaped = html.escape(text, quote=True)
    style = style_from_state(state)
    if not style:
        return escaped
    return f'<span style="{style}">{escaped}</span>'


def legacy_text_to_html(text: str, inherited_state: Optional[Dict[str, Any]] = None) -> str:
    state: Dict[str, Any] = dict(inherited_state or {})
    chunks: List[str] = []
    buffer: List[str] = []
    i = 0

    def flush() -> None:
        if buffer:
            chunks.append(wrap_styled_text("".join(buffer), state))
            buffer.clear()

    while i < len(text):
        char = text[i]
        if char == "§" and i + 1 < len(text):
            code = text[i + 1].lower()
            flush()
            if code in MINECRAFT_COLOR_CODES:
                state = {"color": MINECRAFT_COLOR_CODES[code]}
            elif code == "l":
                state["bold"] = True
            elif code == "o":
                state["italic"] = True
            elif code == "n":
                state["underlined"] = True
            elif code == "m":
                state["strikethrough"] = True
            elif code == "r":
                state = dict(inherited_state or {})
            i += 2
            continue
        buffer.append(char)
        i += 1
    flush()
    return "".join(chunks)


def component_to_html(value: Any, inherited_state: Optional[Dict[str, Any]] = None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return legacy_text_to_html(value, inherited_state)
    if isinstance(value, list):
        return "".join(component_to_html(item, inherited_state) for item in value)
    if isinstance(value, dict):
        state: Dict[str, Any] = dict(inherited_state or {})
        if "color" in value:
            color = safe_minecraft_color(value.get("color"))
            if color:
                state["color"] = color
        for source_key, target_key in (
            ("bold", "bold"),
            ("italic", "italic"),
            ("underlined", "underlined"),
            ("strikethrough", "strikethrough"),
        ):
            if source_key in value and isinstance(value[source_key], bool):
                state[target_key] = value[source_key]

        pieces: List[str] = []
        if "text" in value:
            pieces.append(legacy_text_to_html(str(value.get("text") or ""), state))
        elif "translate" in value:
            pieces.append(wrap_styled_text(str(value.get("translate") or ""), state))
        for item in value.get("with", []) or []:
            pieces.append(component_to_html(item, state))
        for item in value.get("extra", []) or []:
            pieces.append(component_to_html(item, state))
        return "".join(pieces)
    return legacy_text_to_html(str(value), inherited_state)


def motd_to_html(value: Any) -> str:
    rendered = component_to_html(value)
    return rendered.strip() or html.escape("Minecraft Server", quote=True)


def safe_favicon(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if value.startswith("data:image/png;base64,"):
        return value
    return None


def parse_server_address(address: str, default_port: int) -> Tuple[str, int]:
    value = address.strip()
    if not value:
        raise ValueError("服务器地址不能为空")
    if "://" in value or "/" in value or any(ch.isspace() for ch in value):
        raise ValueError("请填写 host[:port]，不要包含协议、路径或空格")

    host = value
    port = default_port
    if value.startswith("["):
        end = value.find("]")
        if end <= 1:
            raise ValueError("IPv6 地址格式应为 [::1]:25565")
        host = value[1:end]
        rest = value[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                raise ValueError("IPv6 地址端口格式应为 [::1]:25565")
            port = int(rest[1:])
    elif value.count(":") == 1:
        host_part, port_part = value.rsplit(":", 1)
        host = host_part
        port = int(port_part)

    host = host.strip()
    if not host or len(host) > 255:
        raise ValueError("服务器地址长度不合法")
    if port < 1 or port > 65535:
        raise ValueError("端口必须在 1-65535 之间")
    return host, port


def group_id_from_scope(scope_id: str) -> str:
    if scope_id.startswith("group:"):
        return scope_id.removeprefix("group:")
    if ":group:" in scope_id:
        return scope_id.split(":group:", 1)[1]
    return ""


def normalize_group_scope_key(value: Any) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    if key.startswith("group:"):
        return key
    group_id = group_id_from_scope(key)
    if group_id:
        return f"group:{group_id}"
    if key.startswith("private:"):
        return key
    return f"group:{key}"


def row_to_target(row: sqlite3.Row, configured: bool = True) -> ServerTarget:
    return ServerTarget(
        scope_id=str(row["scope_id"]),
        scope_label=str(row["scope_label"]),
        server_name=str(row["server_name"]),
        host=str(row["host"]),
        port=int(row["port"]),
        configured=bool(row["configured"]) if "configured" in row.keys() else configured,
    )


async def query_minecraft_status(
    host: str,
    port: int,
    timeout: float,
    protocol_version: int,
) -> MinecraftStatus:
    sampled_at = time.time()
    started = time.perf_counter()
    writer: Optional[asyncio.StreamWriter] = None

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )

        handshake = (
            pack_varint(protocol_version)
            + pack_string(host)
            + struct.pack(">H", port)
            + pack_varint(1)
        )
        writer.write(pack_packet(0, handshake))
        writer.write(pack_packet(0))
        await asyncio.wait_for(writer.drain(), timeout=timeout)

        packet_id, payload = await asyncio.wait_for(read_packet(reader), timeout=timeout)
        if packet_id != 0:
            raise ValueError(f"服务器返回了未知 status 包: {packet_id}")

        response_text, _ = parse_string_from(payload, 0)
        status_json = json.loads(response_text)

        latency_ms: Optional[int] = None
        try:
            ping_started = time.perf_counter()
            writer.write(pack_packet(1, struct.pack(">q", int(time.time() * 1000))))
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            pong_id, _ = await asyncio.wait_for(read_packet(reader), timeout=timeout)
            if pong_id == 1:
                latency_ms = max(0, round((time.perf_counter() - ping_started) * 1000))
        except Exception:
            latency_ms = max(0, round((time.perf_counter() - started) * 1000))

        players = status_json.get("players") or {}
        version = status_json.get("version") or {}
        return MinecraftStatus(
            ok=True,
            sampled_at=sampled_at,
            host=host,
            port=port,
            online=int(players.get("online") or 0),
            max_players=int(players.get("max") or 0),
            motd_plain=clean_motd(status_json.get("description")),
            version_name=str(version.get("name") or ""),
            protocol=int(version["protocol"]) if "protocol" in version else None,
            favicon=safe_favicon(status_json.get("favicon")),
            latency_ms=latency_ms,
            raw_json=status_json,
        )
    except Exception as exc:
        return MinecraftStatus(
            ok=False,
            sampled_at=sampled_at,
            host=host,
            port=port,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def downsample_rows(rows: List[sqlite3.Row], limit: int) -> List[sqlite3.Row]:
    if limit <= 0 or len(rows) <= limit:
        return rows
    step = len(rows) / limit
    picked: List[sqlite3.Row] = []
    for index in range(limit):
        picked.append(rows[math.floor(index * step)])
    if rows[-1] not in picked:
        picked[-1] = rows[-1]
    return picked


def build_chart(
    rows: List[sqlite3.Row],
    current: MinecraftStatus,
    max_points: int,
) -> Dict[str, Any]:
    successful = [row for row in rows if row["success"] and row["online"] is not None]
    sampled = downsample_rows(successful, max(20, max_points))

    online_values = [int(row["online"]) for row in successful]
    peak_online = max(online_values) if online_values else 0
    if current.ok and current.online is not None:
        peak_online = max(peak_online, current.online)

    y_max = max(1, math.ceil(max(peak_online, 1) * 1.2))
    y_mid = max(1, round(y_max / 2))

    plot_left = 38
    plot_right = 742
    plot_top = 18
    plot_bottom = 296
    width = plot_right - plot_left
    height = plot_bottom - plot_top

    line_points = ""
    area_points = ""
    if sampled:
        if len(sampled) == 1:
            xs = [plot_right]
        else:
            first_ts = float(sampled[0]["sampled_at"])
            last_ts = float(sampled[-1]["sampled_at"])
            span = max(last_ts - first_ts, 1)
            xs = [
                plot_left + (float(row["sampled_at"]) - first_ts) / span * width
                for row in sampled
            ]
        points: List[str] = []
        for x, row in zip(xs, sampled):
            online = max(0, int(row["online"]))
            y = plot_bottom - (online / y_max) * height
            points.append(f"{x:.1f},{y:.1f}")
        line_points = " ".join(points)
        area_points = f"{points[0].split(',')[0]},{plot_bottom} {line_points} {points[-1].split(',')[0]},{plot_bottom}"

    return {
        "line_points": line_points,
        "area_points": area_points,
        "y_max": y_max,
        "y_mid": y_mid,
        "peak_online": peak_online,
        "sample_count": len(successful),
    }


def format_ts(ts: float, pattern: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.fromtimestamp(ts, DISPLAY_TZ).strftime(pattern)


class MinecraftMotdPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        if get_astrbot_data_path is not None:
            plugin_data_path = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        else:
            plugin_data_path = Path(__file__).parent / "data"
        self.store = HistoryStore(plugin_data_path / "history.sqlite3")
        self.template = (Path(__file__).parent / "templates" / "status.html").read_text(
            encoding="utf-8"
        )
        self._collector_task: Optional[asyncio.Task[None]] = None
        self._render_cache: Dict[str, RenderCacheEntry] = {}

    async def initialize(self) -> None:
        await self._ensure_collector()

    def _try_start_collector(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._collector_task is None or self._collector_task.done():
            self._collector_task = loop.create_task(self._collector_loop())
        return True

    async def _ensure_collector(self) -> None:
        if not self._try_start_collector():
            self._collector_task = asyncio.create_task(self._collector_loop())

    def _cfg(self, key: str, default: Any) -> Any:
        if self.config is None:
            return default
        getter = getattr(self.config, "get", None)
        if callable(getter):
            value = getter(key, default)
        else:
            value = getattr(self.config, key, default)
        if value is None or value == "":
            return default
        return value

    def _server_name(self) -> str:
        return str(self._cfg("server_name", "Minecraft Server"))

    def _host(self) -> str:
        return str(self._cfg("host", "127.0.0.1"))

    def _port(self) -> int:
        return max(1, min(65535, int(self._cfg("port", 25565))))

    def _protocol_version(self) -> int:
        return max(1, int(self._cfg("protocol_version", 760)))

    def _timeout(self) -> float:
        return max(0.5, float(self._cfg("timeout_seconds", 3.0)))

    def _interval(self) -> int:
        return max(30, int(self._cfg("query_interval_seconds", 300)))

    def _chart_hours(self) -> int:
        return max(1, int(self._cfg("chart_hours", 24)))

    def _retention_days(self) -> int:
        return max(1, int(self._cfg("retention_days", 30)))

    def _max_chart_points(self) -> int:
        return max(20, int(self._cfg("max_chart_points", 180)))

    def _max_parallel_queries(self) -> int:
        return max(1, int(self._cfg("max_parallel_queries", 4)))

    def _render_cache_seconds(self) -> int:
        return max(0, int(self._cfg("render_cache_seconds", 45)))

    def _background_image_url(self) -> str:
        value = str(self._cfg("background_image_url", "https://api.imlazy.ink/img")).strip()
        if not value:
            return ""
        if not (value.startswith("https://") or value.startswith("http://")):
            logger.warning(f"[{PLUGIN_NAME}] background_image_url 必须以 http:// 或 https:// 开头")
            return ""
        if any(ch in value for ch in ['"', "'", "(", ")", "\\", "<", ">"]):
            logger.warning(f"[{PLUGIN_NAME}] background_image_url 包含不安全字符，已忽略")
            return ""
        return value

    def _background_opacity(self) -> float:
        return min(1.0, max(0.0, float(self._cfg("background_opacity", 0.46))))

    def _background_overlay_opacity(self) -> float:
        return min(1.0, max(0.0, float(self._cfg("background_overlay_opacity", 0.54))))

    def _allow_member_set_server(self) -> bool:
        value = self._cfg("allow_member_set_server", False)
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "on"}

    def _enable_setmotd_command(self) -> bool:
        value = self._cfg("enable_setmotd_command", True)
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "on"}

    def _enable_group_whitelist(self) -> bool:
        value = self._cfg("enable_group_whitelist", False)
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "on"}

    def _allow_private_chat(self) -> bool:
        value = self._cfg("allow_private_chat", True)
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "on"}

    def _use_default_server_for_unconfigured_groups(self) -> bool:
        value = self._cfg("use_default_server_for_unconfigured_groups", True)
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "on"}

    def _split_config_list(self, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            raw_items = value
        else:
            raw_items = re.split(r"[\s,，;；]+", str(value))
        return [str(item).strip() for item in raw_items if str(item).strip()]

    def _whitelisted_scopes(self) -> set[str]:
        return {
            normalize_group_scope_key(item)
            for item in self._split_config_list(self._cfg("group_whitelist", ""))
            if normalize_group_scope_key(item)
        }

    def _group_server_config(self) -> Dict[str, ServerTarget]:
        raw = self._cfg("group_servers_json", "{}")
        if isinstance(raw, dict):
            data = raw
        else:
            raw_text = str(raw or "").strip()
            if not raw_text:
                return {}
            try:
                data = json.loads(raw_text)
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] group_servers_json 配置解析失败: {exc}")
                return {}
        if not isinstance(data, dict):
            logger.warning(f"[{PLUGIN_NAME}] group_servers_json 必须是 JSON 对象")
            return {}

        targets: Dict[str, ServerTarget] = {}
        for key, value in data.items():
            scope_id = normalize_group_scope_key(key)
            group_id = group_id_from_scope(scope_id)
            if not scope_id or not group_id:
                logger.warning(f"[{PLUGIN_NAME}] 忽略无效群配置键: {key}")
                continue
            try:
                if isinstance(value, str):
                    host, port = parse_server_address(value, self._port())
                    server_name = f"{host}:{port}"
                elif isinstance(value, dict):
                    address = value.get("address")
                    if address:
                        host, port = parse_server_address(str(address), self._port())
                    else:
                        host = str(value.get("host") or "").strip()
                        port = int(value.get("port") or self._port())
                        if not host:
                            raise ValueError("缺少 address 或 host")
                        if port < 1 or port > 65535:
                            raise ValueError("端口必须在 1-65535 之间")
                    server_name = str(
                        value.get("name")
                        or value.get("server_name")
                        or f"{host}:{port}"
                    ).strip()
                else:
                    raise ValueError("配置值必须是字符串或对象")
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 忽略群 {key} 的无效服务器配置: {exc}")
                continue

            targets[scope_id] = ServerTarget(
                scope_id=scope_id,
                scope_label=f"群 {group_id}",
                server_name=server_name,
                host=host,
                port=port,
                configured=True,
            )
        return targets

    def _is_scope_allowed(
        self,
        scope_id: str,
        group_id: str = "",
        is_private: bool = False,
    ) -> bool:
        if is_private:
            return self._allow_private_chat()
        normalized = normalize_group_scope_key(scope_id)
        if normalized in self._group_server_config():
            return True
        if not self._enable_group_whitelist():
            return True
        whitelist = self._whitelisted_scopes()
        return normalized in whitelist or (
            bool(group_id) and normalize_group_scope_key(group_id) in whitelist
        )

    def _scope_from_event(self, event: AstrMessageEvent) -> Tuple[str, str]:
        group_id = event.get_group_id()
        if group_id:
            return f"group:{group_id}", f"群 {group_id}"
        platform = event.get_platform_id() or event.get_platform_name() or "unknown"
        session_id = event.get_session_id() or event.unified_msg_origin
        return f"{platform}:private:{session_id}", "私聊会话"

    def _default_target(self, scope_id: str, scope_label: str) -> ServerTarget:
        return ServerTarget(
            scope_id=scope_id,
            scope_label=scope_label,
            server_name=self._server_name(),
            host=self._host(),
            port=self._port(),
            configured=False,
        )

    async def _target_for_event(self, event: AstrMessageEvent) -> ServerTarget:
        scope_id, scope_label = self._scope_from_event(event)
        group_id = event.get_group_id()
        is_private = not bool(group_id)
        if not self._is_scope_allowed(scope_id, group_id, is_private):
            raise PermissionError("当前群未在 MOTD 白名单中，不能使用查询。")

        configured_target = self._group_server_config().get(scope_id)
        if configured_target is not None:
            return configured_target

        row = await self.store.get_server(scope_id)
        if row is not None:
            return row_to_target(row)
        if not self._use_default_server_for_unconfigured_groups():
            raise LookupError("当前群还没有配置 MOTD 查询地址。")
        target = self._default_target(scope_id, scope_label)
        await self.store.upsert_server(target)
        return target

    async def _collector_loop(self) -> None:
        await asyncio.sleep(1)
        while True:
            try:
                targets = await self._collector_targets()
                if targets:
                    sem = asyncio.Semaphore(self._max_parallel_queries())
                    await asyncio.gather(
                        *[
                            self._sample_target_safely(target, sem)
                            for target in targets
                        ]
                    )
                    cutoff = time.time() - self._retention_days() * 86400
                    await self.store.purge_older_than(cutoff)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"[{PLUGIN_NAME}] collector loop error: {exc}")
            await asyncio.sleep(self._interval())

    async def _collector_targets(self) -> List[ServerTarget]:
        targets: Dict[str, ServerTarget] = {}

        backend_targets = self._group_server_config()
        targets.update(backend_targets)

        rows = await self.store.list_servers()
        for row in rows:
            target = row_to_target(row)
            if target.scope_id in targets:
                continue
            group_id = group_id_from_scope(target.scope_id)
            is_private = not bool(group_id)
            if self._is_scope_allowed(target.scope_id, group_id, is_private):
                targets[target.scope_id] = target

        if self._use_default_server_for_unconfigured_groups():
            for scope_id in self._whitelisted_scopes():
                if scope_id in targets:
                    continue
                group_id = group_id_from_scope(scope_id)
                if group_id:
                    targets[scope_id] = self._default_target(scope_id, f"群 {group_id}")

        return list(targets.values())

    async def _sample_target_safely(
        self,
        target: ServerTarget,
        sem: asyncio.Semaphore,
    ) -> None:
        async with sem:
            status = await self._sample_and_store(target)
            if status.ok:
                logger.info(
                    f"[{PLUGIN_NAME}] {target.scope_label} {status.host}:{status.port} "
                    f"{status.online}/{status.max_players} online"
                )
            else:
                logger.warning(
                    f"[{PLUGIN_NAME}] {target.scope_label} status query failed: "
                    f"{status.error}"
                )

    async def _sample_and_store(self, target: ServerTarget) -> MinecraftStatus:
        status = await query_minecraft_status(
            host=target.host,
            port=target.port,
            timeout=self._timeout(),
            protocol_version=self._protocol_version(),
        )
        await self.store.add_sample(target.scope_id, status)
        cutoff = time.time() - self._retention_days() * 86400
        await self.store.purge_older_than(cutoff)
        return status

    def _safe_text(self, value: Any) -> str:
        return html.escape(str(value or ""), quote=True)

    def _template_data(
        self,
        target: ServerTarget,
        current: MinecraftStatus,
        rows: List[sqlite3.Row],
    ) -> Dict[str, Any]:
        chart = build_chart(rows, current, self._max_chart_points())
        start_ts = time.time() - self._chart_hours() * 3600
        end_ts = time.time()
        current_view = {
            "ok": current.ok,
            "online": current.online,
            "max_players": current.max_players,
            "motd_plain": self._safe_text(current.motd_plain),
            "motd_html": motd_to_html(
                (current.raw_json or {}).get("description")
                if current.raw_json
                else current.motd_plain
            ),
            "favicon": current.favicon,
            "error": self._safe_text(current.error or "服务器未响应"),
            "sampled_at_text": format_ts(current.sampled_at),
        }
        return {
            "server_name": self._safe_text(target.server_name),
            "scope_label": self._safe_text(target.scope_label),
            "host": self._safe_text(current.host),
            "port": current.port,
            "current": current_view,
            "chart_hours": self._chart_hours(),
            "retention_days": self._retention_days(),
            "x_start_label": format_ts(start_ts, "%H:%M"),
            "x_q1_label": format_ts(start_ts + (end_ts - start_ts) * 0.25, "%H:%M"),
            "x_mid_label": format_ts(start_ts + (end_ts - start_ts) * 0.5, "%H:%M"),
            "x_q3_label": format_ts(start_ts + (end_ts - start_ts) * 0.75, "%H:%M"),
            "x_end_label": format_ts(end_ts, "%H:%M"),
            "background_image_url": self._safe_text(self._background_image_url()),
            "background_opacity": f"{self._background_opacity():.2f}",
            "background_overlay_opacity": f"{self._background_overlay_opacity():.2f}",
            **chart,
        }

    def _render_cache_key(self, target: ServerTarget) -> str:
        return "|".join(
            [
                target.scope_id,
                target.host,
                str(target.port),
                target.server_name,
                str(self._chart_hours()),
                str(self._max_chart_points()),
                self._background_image_url(),
                f"{self._background_opacity():.2f}",
                f"{self._background_overlay_opacity():.2f}",
            ]
        )

    def _get_cached_render(self, cache_key: str) -> Optional[str]:
        ttl = self._render_cache_seconds()
        if ttl <= 0:
            return None
        entry = self._render_cache.get(cache_key)
        if entry is None:
            return None
        if time.time() - entry.created_at > ttl:
            self._render_cache.pop(cache_key, None)
            return None
        return entry.image_url

    def _set_cached_render(self, cache_key: str, image_url: str) -> None:
        ttl = self._render_cache_seconds()
        if ttl <= 0:
            return
        self._render_cache[cache_key] = RenderCacheEntry(
            created_at=time.time(),
            image_url=image_url,
        )

    def _plain_status(self, target: ServerTarget, current: MinecraftStatus) -> str:
        if current.ok:
            return (
                f"{target.server_name} 当前在线：{current.online}/{current.max_players}\n"
                f"地址：{current.host}:{current.port}\n"
                f"MOTD：{current.motd_plain}"
            )
        return (
            f"{target.server_name} 查询失败\n"
            f"地址：{current.host}:{current.port}\n"
            f"错误：{current.error or '服务器未响应'}"
        )

    @filter.command("motd")
    async def motd(self, event: AstrMessageEvent):
        """查询 Minecraft 服务器 MOTD，并返回在线人数历史图片。"""
        await self._ensure_collector()
        try:
            target = await self._target_for_event(event)
        except PermissionError as exc:
            yield event.plain_result(str(exc))
            return
        except LookupError as exc:
            yield event.plain_result(
                f"{exc}\n请管理员在后台 group_servers_json 中配置，或使用 /setmotd <host[:port]> [名称]。"
            )
            return
        cache_key = self._render_cache_key(target)
        cached_image = self._get_cached_render(cache_key)
        if cached_image:
            yield event.image_result(cached_image)
            return

        current = await self._sample_and_store(target)
        rows = await self.store.load_history(
            target.scope_id,
            target.host,
            target.port,
            self._chart_hours(),
        )
        data = self._template_data(target, current, rows)
        try:
            url = await self.html_render(
                self.template,
                data,
                options={
                    "type": "png",
                    "full_page": False,
                    "clip": {"x": 0, "y": 0, "width": 790, "height": 500},
                    "omit_background": False,
                    "scale": "device",
                },
            )
            self._set_cached_render(cache_key, url)
            yield event.image_result(url)
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] render status image failed: {exc}")
            yield event.plain_result(
                self._plain_status(target, current)
                + f"\n\n图片渲染失败：{type(exc).__name__}: {exc}"
            )

    @filter.command("setmotd")
    async def setmotd(self, event: AstrMessageEvent, address: str, server_name: str = ""):
        """设置当前群/会话的 Minecraft 查询地址。"""
        if not self._enable_setmotd_command():
            yield event.plain_result("当前已关闭群内设置，请管理员在后台配置 MOTD 查询地址。")
            return
        if not self._allow_member_set_server() and not event.is_admin():
            yield event.plain_result("当前配置仅允许管理员修改 Minecraft 查询地址。")
            return
        try:
            host, port = parse_server_address(address, self._port())
        except Exception as exc:
            yield event.plain_result(
                f"地址格式不正确：{exc}\n示例：/setmotd 127.0.0.1:25565"
            )
            return

        scope_id, scope_label = self._scope_from_event(event)
        group_id = event.get_group_id()
        is_private = not bool(group_id)
        if not self._is_scope_allowed(scope_id, group_id, is_private):
            yield event.plain_result("当前群未在 MOTD 白名单中，不能设置查询地址。")
            return
        if scope_id in self._group_server_config():
            yield event.plain_result("当前群已由后台 group_servers_json 配置，请在后台修改。")
            return

        display_name = server_name.strip() or f"{host}:{port}"
        target = ServerTarget(
            scope_id=scope_id,
            scope_label=scope_label,
            server_name=display_name,
            host=host,
            port=port,
            configured=True,
        )
        await self.store.upsert_server(target)
        current = await self._sample_and_store(target)
        result = (
            f"已为{scope_label}绑定 Minecraft 查询地址：{display_name} "
            f"({host}:{port})"
        )
        if current.ok:
            result += f"\n当前在线：{current.online}/{current.max_players}"
        else:
            result += f"\n首次查询失败：{current.error or '服务器未响应'}"
        yield event.plain_result(result)

    @filter.command("clearmotd")
    async def clearmotd(self, event: AstrMessageEvent):
        """清除当前群/会话的 Minecraft 查询地址设置。"""
        if not self._enable_setmotd_command():
            yield event.plain_result("当前已关闭群内设置，请管理员在后台配置 MOTD 查询地址。")
            return
        if not self._allow_member_set_server() and not event.is_admin():
            yield event.plain_result("当前配置仅允许管理员修改 Minecraft 查询地址。")
            return
        scope_id, scope_label = self._scope_from_event(event)
        group_id = event.get_group_id()
        is_private = not bool(group_id)
        if not self._is_scope_allowed(scope_id, group_id, is_private):
            yield event.plain_result("当前群未在 MOTD 白名单中，不能清除查询地址。")
            return
        if scope_id in self._group_server_config():
            yield event.plain_result("当前群由后台 group_servers_json 配置，不能在群内清除。")
            return
        await self.store.delete_server(scope_id)
        yield event.plain_result(
            f"已清除{scope_label}的 Minecraft 查询地址设置。\n"
            f"下次 /motd 会使用插件默认地址：{self._host()}:{self._port()}"
        )

    async def terminate(self):
        if self._collector_task is not None:
            self._collector_task.cancel()
            try:
                await self._collector_task
            except asyncio.CancelledError:
                pass
