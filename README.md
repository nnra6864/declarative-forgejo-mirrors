## Forgejo Mirrors

> [!WARNING]
> This was made with Claude.<br>
> It's made for personal use only, so don't expect any kind of support.

Utility script for mirroring repos and entire users/organizations from most Git platforms to Forgejo.

Everything is mirrored into a Forgejo org named after the source owner, so `owner/repo` is the same path on both servers and relative submodule URLs like `../../owner/repo` work on either.

Requires Python 3.11+.

### Setup

Forgejo tokens can't be limited to creating without deleting, `write` always includes both.\
So limit who the token belongs to instead, and **don't use an admin's token**:

1. As admin, create a regular user, e.g. `mirror-bot`, that may create organizations.
2. Log in as it, then under Settings → Applications generate a token with:
   - organization: read and write
   - repository: read and write
3. Run `./forgejo-mirrors.py` once, it copies `config.toml.example` to `~/.config/forgejo-mirrors/config.toml` and exits.
4. Save the token in your keyring, this works with anything that provides Secret Service, e.g. KeePassXC, GNOME Keyring or KWallet:
   ```sh
   secret-tool store --label='forgejo-mirrors git.nnstdios.com' service forgejo-mirrors host git.nnstdios.com
   ```

Without a keyring, e.g. on a headless server, the token is read from `~/.local/share/forgejo-mirrors/tokens/<server>` instead, which has to be `chmod 600`.

If that token leaks, the worst it can do is delete or tamper with the bot's own orgs and mirrors, and create junk orgs and repos.\
It can't touch other users or their repos. Revoke it and re-run the script, which restores anything deleted.

### Usage

Each header in the config is a server to mirror to, with its own token. Paste repo, user or org URLs under it:

```toml
["git.nnstdios.com"]
mirrors = [
  "https://github.com/owner/repo",
  "https://gitlab.com/owner",
  { url = "https://codeberg.org/owner", forks = false },
  { url = "https://git.example.com/repo", org = "my-mirrors" },
]
```

Plain git servers have no owners, so their repos need an `org`, which also works on any other URL to override the target org.\
Avatars are only copied when `org` isn't set, and the bot must be an owner of that org.

```sh
./forgejo-mirrors.py --dry-run
./forgejo-mirrors.py
```

Existing repos are skipped, so re-running only picks up new ones and refreshes org avatars, and nothing is ever deleted.\
To move servers, change the header, save a token for it and run it again.

### Limitations

- Headers need quotes, otherwise TOML treats the dots as nesting.
- Plain git servers can only mirror single repos, since there's no way to list them.
- An owner can't be mirrored if that name is already a Forgejo user, and the bot must be an owner of any org that already exists.
- Same-named owners on different sources share an org, and if a repo name is taken the later one is skipped.
- GitLab subgroups are flattened, `group/sub/repo` becomes `group/sub-repo`, so relative URLs won't work for those.
- GitHub allows 60 unauthenticated requests per hour.
- GitLab rate limits unauthenticated requests hard, especially for user URLs, so those can fail with HTTP 429 for a while. Group and repo URLs are more reliable.
- `forks = false` doesn't work on GitLab, which doesn't reveal forks without a token.
- Each repo has to finish cloning before the next starts, so a reverse proxy timeout can show up as `FAIL`.

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
