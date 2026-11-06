## Forgejo Mirrors

> [!WARNING]
> This was made with Claude.<br>
> It's made for personal use only, so don't expect any kind of support.

Utility script for declaratively mirroring repos and entire users/organizations from most Git platforms to Forgejo.

Everything is mirrored into a Forgejo org named after the source owner, so `owner/repo` is the same path on both servers and relative submodule URLs like `../../owner/repo` work on either.

Requires Python 3.11+.

### Setup

Forgejo tokens can't be limited to creating without deleting, `write` always includes both.\
So limit who the token belongs to instead, and **don't use an admin's token**:

1. As admin, create a regular user, e.g. `mirror-bot`, that may create organizations.
2. Log in as it, then under Settings → Applications generate a token with:
   - organization: read and write
   - repository: read and write
3. From the repo, link the script into your `PATH`:
   ```sh
   mkdir -p ~/.local/bin
   ln -s "$PWD/declarative-forgejo-mirrors.py" ~/.local/bin/declarative-forgejo-mirrors
   ```
4. Run `declarative-forgejo-mirrors` once, it copies `config.toml.example` to `~/.config/declarative-forgejo-mirrors/config.toml` and exits.
5. Save the token, it asks for it and stores it in your keyring, which works with anything that provides Secret Service, e.g. KeePassXC, GNOME Keyring or KWallet:
   ```sh
   declarative-forgejo-mirrors --token git.nnstdios.com
   ```

Without a keyring, e.g. on a headless server, it's saved to `~/.local/share/declarative-forgejo-mirrors/tokens/<host>` instead, which has to stay `chmod 600`.

#### Source tokens

Tokens for sources are optional, and are saved the same way under the source's host:

```sh
declarative-forgejo-mirrors --token github.com
declarative-forgejo-mirrors --token gitlab.com
declarative-forgejo-mirrors --token codeberg.org
```

They're sent with every API request to that host, which raises its rate limit, GitHub goes from 60 to 5000 requests per hour, and lets `forks = false` work on GitLab.\
A read-only token is enough:

- GitHub: a fine-grained token with "Public repositories" access and no permissions, under Settings → Developer settings → Personal access tokens.
- GitLab: a personal access token with only the `read_api` scope.
- Forgejo, e.g. Codeberg: a token with repository and user set to read.

If that token leaks, the worst it can do is delete or tamper with the bot's own orgs and mirrors, and create junk orgs and repos.\
It can't touch other users or their repos. Revoke it and re-run the script, which restores anything deleted.

### Usage

Each header in the config is a server to mirror to, with its own token. Paste repo, user or org URLs under it:

```toml
interval = "24h"
rescan = "24h"

["git.nnstdios.com"]
mirrors = [
  "https://github.com/owner/repo",
  "https://gitlab.com/owner",
  { url = "https://codeberg.org/owner", forks = false },
  { url = "https://github.com/owner", archived = false },
  { url = "https://git.example.com/repo", org = "my-mirrors" },
  { url = "https://cgit.example.com", org = "my-mirrors" },
  { url = "https://github.com/owner/repo", name = "other-name" },
  { url = "https://github.com/owner", interval = "1h" },
  { url = "https://github.com/owner", exclude = ["huge-repo", "old-*"] },
]
```

`forks = false` and `archived = false` skip forks and archived repos, both are mirrored by default. Like `exclude`, they only stop new mirrors.\
The archived status itself isn't mirrored, mirrors of archived repos are regular mirrors on your server.\
`name` renames the mirror and only works on URLs that resolve to a single repo.\
`exclude` skips repos by their name on the source, ignoring case, `*` and `?` work as wildcards. It only stops new mirrors, ones that already exist are kept.\
`interval` sets how often Forgejo syncs the mirrors, at the top for all of them and on a mirror to override that, as hours, minutes and seconds like `24h` or `1h30m`, or `0` to never sync. The default is `24h`, which also applies to existing mirrors, and Forgejo rejects anything below its minimum, `10m` by default.

`rescan` is how long a source is left alone after it was checked, `24h` by default, or `0` to check every run.\
Within it, only the intervals of its mirrors are updated, and it's checked anyway if one of its mirrors went missing.

Plain git servers have no owners, so their repos need an `org`, which also works on any other URL to override the target org.\
Avatars are only copied when `org` isn't set, or when the source has no owners, like a cgit logo, and the bot must be an owner of that org.

```sh
declarative-forgejo-mirrors --dry-run
declarative-forgejo-mirrors
```

Existing repos are skipped, so re-running only picks up new ones, and nothing is ever deleted.\
Single repo URLs that are already mirrored are checked against your server only, without contacting the source.\
Sources checked within `rescan` are skipped, pass `--force` to check them all.\
Avatars are only uploaded when an org is created, pass `--avatars` to refresh them for existing orgs too.\
To move servers, change the header, save a token for it and run it again.

### Limitations

- Headers need quotes, otherwise TOML treats the dots as nesting, and the global `interval` has to come before them.
- Plain git servers can only mirror single repos, since there's no way to list them, except cgit, whose index page lists them all.
- An owner can't be mirrored if that name is already a Forgejo user, and the bot must be an owner of any org that already exists.
- Same-named owners on different sources share an org, and if a repo name is taken the later one is skipped.
- GitLab subgroups are flattened, `group/sub/repo` becomes `group/sub-repo`, so relative URLs won't work for those.
- GitHub allows 60 unauthenticated requests per hour, and GitLab rate limits unauthenticated requests hard, especially for user URLs. Once a source rate limits, it's skipped for the rest of the run, save a token for it to avoid that.
- `forks = false` doesn't work on GitLab without a token, since it doesn't reveal forks otherwise.
- Some old repos have broken objects, like commits with invalid dates, and fail with `fsck error`. That's Forgejo's git refusing them, not this script. Allow them in Forgejo's `app.ini`, which also covers syncing them later:
  ```ini
  [git.config]
  fetch.fsck.badDate = ignore
  ```
  Use the name after the object hash in the error, or `fetch.fsckObjects = false` to stop checking entirely.
- `name` only applies when a mirror is created, while `interval` is also updated on existing mirrors.
- Forgejo clones a repo before answering, so big ones can hit your reverse proxy's timeout. That's a `WARN`, not a `FAIL`, since Forgejo usually finishes the clone anyway. Check that the repo shows up, and raise the proxy timeout for `/api/v1/repos/migrate` (e.g. `proxy_read_timeout 30m;` in nginx/Angie) to stop it.

# ☦

```
   Ὤ
 Ὁ   Ν
Ι̅Ϲ̅ │ Χ̅Ϲ̅
───┼───
ΝΙ │ ΚΑ
   ☦
```

Εἰς δόξαν τοῦ Θεοῦ<br>
*To the glory of God*

Τῇ Ὑπεραγίᾳ Θεοτόκῳ δόξα<br>
*Glory to the Most Holy Theotokos*

Δόξα τῷ Θεῷ πάντων ἕνεκεν<br>
*Glory to God for all things*

ΑΜΗΝ

☦
