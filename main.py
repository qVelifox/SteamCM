from __future__ import annotations

import atexit
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import winreg
except ImportError:
    winreg = None


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"
YELLOW = "\033[33m"

APP_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)

DATA_DIR = APP_ROOT / "data"
TEMP_DIR = DATA_DIR / ".temp"
LIBRARY_FILE = DATA_DIR / "library.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class GameInfo:
    id: int
    name: str
    installed_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "GameInfo":
        return cls(
            id=int(payload["id"]),
            name=str(payload["name"]),
            installed_files=[str(item) for item in payload.get("installed_files", [])],
        )


def enable_windows_ansi() -> None:
    if os.name == "nt":
        os.system("")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        subprocess.run(
            ["attrib", "+H", str(TEMP_DIR)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def cleanup_temp() -> None:
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR, ignore_errors=True)


def cleanup_data() -> None:
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR, ignore_errors=True)


atexit.register(cleanup_temp)
atexit.register(cleanup_data)


class HttpClient:
    def __init__(self) -> None:
        self._ssl_context = ssl.create_default_context()

    def request(self, url: str, method: str = "GET", timeout: int = 30):
        req = Request(
            url=url,
            method=method,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://store.steampowered.com/",
            },
        )
        return urlopen(req, timeout=timeout, context=self._ssl_context)

    def get_json(self, url: str, timeout: int = 30):
        with self.request(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    def head_ok(self, url: str, timeout: int = 15) -> bool:
        try:
            with self.request(url, method="HEAD", timeout=timeout) as response:
                return 200 <= getattr(response, "status", 200) < 300
        except (HTTPError, URLError):
            return False


class SteamApi:
    def __init__(self) -> None:
        self.http = HttpClient()
        self.search_cache: dict[str, list[GameInfo]] = {}
        self.brief_cache: dict[int, GameInfo] = {}
        self.availability_cache: dict[str, bool] = {}

    @staticmethod
    def build_game_info(app_id: int, name: str) -> GameInfo:
        return GameInfo(id=app_id, name=name)

    def _local_matches(self, cache_key: str) -> list[GameInfo]:
        return [
            game for game in self.brief_cache.values()
            if cache_key in game.name.lower() or cache_key in str(game.id)
        ]

    def search_games(self, query: str) -> list[GameInfo]:
        query = query.strip()

        if not query:
            return []

        cache_key = query.lower()

        if cache_key in self.search_cache:
            return [GameInfo.from_dict(g.to_dict()) for g in self.search_cache[cache_key]]

        local = self._local_matches(cache_key)

        if query.isdigit():
            app_id = int(query)

            if app_id in self.brief_cache:
                result = [self.brief_cache[app_id]]
                self.search_cache[cache_key] = result
                return [GameInfo.from_dict(g.to_dict()) for g in result]

            try:
                payload = self.http.get_json(
                    f"https://store.steampowered.com/api/appdetails?appids={app_id}"
                )
                data = (payload.get(str(app_id), {}).get("data") or {})
                name = data.get("name")
                if name:
                    game = self.build_game_info(app_id, name)
                    self.brief_cache[app_id] = game
                    self.search_cache[cache_key] = [game]
                    return [GameInfo.from_dict(game.to_dict())]
            except Exception:
                pass

            if local:
                self.search_cache[cache_key] = local
                return [GameInfo.from_dict(g.to_dict()) for g in local]

            return []

        if len(local) >= 5:
            local.sort(key=lambda g: g.name.lower())
            self.search_cache[cache_key] = local
            return [GameInfo.from_dict(g.to_dict()) for g in local]

        try:
            encoded = quote_plus(query)
            payload = self.http.get_json(
                f"https://store.steampowered.com/search/results/"
                f"?term={encoded}&json=1&cc=US&l=english"
            )

            games: list[GameInfo] = []

            for item in payload.get("items", []):
                logo = item.get("logo", "")
                parts = logo.split("/apps/")
                if len(parts) < 2:
                    continue
                app_id_raw = parts[1].split("/", 1)[0]
                if not app_id_raw.isdigit():
                    continue
                game = self.build_game_info(int(app_id_raw), item.get("name", "Unknown"))
                self.brief_cache[game.id] = game
                games.append(game)

            seen_ids = {g.id for g in games}
            for g in local:
                if g.id not in seen_ids:
                    games.append(g)
                    seen_ids.add(g.id)

            self.search_cache[cache_key] = games
            return [GameInfo.from_dict(g.to_dict()) for g in games]

        except Exception:
            if local:
                self.search_cache[cache_key] = local
                return [GameInfo.from_dict(g.to_dict()) for g in local]
            return []

    def get_game_brief(self, app_id: int) -> GameInfo:
        if app_id in self.brief_cache:
            return GameInfo.from_dict(self.brief_cache[app_id].to_dict())

        payload = self.http.get_json(
            f"https://store.steampowered.com/api/appdetails?appids={app_id}"
        )
        data = (payload.get(str(app_id), {}).get("data") or {})
        name = data.get("name") or f"App {app_id}"
        game = self.build_game_info(app_id, name)
        self.brief_cache[app_id] = game
        return GameInfo.from_dict(game.to_dict())

    def check_game_availability(self, game_id: int | str) -> bool:
        key = str(game_id)

        if key in self.availability_cache:
            return self.availability_cache[key]

        urls = [
            f"https://api.luagen.revobd.club/{key}.zip",
            f"https://codeload.github.com/SteamAutoCracks/ManifestHub/zip/refs/heads/{key}",
        ]

        available = any(self.http.head_ok(url) for url in urls)
        self.availability_cache[key] = available
        return available


class SteamPaths:
    def detect_steam_path(self) -> Path | None:
        if winreg is not None:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                    raw_path, _ = winreg.QueryValueEx(key, "SteamPath")
                    path = Path(str(raw_path).replace("/", "\\"))
                    if path.exists():
                        return path
            except OSError:
                pass

        for candidate in (
            Path(r"C:\Program Files (x86)\Steam"),
            Path(r"C:\Program Files\Steam"),
        ):
            if candidate.exists():
                return candidate

        return None


class LibraryStore:
    def load(self) -> list[GameInfo]:
        if not LIBRARY_FILE.exists():
            return []

        try:
            payload = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

        games: list[GameInfo] = []
        for item in payload:
            try:
                games.append(GameInfo.from_dict(item))
            except Exception:
                continue

        return games

    def save(self, games: list[GameInfo]) -> None:
        LIBRARY_FILE.write_text(
            json.dumps([game.to_dict() for game in games], indent=2),
            encoding="utf-8",
        )

    def upsert(self, game: GameInfo) -> None:
        games = self.load()
        for index, current in enumerate(games):
            if current.id == game.id:
                games[index] = game
                self.save(games)
                return
        games.append(game)
        self.save(games)

    def remove(self, game_id: int) -> None:
        self.save([game for game in self.load() if game.id != game_id])


class Downloader:
    def __init__(self) -> None:
        self.http = HttpClient()

    def resolve_game_download_url(self, game_id: int | str) -> str:
        game_id = str(game_id)

        direct = f"https://api.luagen.revobd.club/{game_id}.zip"
        github = f"https://codeload.github.com/SteamAutoCracks/ManifestHub/zip/refs/heads/{game_id}"
        kernel = f"https://kernelos.org/games/download.php?gen=depotool&id={game_id}"

        if self.http.head_ok(direct):
            return direct

        if self.http.head_ok(github):
            return github

        try:
            payload = self.http.get_json(kernel)
            relative = payload.get("url")
            if relative:
                return f"https://kernelos.org{relative}"
        except Exception:
            pass

        raise RuntimeError("No download source available for this game.")

    def download_game_package(self, game_id: int | str, progress_callback) -> Path:
        url = self.resolve_game_download_url(game_id)
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        target_file = TEMP_DIR / f"{game_id}.zip"

        with self.http.request(url) as response:
            total = int(response.headers.get("Content-Length", "0") or 0)
            downloaded = 0
            started = time.time()
            last_tick = 0.0

            with target_file.open("wb") as handle:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break

                    handle.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()

                    if now - last_tick > 0.1 or (total and downloaded >= total):
                        elapsed = max(now - started, 0.001)
                        speed_mib_s = downloaded / elapsed / 1_048_576.0
                        eta_seconds = (
                            int((total - downloaded) / (downloaded / elapsed))
                            if total and downloaded else 0
                        )
                        progress_callback(downloaded, total, speed_mib_s, eta_seconds)
                        last_tick = now

        return target_file


class Installer:
    def __init__(self, api: SteamApi) -> None:
        self.api = api

    def install_manifest_package(self, zip_path: Path, steam_path: Path) -> list[str]:
        plugin_dir = steam_path / "config" / "stplug-in"
        depot_dir = steam_path / "config" / "depotcache"

        plugin_dir.mkdir(parents=True, exist_ok=True)
        depot_dir.mkdir(parents=True, exist_ok=True)

        installed_files: list[str] = []

        with zipfile.ZipFile(zip_path) as archive:
            for name in archive.namelist():
                if name.endswith("/"):
                    continue

                source = Path(name)

                if source.suffix.lower() == ".lua":
                    target = plugin_dir / source.name
                elif source.suffix.lower() == ".manifest":
                    target = depot_dir / source.name
                else:
                    continue

                if target.exists():
                    shutil.copy2(target, target.with_suffix(f"{target.suffix}.bak"))

                with archive.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

                installed_files.append(str(target.relative_to(steam_path)))

        return installed_files

    def remove_game_files(self, game: GameInfo, steam_path: Path) -> None:
        for relative in game.installed_files:
            target = steam_path / relative
            if target.exists():
                target.unlink()

        for folder, pattern in (
            (steam_path / "config" / "stplug-in", f"{game.id}*.lua"),
            (steam_path / "config" / "depotcache", f"{game.id}*.manifest"),
        ):
            if not folder.exists():
                continue
            for path in folder.glob(pattern):
                if path.exists():
                    path.unlink()

    def restart_steam(self, steam_path: Path, flush: bool = False) -> None:
        subprocess.run(
            ["taskkill", "/IM", "steam.exe", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        time.sleep(2)

        steam_exe = steam_path / "steam.exe"
        exe = str(steam_exe) if steam_exe.exists() else "steam"

        args = [exe]
        if flush:
            args.append("-flushconfig")

        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class SteamCMApp:
    def __init__(self) -> None:
        enable_windows_ansi()
        ensure_dirs()

        self.api = SteamApi()
        self.paths = SteamPaths()
        self.library = LibraryStore()
        self.downloader = Downloader()
        self.installer = Installer(self.api)
        self.steam_path = self.paths.detect_steam_path()

    def run(self) -> None:
        if msvcrt is None:
            print("Windows console input is required.")
            return

        if self.steam_path is None:
            print("Steam path not found.")
            return

        selected = 0

        while True:
            self.render_panel(
                "SteamCM - By qVelifox",
                "",
                [
                    self.choice_line("Install game", selected == 0),
                    self.choice_line("Remove game", selected == 1),
                    self.choice_line("Relaunch Steam", selected == 2),
                    "",
                    "Use arrow keys and Enter",
                ],
            )

            key = self.read_key()

            if key == "UP":
                selected = (selected - 1) % 3
            elif key == "DOWN":
                selected = (selected + 1) % 3
            elif key == "ENTER":
                if selected == 0:
                    self.install_flow()
                elif selected == 1:
                    self.remove_flow()
                else:
                    self.installer.restart_steam(self.steam_path, flush=True)
                    self.message_screen(
                        "Relaunch Steam",
                        "Steam is restarting with a fresh download cache.",
                    )

    def install_flow(self) -> None:
        query = ""
        results: list[GameInfo] = []
        selected = 0

        while True:
            lines = [
                f"{DIM}Type a game name or App ID.{RESET}",
                "",
                f"{BOLD}Query{RESET}  {query or DIM + '...' + RESET}",
                "",
            ]

            if query and not results:
                lines.append(f"{YELLOW}No results for '{query}'.{RESET}")

            for index, game in enumerate(results[:8]):
                lines.append(
                    self.choice_line(f"{game.name}  {DIM}[{game.id}]{RESET}", selected == index)
                )

            if not query:
                lines.append(f"{DIM}Start typing to search.{RESET}")

            self.render_panel("Install game", "Esc to go back.", lines)

            key = self.read_key()

            if key == "ESC":
                return

            if key == "BACKSPACE":
                query = query[:-1]
                results = self.api.search_games(query)[:10] if query.strip() else []
                selected = 0
                continue

            if key == "UP" and results:
                selected = (selected - 1) % len(results)
                continue

            if key == "DOWN" and results:
                selected = (selected + 1) % len(results)
                continue

            if key == "ENTER" and results:
                game = results[selected]
                if self.confirm_screen("Install game", f"Install {game.name} [{game.id}]?"):
                    self.install_game(game)
                return

            if len(key) == 1 and key.isprintable():
                query += key
                results = self.api.search_games(query)[:10] if query.strip() else []
                selected = 0

    def install_game(self, game: GameInfo) -> None:
        if not self.api.check_game_availability(game.id):
            self.message_screen("Install failed", "This game is not available on the server.")
            return

        zip_path = self.downloader.download_game_package(game.id, self.render_progress)
        installed_files = self.installer.install_manifest_package(zip_path, self.steam_path)

        installed_game = self.api.get_game_brief(game.id)
        installed_game.installed_files = installed_files

        self.library.upsert(installed_game)
        self.message_screen(
            "Install complete",
            f"{installed_game.name} [{installed_game.id}] installed successfully.",
        )

    def remove_flow(self) -> None:
        query = ""
        selected = 0

        self.render_panel("Remove game", "", [f"{DIM}Scanning installed games...{RESET}"])
        all_results = self.filter_library("")

        while True:
            results = [
                g for g in all_results
                if not query.strip()
                or query.lower() in g.name.lower()
                or query.lower() in str(g.id)
            ]

            if results and selected >= len(results):
                selected = len(results) - 1

            lines = [
                f"{DIM}Filter local library, then press Enter to remove.{RESET}",
                "",
                f"{BOLD}Query{RESET}  {query or DIM + '...' + RESET}",
                "",
            ]

            if query and not results:
                lines.append(f"{YELLOW}No results for '{query}'.{RESET}")

            for index, game in enumerate(results[:8]):
                lines.append(
                    self.choice_line(f"{game.name}  {DIM}[{game.id}]{RESET}", selected == index)
                )

            if not query and not results:
                lines.append(f"{DIM}Library is empty.{RESET}")

            self.render_panel("Remove game", "Esc to go back.", lines)

            key = self.read_key()

            if key == "ESC":
                return

            if key == "BACKSPACE":
                query = query[:-1]
                selected = 0
                continue

            if key == "UP" and results:
                selected = (selected - 1) % len(results)
                continue

            if key == "DOWN" and results:
                selected = (selected + 1) % len(results)
                continue

            if key == "ENTER" and results:
                game = results[selected]
                if self.confirm_screen("Remove game", f"Remove {game.name} [{game.id}]?"):
                    self.installer.remove_game_files(game, self.steam_path)
                    self.library.remove(game.id)
                    self.message_screen(
                        "Remove complete",
                        f"{game.name} [{game.id}] removed successfully.",
                    )
                return

            if len(key) == 1 and key.isprintable():
                query += key
                selected = 0

    def render_progress(
        self,
        downloaded: int,
        total: int,
        speed_mib_s: float,
        eta_seconds: int,
    ) -> None:
        percent = (downloaded / total * 100.0) if total else 0.0
        width = 32
        filled = max(0, min(width, int((percent / 100.0) * width)))
        progress_bar = "[" + ("#" * filled) + ("-" * (width - filled)) + "]"

        self.render_panel(
            "Installing",
            "Downloading manifests...",
            [
                progress_bar,
                "",
                f"Progress : {percent:6.2f}%",
                f"Downloaded: {downloaded / (1024 * 1024):7.2f} MiB",
                f"Total     : {total / (1024 * 1024):7.2f} MiB" if total else "Total     : unknown",
                f"Speed     : {speed_mib_s:5.2f} MiB/s",
                f"ETA       : {eta_seconds:4d}s",
            ],
        )

    def confirm_screen(self, title: str, message: str) -> bool:
        selected = 1

        while True:
            self.render_panel(
                title,
                "Choose and press Enter.",
                [
                    message,
                    "",
                    self.choice_line("No", selected == 0),
                    self.choice_line("Yes", selected == 1),
                ],
            )

            key = self.read_key()

            if key in ("UP", "LEFT"):
                selected = (selected - 1) % 2
            elif key in ("DOWN", "RIGHT"):
                selected = (selected + 1) % 2
            elif key == "ENTER":
                return selected == 1
            elif key == "ESC":
                return False

    def message_screen(self, title: str, message: str) -> None:
        while True:
            self.render_panel(title, "Press Enter to return to menu.", [message])
            key = self.read_key()
            if key in ("ENTER", "ESC"):
                return

    def scan_installed_games(self) -> list[GameInfo]:
        plugin_dir = self.steam_path / "config" / "stplug-in"

        if not plugin_dir.exists():
            return []

        depot_dir = self.steam_path / "config" / "depotcache"
        lib_games: dict[int, GameInfo] = {g.id: g for g in self.library.load()}
        seen: dict[int, GameInfo] = {}

        app_ids: list[int] = []
        for lua_file in plugin_dir.glob("*.lua"):
            stem = lua_file.stem.split("_")[0]
            if stem.isdigit():
                app_id = int(stem)
                if app_id not in seen:
                    app_ids.append(app_id)

        unknown_ids = [
            aid for aid in app_ids
            if aid not in self.api.brief_cache and aid not in lib_games
        ]

        for app_id in unknown_ids:
            try:
                payload = self.api.http.get_json(
                    f"https://store.steampowered.com/api/appdetails?appids={app_id}"
                )
                data = payload.get(str(app_id), {}).get("data") or {}
                name = data.get("name")
                if name:
                    game = GameInfo(id=app_id, name=name)
                    self.api.brief_cache[app_id] = game
                    self.library.upsert(game)
            except Exception:
                pass

        lib_games = {g.id: g for g in self.library.load()}

        for app_id in app_ids:
            cached = self.api.brief_cache.get(app_id)
            if cached:
                name = cached.name
            elif app_id in lib_games:
                name = lib_games[app_id].name
            else:
                name = f"App {app_id}"

            installed: list[str] = []

            for f in plugin_dir.glob(f"{app_id}*.lua"):
                try:
                    installed.append(str(f.relative_to(self.steam_path)))
                except ValueError:
                    pass

            if depot_dir.exists():
                for f in depot_dir.glob(f"{app_id}*.manifest"):
                    try:
                        installed.append(str(f.relative_to(self.steam_path)))
                    except ValueError:
                        pass

            seen[app_id] = GameInfo(id=app_id, name=name, installed_files=installed)

        return sorted(seen.values(), key=lambda g: g.name.lower())

    def filter_library(self, query: str) -> list[GameInfo]:
        games = self.scan_installed_games()

        if not query.strip():
            return games

        lowered = query.lower()
        return [
            game for game in games
            if lowered in game.name.lower() or lowered in str(game.id)
        ]

    def render_panel(self, title: str, subtitle: str, lines: list[str]) -> None:
        width, height = shutil.get_terminal_size((120, 30))
        os.system("cls")
        print(f"{WHITE}{title}{RESET}")
        print()

        if subtitle:
            print(f"{DIM}{subtitle}{RESET}")

        print(f"{DIM}Steam path: {self.steam_path}{RESET}")
        print()

        available_lines = max(8, height - 8)
        rendered = 0

        for line in lines[:available_lines]:
            print(line[:width])
            rendered += 1

        for _ in range(max(0, available_lines - rendered)):
            print()

    @staticmethod
    def choice_line(label: str, active: bool) -> str:
        if active:
            return f"{MAGENTA}> {CYAN}{label}{RESET}"
        return f"  {label}"

    @staticmethod
    def read_key() -> str:
        first = msvcrt.getwch()

        if first in ("\x00", "\xe0"):
            second = msvcrt.getwch()
            return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(second, "")

        if first == "\r":
            return "ENTER"
        if first == "\x1b":
            return "ESC"
        if first == "\x08":
            return "BACKSPACE"
        if first == "\x03":
            raise KeyboardInterrupt

        return first


def main() -> None:
    SteamCMApp().run()


if __name__ == "__main__":
    main()