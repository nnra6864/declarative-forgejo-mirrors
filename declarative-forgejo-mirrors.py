#!/usr/bin/env python3
import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
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
SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
CGIT_REPO = re.compile(r"<td class='(?:sub|top)level-repo'><a [^>]*?href='([^']+)'.*?</td><td><a [^>]*>(.*?)</a>", re.S)
CGIT_LOGO = re.compile(r"<td class='logo'.*?<img src='([^']+)'", re.S)
CGIT_STOCK_LOGO = "db2b9d874e71b03f02d1fef2f6786bec0721a4992653a0eb0766ec8691537bb2"

failed = False


class Repo(NamedTuple):
    owner: str
    name: str
    clone_url: str
    description: str
    avatar_url: str
    fork: bool


class SourceError(Exception):
    pass


def die(message):
    sys.exit(f"error: {message}")


def fail(message):
    global failed
    failed = True
    print(f"FAIL  {message}", file=sys.stderr)


def error_text(body):
    try:
        return json.loads(body)["message"]
    except (ValueError, KeyError, TypeError):
        return body.decode(errors="replace").strip()[:200]


def request(method, url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"User-Agent": NAME, "Content-Type": "application/json", **(headers or {})}
    try:
        with urlopen(Request(url, data, headers, method=method)) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
    except URLError as error:
        return 0, str(error.reason).encode()


def get_json(url):
    status, body = request("GET", url)
    if status != 200:
        raise SourceError(f"{url} (HTTP {status}): {error_text(body)}")
    return json.loads(body)


def probe(url):
    status, body = request("GET", url)
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
            repos[clone_url] = Repo("", name, clone_url, html.unescape(description).strip(), avatar_url, False)
        if len(repos) == count:
            break
    return list(repos.values())


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
    if source is gitlab_repos and not forks:
        raise SourceError(f"{origin} doesn't reveal forks without a token, remove forks = false")
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
        self.api = f"{url}/api/v1"
        self.token = token
        self.dry_run = dry_run
        self.avatars = avatars
        self.orgs = {}
        self.repos = {}

    def call(self, method, path, body=None):
        status, data = request(method, f"{self.api}{path}", body, {"Authorization": f"token {self.token}"})
        if status == 401:
            die(f"{self.api} rejected the token: {error_text(data)}")
        return status, data

    def existing(self, org):
        org = org.lower()
        if org not in self.repos:
            self.repos[org] = set()
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
                self.repos[org].update(repo["name"].lower() for repo in batch)
                page += 1
        return self.repos[org]

    def has(self, owner, name):
        return name.lower() in self.existing(owner)

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
        # sites often replace cgit.png with their own logo, so only the stock one is skipped
        if hashlib.sha256(image).hexdigest() == CGIT_STOCK_LOGO:
            return
        status, body = self.call("POST", f"/orgs/{org}/avatar", {"image": base64.b64encode(image).decode()})
        if status != 204:
            fail(f"setting avatar of {org} (HTTP {status}): {error_text(body)}")

    def sync_org(self, repo):
        if repo.owner not in self.orgs:
            self.orgs[repo.owner] = self.ensure_org(repo.owner, repo.avatar_url)
        return self.orgs[repo.owner]

    def mirror(self, repo):
        full_name = f"{repo.owner}/{repo.name}"
        if self.has(repo.owner, repo.name):
            return
        if self.dry_run:
            self.existing(repo.owner).add(repo.name.lower())
            print(f"would mirror {full_name}")
            return
        status, body = self.call("POST", "/repos/migrate", {
            "clone_addr": repo.clone_url,
            "repo_owner": repo.owner,
            "repo_name": repo.name,
            "description": repo.description,
            "mirror": True,
            "lfs": True,
            "wiki": True,
            "service": "git",
            "private": False,
        })
        if status == 201:
            self.existing(repo.owner).add(repo.name.lower())
            print(f"ok    {full_name}")
        elif status != 409:
            fail(f"{full_name} (HTTP {status}): {error_text(body)}")


def parse_item(item):
    if isinstance(item, str):
        return split_url(item), True, None
    if isinstance(item, dict) and isinstance(item.get("url"), str) and set(item) <= {"url", "forks", "org"}:
        forks, org = item.get("forks", True), item.get("org")
        if isinstance(forks, bool) and (org is None or isinstance(org, str) and SEGMENT.fullmatch(org)):
            return split_url(item["url"]), forks, org
    die(f'bad mirror: {item!r}, use "url" or {{ url = "url", forks = false, org = "org" }}')


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

    servers = {}
    for server, section in config.items():
        if not isinstance(section, dict) or not isinstance(section.get("mirrors"), list) or len(section) != 1:
            die(f'[{server}] must only have a mirrors list, and the header needs quotes: ["git.example.com"]')
        url = server if "://" in server else f"https://{server}"
        servers[url.rstrip("/")] = [parse_item(item) for item in section["mirrors"]]
    return servers


def load_token(url):
    host = urlsplit(url).netloc
    try:
        result = subprocess.run(["secret-tool", "lookup", "service", NAME, "host", host], capture_output=True, text=True)
        if result.stdout.strip():
            return result.stdout.strip()
    except FileNotFoundError:
        pass

    path = TOKENS / host
    if not path.exists():
        die(f"no token for {host}, run: secret-tool store --label='{NAME} {host}' service {NAME} host {host}")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        die(f"{path} is too open, run: chmod 600 {path}")
    token = path.read_text().strip()
    if not token:
        die(f"{path} is empty")
    return token


def main():
    parser = argparse.ArgumentParser(description="Mirror GitHub, GitLab and Forgejo repos into Forgejo orgs.")
    parser.add_argument("config", nargs="?", type=Path, default=CONFIG)
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument("--avatars", action="store_true", help="re-upload avatars of existing orgs")
    args = parser.parse_args()

    servers = load_config(args.config)
    tokens = {url: load_token(url) for url in servers}

    for url, items in servers.items():
        print(f"== {url}")
        forgejo = Forgejo(url, tokens[url], args.dry_run, args.avatars)
        for (origin, path), forks, org in items:
            # owner/repo urls name their target, so existing mirrors never touch the source
            if len(path) == 2 and not args.avatars and forgejo.has(org or path[0], path[1]):
                continue
            try:
                repos = fetch_repos(origin, path, forks)
            except SourceError as error:
                fail(str(error))
                continue
            if not repos:
                print(f"skip  {origin}/{'/'.join(path)} (no public repos)")
            for repo in repos:
                if org:
                    repo = repo._replace(owner=org, avatar_url=repo.avatar_url if not repo.owner else "")
                if not repo.owner:
                    fail(f"{repo.clone_url} has no owner, set org")
                elif forgejo.sync_org(repo):
                    forgejo.mirror(repo)

    sys.exit(failed)


if __name__ == "__main__":
    main()
