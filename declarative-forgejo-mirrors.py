#!/usr/bin/env python3
import argparse
import base64
import fnmatch
import getpass
import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from functools import cache
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen

NAME = "declarative-forgejo-mirrors"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / NAME / "config.toml"
EXAMPLE = Path(__file__).resolve().with_name("config.toml.example")
TOKENS = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / NAME / "tokens"
SCANS = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / NAME / "scans.json"
SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
INTERVAL = re.compile(r"0|(?=\d)(\d+h)?(\d+m)?(\d+s)?")
CGIT_REPO = re.compile(r"<td class='(?:sub|top)level-repo'><a [^>]*?href='([^']+)'.*?</td><td><a [^>]*>(.*?)</a>", re.S)
CGIT_LOGO = re.compile(r"<td class='logo'.*?<img src='([^']+)'", re.S)
CGIT_CLONE = re.compile(r"<link rel='vcs-git' href='([^']+)'")
# seconds before giving up on a silent server, migrations clone inside the request so they get longer
TIMEOUT = 60
MIGRATE_TIMEOUT = 30 * 60
TIMED_OUT = b"timed out"

failed = False
# origins that rate limited us, they aren't contacted again this run
limited = set()


class Repo(NamedTuple):
    owner: str
    name: str
    clone_url: str
    description: str
    avatar_url: str
    fork: bool
    cgit: bool = False


class Mirror(NamedTuple):
    origin: str
    path: tuple
    forks: bool
    org: str | None
    name: str | None
    interval: str | None
    exclude: tuple = ()


class SourceError(Exception):
    pass


class RateLimited(SourceError):
    pass


def die(message):
    sys.exit(f"error: {message}")


def fail(message):
    global failed
    failed = True
    print(f"FAIL  {message}", file=sys.stderr)


def warn(message):
    print(f"WARN  {message}", file=sys.stderr)


def error_text(body):
    try:
        return json.loads(body)["message"]
    except (ValueError, KeyError, TypeError):
        return body.decode(errors="replace").strip()[:200]


def send(method, url, body=None, headers=None, timeout=TIMEOUT):
    data = None if body is None else json.dumps(body).encode()
    headers = {"User-Agent": NAME, "Content-Type": "application/json", **(headers or {})}
    try:
        with urlopen(Request(url, data, headers, method=method), timeout=timeout) as response:
            return response.status, response.read(), response.headers
    except HTTPError as error:
        return error.code, error.read(), error.headers
    except URLError as error:
        if isinstance(error.reason, TimeoutError):
            return 0, TIMED_OUT, {}
        return 0, str(error.reason).encode(), {}
    except TimeoutError:
        # read timeouts aren't wrapped in URLError
        return 0, TIMED_OUT, {}


def request(method, url, body=None, headers=None, timeout=TIMEOUT):
    return send(method, url, body, headers, timeout)[:2]


def reset_time(headers):
    if (retry := headers.get("retry-after", "")).isdigit():
        return time.time() + int(retry)
    for key in ("x-ratelimit-reset", "ratelimit-reset"):
        if (value := headers.get(key, "")).isdigit():
            return int(value)
    return None


def api_get(url):
    host = urlsplit(url).netloc
    token = find_token({"api.github.com": "github.com"}.get(host, host))
    status, body, headers = send("GET", url, headers={"Authorization": f"Bearer {token}"} if token else None)
    if status == 429 or status == 403 and (headers.get("x-ratelimit-remaining") == "0" or b"rate limit" in body.lower()):
        reset = reset_time(headers)
        until = f" until {time.strftime('%H:%M', time.localtime(reset))}" if reset else ""
        hint = "" if token else f", a token for {host.removeprefix('api.')} raises the limit"
        raise RateLimited(f"{host} rate limited us{until}{hint}")
    return status, body


def get_json(url):
    status, body = api_get(url)
    if status != 200:
        raise SourceError(f"{url} (HTTP {status}): {error_text(body)}")
    return json.loads(body)


def probe(url):
    status, body = api_get(url)
    if status == 404:
        return None
    if status != 200:
        raise SourceError(f"{url} (HTTP {status}): {error_text(body)}")
    return json.loads(body)


def paged(url):
    page = 1
    while batch := get_json(f"{url}&page={page}"):
        yield from batch
        page += 1


def forge_repos(api, path, avatar_suffix=""):
    if len(path) == 2:
        repos = [get_json(f"{api}/repos/{path[0]}/{path[1]}")]
    elif len(path) == 1:
        repos = paged(f"{api}/users/{path[0]}/repos?per_page=100&limit=50")
    else:
        raise SourceError(f"{'/'.join(path)} is not an owner or repo")
    return [
        Repo(
            repo["owner"]["login"],
            repo["name"],
            repo["clone_url"],
            repo["description"] or "",
            f"{repo['owner']['avatar_url']}{avatar_suffix}",
            repo["fork"],
        )
        for repo in repos
    ]


def github_repos(origin, path):
    return forge_repos("https://api.github.com", path, "&s=512")


def forgejo_repos(origin, path):
    return forge_repos(f"{origin}/api/v1", path)


def cgit_repos(url):
    repos = {}
    while True:
        status, body = request("GET", f"{url}/?ofs={len(repos)}")
        page = body.decode(errors="replace")
        if status != 200 or "content='cgit" not in page:
            break
        count = len(repos)
        logo = CGIT_LOGO.search(page)
        avatar_url = urljoin(f"{url}/", html.unescape(logo[1])) if logo else ""
        for href, description in CGIT_REPO.findall(page):
            clone_url = urljoin(f"{url}/", html.unescape(href)).rstrip("/")
            name = clone_url.rsplit("/", 1)[-1].removesuffix(".git")
            repos[clone_url] = Repo("", name, clone_url, html.unescape(description).strip(), avatar_url, False, True)
        if len(repos) == count:
            break
    return list(repos.values())


def cgit_clone_url(url):
    # cgit's clone-url can point clones at another host, its summary page advertises where
    status, body = request("GET", f"{url}/")
    urls = map(html.unescape, CGIT_CLONE.findall(body.decode(errors="replace"))) if status == 200 else ()
    return next((clone_url for clone_url in urls if clone_url.startswith(("https://", "http://"))), url)


def plain_repos(origin, path):
    url = "/".join((origin, *path))
    for clone_url in (url, f"{url}.git") if path else ():
        if request("GET", f"{clone_url}/info/refs?service=git-upload-pack")[0] == 200:
            return [Repo(path[0] if len(path) == 2 else "", path[-1], clone_url, "", "", False)]
    if repos := cgit_repos(url):
        return repos
    raise SourceError(f"{url} isn't a git repo or cgit index")


@cache
def gitlab_avatar(origin, owner):
    api = f"{origin}/api/v4"
    try:
        account = probe(f"{api}/groups/{owner}") or next(iter(probe(f"{api}/users?username={owner}") or []), {})
    except RateLimited:
        raise
    except SourceError:
        return ""
    url = account.get("avatar_url")
    return urljoin(origin, url).replace("s=80", "s=512") if url else ""


def gitlab_repos(origin, path):
    api = f"{origin}/api/v4"
    full_path = quote("/".join(path), safe="")
    if project := probe(f"{api}/projects/{full_path}"):
        projects = [project]
    elif probe(f"{api}/groups/{full_path}"):
        projects = paged(f"{api}/groups/{full_path}/projects?include_subgroups=true&per_page=100")
    else:
        projects = paged(f"{api}/users/{full_path}/projects?per_page=100")

    repos = []
    for project in projects:
        owner, *name = project["path_with_namespace"].split("/")
        repos.append(Repo(
            owner,
            "-".join(name),
            project["http_url_to_repo"],
            project["description"] or "",
            gitlab_avatar(origin, owner),
            "forked_from_project" in project,
        ))
    return repos


@cache
def detect_source(origin):
    if origin == "https://github.com":
        return github_repos
    if origin == "https://gitlab.com":
        return gitlab_repos
    try:
        if "version" in (probe(f"{origin}/api/v1/version") or {}):
            return forgejo_repos
    except RateLimited:
        raise
    except (SourceError, ValueError):
        pass
    if request("GET", f"{origin}/api/v4/version")[0] in (200, 401, 429):
        return gitlab_repos
    return plain_repos


@cache
def fetch_repos(origin, path, forks):
    source = detect_source(origin)
    if not path and source is not plain_repos:
        raise SourceError(f"{origin} needs an owner or repo in the url")
    if source is gitlab_repos and not forks and not find_token(urlsplit(origin).netloc):
        raise SourceError(f"{origin} doesn't reveal forks without a token, save one or remove forks = false")
    return [repo for repo in source(origin, path) if forks or not repo.fork]


def split_url(url):
    parts = urlsplit(url if "://" in url else f"https://{url}")
    path = parts.path.strip("/").removesuffix(".git")
    path = tuple(path.split("/")) if path else ()
    if parts.scheme not in ("https", "http") or not parts.netloc or not all(map(SEGMENT.fullmatch, path)):
        die(f"bad url: {url}")
    return f"{parts.scheme}://{parts.netloc}", path


class Forgejo:
    def __init__(self, url, token, dry_run, avatars):
        self.url = url
        self.api = f"{url}/api/v1"
        self.token = token
        self.dry_run = dry_run
        self.avatars = avatars
        self.orgs = {}
        self.repos = {}

    def call(self, method, path, body=None, timeout=TIMEOUT):
        status, data = request(method, f"{self.api}{path}", body, {"Authorization": f"token {self.token}"}, timeout)
        if status == 401:
            die(f"{self.api} rejected the token: {error_text(data)}")
        return status, data

    def existing(self, org):
        org = org.lower()
        if org not in self.repos:
            # lowercased name -> mirror interval, None for repos that aren't mirrors
            self.repos[org] = {}
            page = 1
            while True:
                status, body = self.call("GET", f"/orgs/{org}/repos?limit=50&page={page}")
                if status == 404:
                    break
                if status != 200:
                    fail(f"listing repos of {org} (HTTP {status}): {error_text(body)}")
                    break
                if not (batch := json.loads(body)):
                    break
                self.repos[org].update(
                    (repo["name"].lower(), repo["mirror_interval"] if repo["mirror"] else None) for repo in batch
                )
                page += 1
        return self.repos[org]

    def has(self, owner, name):
        return name.lower() in self.existing(owner)

    def sync_interval(self, owner, name, interval):
        current = self.existing(owner)[name.lower()]
        if not interval or current is None or seconds(current) == seconds(interval):
            return
        full_name = f"{owner}/{name}"
        if self.dry_run:
            print(f"would set {full_name} interval to {interval}")
            return
        status, body = self.call("PATCH", f"/repos/{owner}/{name}", {"mirror_interval": interval})
        if status == 200:
            self.existing(owner)[name.lower()] = interval
            print(f"ok    {full_name} interval {interval}")
        else:
            fail(f"setting interval of {full_name} (HTTP {status}): {error_text(body)}")

    def ensure_org(self, org, avatar_url):
        status, body = self.call("GET", f"/orgs/{org}")
        if status == 200:
            if self.avatars:
                self.sync_avatar(org, avatar_url)
            return True
        if status != 404:
            fail(f"looking up {org} (HTTP {status}): {error_text(body)}")
            return False
        if self.dry_run:
            print(f"would create org {org}")
            return True
        status, body = self.call("POST", "/orgs", {"username": org, "visibility": "public"})
        if status != 201:
            fail(f"creating org {org} (HTTP {status}): {error_text(body)}")
            return False
        print(f"created org {org}")
        self.sync_avatar(org, avatar_url)
        return True

    def sync_avatar(self, org, url):
        if self.dry_run or not url:
            return
        status, image = request("GET", url)
        if status != 200:
            fail(f"fetching avatar of {org} (HTTP {status})")
            return
        status, body = self.call("POST", f"/orgs/{org}/avatar", {"image": base64.b64encode(image).decode()})
        if status != 204:
            fail(f"setting avatar of {org} (HTTP {status}): {error_text(body)}")

    def sync_org(self, repo):
        if repo.owner not in self.orgs:
            self.orgs[repo.owner] = self.ensure_org(repo.owner, repo.avatar_url)
        return self.orgs[repo.owner]

    def mirror(self, repo, interval):
        full_name = f"{repo.owner}/{repo.name}"
        if self.has(repo.owner, repo.name):
            self.sync_interval(repo.owner, repo.name, interval)
            return True
        if self.dry_run:
            self.existing(repo.owner)[repo.name.lower()] = interval
            print(f"would mirror {full_name}")
            return True
        status, body = self.call("POST", "/repos/migrate", {
            "clone_addr": cgit_clone_url(repo.clone_url) if repo.cgit else repo.clone_url,
            "repo_owner": repo.owner,
            "repo_name": repo.name,
            "description": repo.description,
            "mirror": True,
            **({"mirror_interval": interval} if interval else {}),
            "lfs": True,
            "wiki": True,
            "service": "git",
            "private": False,
        }, MIGRATE_TIMEOUT)
        if status == 201:
            self.existing(repo.owner)[repo.name.lower()] = interval
            print(f"ok    {full_name}")
        elif status in (504, 524):
            # the proxy gave up waiting, Forgejo usually keeps cloning, the next run checks it again
            warn(f"{full_name} timed out at the reverse proxy (HTTP {status}), it's probably still cloning\n"
                 f"      check that {self.url}/{full_name} exists, and raise the proxy's timeout to stop this")
            return False
        elif status == 0 and body == TIMED_OUT:
            warn(f"{full_name} got no answer in {MIGRATE_TIMEOUT // 60}m, it may still be cloning or stuck\n"
                 f"      check that {self.url}/{full_name} exists, the next run checks it again")
            return False
        elif status != 409:
            message = error_text(body)
            if "fsck error" in message:
                message += "\n      the source has broken objects that your Forgejo's git refuses, see the README"
            fail(f"{full_name} (HTTP {status}): {message}")
            return False
        return True


def seconds(duration):
    units = {"h": 3600, "m": 60, "s": 1}
    return sum(int(amount) * units[unit] for amount, unit in re.findall(r"(\d+)([hms])", duration))


def segment(value):
    return value is None or isinstance(value, str) and SEGMENT.fullmatch(value)


def duration(value):
    return value is None or isinstance(value, str) and INTERVAL.fullmatch(value)


def parse_item(item, interval):
    if isinstance(item, str):
        return Mirror(*split_url(item), True, None, None, interval)
    keys = {"url", "forks", "org", "name", "interval", "exclude"}
    if isinstance(item, dict) and isinstance(item.get("url"), str) and set(item) <= keys:
        forks, org, name, interval = item.get("forks", True), item.get("org"), item.get("name"), item.get("interval", interval)
        exclude = item.get("exclude", [])
        if (
            isinstance(forks, bool) and segment(org) and segment(name) and duration(interval)
            and isinstance(exclude, list) and all(isinstance(pattern, str) and pattern for pattern in exclude)
        ):
            return Mirror(*split_url(item["url"]), forks, org, name, interval, tuple(exclude))
    die(
        f'bad mirror: {item!r}, use "url" or '
        '{ url = "url", forks = false, org = "org", name = "repo", interval = "24h", exclude = ["repo", "prefix-*"] }'
    )


def load_config(path):
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(EXAMPLE, path)
        except OSError as error:
            die(f"can't create config: {error}")
        sys.exit(f"created {path}, add your mirrors to it and run again")

    try:
        with open(path, "rb") as file:
            config = tomllib.load(file)
    except OSError as error:
        die(f"can't read config: {error}")
    except tomllib.TOMLDecodeError as error:
        die(f"bad config {path}: {error}")

    interval = config.pop("interval", "24h")
    if not duration(interval):
        die(f'bad interval: {interval!r}, use hours, minutes and seconds like "24h" or "1h30m", or "0"')
    rescan = config.pop("rescan", "24h")
    if not duration(rescan):
        die(f'bad rescan: {rescan!r}, use hours, minutes and seconds like "24h" or "1h30m", or "0"')

    servers = {}
    for server, section in config.items():
        if not isinstance(section, dict) or not isinstance(section.get("mirrors"), list) or len(section) != 1:
            die(f'[{server}] must only have a mirrors list, and the header needs quotes: ["git.example.com"]')
        url = server if "://" in server else f"https://{server}"
        servers[url.rstrip("/")] = [parse_item(item, interval) for item in section["mirrors"]]
    return servers, rescan


@cache
def find_token(host):
    try:
        result = subprocess.run(["secret-tool", "lookup", "service", NAME, "host", host], capture_output=True, text=True)
        if result.stdout.strip():
            return result.stdout.strip()
    except FileNotFoundError:
        pass

    path = TOKENS / host
    if not path.exists():
        return None
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        die(f"{path} is too open, run: chmod 600 {path}")
    if not (token := path.read_text().strip()):
        die(f"{path} is empty")
    return token


def load_token(url):
    host = urlsplit(url).netloc
    if not (token := find_token(host)):
        die(f"no token for {host}, run: {NAME} --token {host}")
    return token


def store_token(host):
    host = split_url(host)[0].split("://", 1)[1]
    try:
        token = getpass.getpass(f"token for {host}: ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit(1)
    if not token:
        die("no token given")
    try:
        result = subprocess.run(
            ["secret-tool", "store", f"--label={NAME} {host}", "service", NAME, "host", host],
            input=token, capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"saved token for {host} in the keyring")
            return
        print(f"keyring failed: {result.stderr.strip()}", file=sys.stderr)
    except FileNotFoundError:
        pass
    path = TOKENS / host
    try:
        TOKENS.mkdir(parents=True, exist_ok=True)
        path.touch(0o600)
        path.chmod(0o600)
        path.write_text(f"{token}\n")
    except OSError as error:
        die(f"can't save token: {error}")
    print(f"saved token for {host} in {path}")


def load_scans():
    try:
        return json.loads(SCANS.read_text())
    except (OSError, ValueError):
        return {}


def save_scans(scans):
    try:
        SCANS.parent.mkdir(parents=True, exist_ok=True)
        temporary = SCANS.with_suffix(".tmp")
        temporary.write_text(json.dumps(scans, indent=1))
        temporary.replace(SCANS)
    except OSError as error:
        fail(f"can't save {SCANS}: {error}")


def main():
    parser = argparse.ArgumentParser(description="Mirror GitHub, GitLab and Forgejo repos into Forgejo orgs.")
    parser.add_argument("config", nargs="?", type=Path, default=CONFIG)
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument("--avatars", action="store_true", help="re-upload avatars of existing orgs")
    parser.add_argument("-f", "--force", action="store_true", help="check sources that were checked recently too")
    parser.add_argument("--token", metavar="HOST", help="save a token for a server or source, e.g. github.com")
    args = parser.parse_args()

    if args.token:
        store_token(args.token)
        return

    servers, rescan = load_config(args.config)
    rescan_seconds = seconds(rescan)
    tokens = {url: load_token(url) for url in servers}
    scans, old_scans = {}, load_scans()

    for url, items in servers.items():
        print(f"== {url}")
        forgejo = Forgejo(url, tokens[url], args.dry_run, args.avatars)
        recent = 0
        for item in items:
            origin, path, org, name = item.origin, item.path, item.org, item.name
            # owner/repo urls name their target, so existing mirrors never touch the source
            if len(path) == 2 and not args.avatars and forgejo.has(org or path[0], name or path[1]):
                forgejo.sync_interval(org or path[0], name or path[1], item.interval)
                continue
            # sources checked within rescan are skipped, unless one of their mirrors went missing
            key = f"{url} {origin}/{'/'.join(path)} forks={item.forks} org={org} name={name} exclude={sorted(item.exclude)}"
            scan = old_scans.get(key)
            if (
                scan and not (args.force or args.avatars) and time.time() - scan["time"] < rescan_seconds
                and all(forgejo.has(*repo) for repo in scan["repos"])
            ):
                for repo in scan["repos"]:
                    forgejo.sync_interval(*repo, item.interval)
                scans[key] = scan
                recent += 1
                continue
            if origin in limited:
                continue
            try:
                repos = fetch_repos(origin, path, item.forks)
            except RateLimited as error:
                limited.add(origin)
                fail(f"{error}, skipping {origin} for the rest of this run")
                continue
            except SourceError as error:
                fail(str(error))
                continue
            # matched against the source name, case-insensitively, before any rename
            repos = [
                repo for repo in repos
                if not any(fnmatch.fnmatch(repo.name.lower(), pattern.lower()) for pattern in item.exclude)
            ]
            if not repos:
                print(f"skip  {origin}/{'/'.join(path)} (no public repos)")
            if name and len(repos) > 1:
                fail(f"{origin}/{'/'.join(path)} has {len(repos)} repos, name only works on single repos")
                continue
            complete, mirrored = True, []
            for repo in repos:
                if name:
                    repo = repo._replace(name=name)
                if org:
                    repo = repo._replace(owner=org, avatar_url=repo.avatar_url if not repo.owner else "")
                if not repo.owner:
                    fail(f"{repo.clone_url} has no owner, set org")
                    complete = False
                elif forgejo.sync_org(repo) and forgejo.mirror(repo, item.interval):
                    mirrored.append((repo.owner, repo.name))
                else:
                    complete = False
            if complete:
                scans[key] = {"time": time.time(), "repos": mirrored}
        if recent:
            print(f"skip  {recent} source{'s' * (recent > 1)} checked in the last {rescan}, pass --force to check them")

    if not args.dry_run:
        save_scans(scans)
    sys.exit(failed)


if __name__ == "__main__":
    main()
