# Releasing

Same shape as `bm`: push a `v*.*.*` tag and CI does the rest.

```bash
# bump version in pyproject.toml first — CI fails the build if it
# disagrees with the tag
git commit -am "pdfind 0.1.0"
git tag -a v0.1.0 -m "pdfind 0.1.0"
git push origin master --tags
```

`.github/workflows/release.yml` then runs:

| Job | Result |
|---|---|
| `build` | sdist + wheel, `twine check`, tag/version guard |
| `github-release` | GitHub release with generated notes, artifacts attached |
| `pypi` | publishes to PyPI via trusted publishing |
| `aur` | pins `pkgver`/`sha256sums`, pushes to `ssh://aur@aur.archlinux.org/pdfind.git` |
| `homebrew` | updates `Formula/pdfind.rb` in `Shu-AFK/homebrew-tap` |

## Why not goreleaser

`bm` uses goreleaser, and its `nfpms`/`brews`/`scoops`/`aurs` sections are what
publish everything. Those all key off goreleaser's `builds:` section, which
cross-compiles **Go** binaries per OS/arch. pdfind is pure Python: one
`py3-none-any` wheel covers every platform, and the AUR package is `arch=any`,
so there is nothing to cross-compile and no per-arch artifact to attach.

The workflow above therefore does the same jobs by hand. Practical
consequences of the difference:

- The AUR package is **`pdfind`** (builds from the source tarball), not
  `pdfind-bin`. `bm-bin` is a `-bin` package because it ships prebuilt Go
  binaries; there is no such thing here.
- No Scoop bucket entry. pdfind needs poppler and a unix socket; Windows is
  untested and `env.py` only has a best-effort `os.startfile` path.
- No deb/rpm from `nfpms`. PyPI covers those users via
  `pipx install pdfind`; building distro packages for a Python app with 29
  transitive dependencies is a much bigger job than shipping one Go binary.

## Secrets

Two already exist on `bm` and can be reused verbatim — the workflow uses the
same names:

- `AUR_KEY` — SSH private key registered with your AUR account
- `GORELEASER_GITHUB_TOKEN` — PAT with write access to `Shu-AFK/homebrew-tap`

One is new. **PyPI trusted publishing**, which needs no token at all:

1. Create the project on PyPI (or pre-register the name — `pdfind` was
   unclaimed as of writing).
2. PyPI → project → Publishing → add a GitHub publisher:
   owner `Shu-AFK`, repo `pdfind`, workflow `release.yml`, environment `pypi`.
3. In the GitHub repo, create an environment named `pypi`.

Until step 3 exists the `pypi` job fails; the other jobs are unaffected.

## First release checklist

The AUR package name must be registered by your first push — that happens
automatically, but the repo has to be public and the tag must exist first,
because the PKGBUILD fetches
`https://github.com/Shu-AFK/pdfind/archive/refs/tags/v0.1.0.tar.gz`.

```bash
gh repo create Shu-AFK/pdfind --public --source=. --remote=origin --push
```

## Doing it by hand

If you want to publish without CI:

```bash
# PyPI
python -m build && python -m twine check dist/* && python -m twine upload dist/*

# AUR
cd packaging/aur
updpkgsums                       # needs pacman-contrib
makepkg -si                      # needs python-build python-installer
namcap PKGBUILD *.pkg.tar.zst    # needs namcap
git clone ssh://aur@aur.archlinux.org/pdfind.git aur && cp PKGBUILD aur/
cd aur && makepkg --printsrcinfo > .SRCINFO
git add -A && git commit -m "pdfind 0.1.0" && git push
```

`packaging/aur/PKGBUILD-git` is the master-tracking counterpart, for
`ssh://aur@aur.archlinux.org/pdfind-git.git`.

## Bumping a version

Only `pyproject.toml` needs editing — CI derives `pkgver`, the formula `url`,
and both checksums from the tag. Keep `packaging/aur/PKGBUILD`'s committed
`pkgver` roughly in step anyway so a manual `makepkg` still works.
