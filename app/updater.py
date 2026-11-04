"""Self-update from GitHub, keeping your settings, data and passwords.

Used two ways:
  * the "Update app" button in the dashboard (runs as the app user, then the app restarts itself)
  * deploy/update.sh from the server console:  python app/updater.py <extracted-source-dir>

Never touched: .env (keys/passwords), data/ (jobs, resume, logins, screenshots).
config.yaml: your edits are kept; hospitals added in the new version are appended.
"""
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml

TARBALL = "https://codeload.github.com/ardentluma01-spec/hospital-autoapply/tar.gz/refs/heads/main"
ROOT = Path(__file__).resolve().parent.parent
OLD_DEFAULTS = {
    "include_keywords": [["patient services", "psa", "patient service assistant", "ward assistant", "personal care",
                          "enrolled nurse", "registered nurse", "nurse"]],
    "exclude_keywords": [["manager", "director", "educator", "midwife", "pharmacist", "registrar", "medical officer", "specialist"]],
}
REPLACE = ["app", "deploy", "requirements.txt", "README.md", "install.sh", "VERSION", ".env.example"]


def _merge_config(new_cfg: Path, cur_cfg: Path):
    if not cur_cfg.exists():
        shutil.copy2(new_cfg, cur_cfg)
        return
    cur = yaml.safe_load(cur_cfg.read_text()) or {}
    new = yaml.safe_load(new_cfg.read_text()) or {}
    # replace placeholder entries (e.g. Northern Health "PASTE..." address) with the new version's entry
    newmap = {h.get("login_env"): h for h in new.get("hospitals", [])}
    cur["hospitals"] = [newmap.get(h.get("login_env"), h) if "PASTE" in str(h.get("careers_url", "")) else h
                        for h in cur.get("hospitals", [])]
    have = {h.get("login_env") for h in cur.get("hospitals", [])}
    added = [h for h in new.get("hospitals", []) if h.get("login_env") not in have]
    for k, v in new.items():          # new settings the old file didn't have
        cur.setdefault(k, v)
    # if the keyword lists are still an older version's defaults (never edited), use the new defaults
    for k in ("include_keywords", "exclude_keywords"):
        if sorted(cur.get(k) or []) in [sorted(d) for d in OLD_DEFAULTS.get(k, [])]:
            cur[k] = new.get(k, cur[k])
    if added:
        cur.setdefault("hospitals", []).extend(added)
    cur_cfg.write_text(yaml.safe_dump(cur, sort_keys=False, allow_unicode=True))


def apply_from_dir(src: Path, root: Path = ROOT) -> str:
    src = Path(src)
    if not (src / "app" / "main.py").exists():
        kids = [p for p in src.iterdir() if p.is_dir()]
        if len(kids) == 1 and (kids[0] / "app" / "main.py").exists():
            src = kids[0]
        else:
            raise RuntimeError("download doesn't look like the app")
    for name in REPLACE:
        s, d = src / name, root / name
        if not s.exists():
            continue
        if s.is_dir():
            tmp = root / f".{name}.new"
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(s, tmp)
            shutil.rmtree(d, ignore_errors=True)
            tmp.rename(d)
        else:
            shutil.copy2(s, d)
    if (src / "config.yaml").exists():
        _merge_config(src / "config.yaml", root / "config.yaml")
    pip = root / ".venv" / "bin" / "pip"
    if pip.exists():
        subprocess.run([str(pip), "install", "-q", "-r", str(root / "requirements.txt")], check=False, timeout=900)
    return (root / "VERSION").read_text().strip() if (root / "VERSION").exists() else "updated"


def update_from_github() -> str:
    import httpx
    r = httpx.get(TARBALL, timeout=120, follow_redirects=True)
    r.raise_for_status()
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
            tf.extractall(tmp, filter="data")
        return apply_from_dir(Path(tmp))


if __name__ == "__main__":
    # usage: updater.py <extracted-source-dir> [install-dir, default /opt/autoapply]
    print("Updated to", apply_from_dir(Path(sys.argv[1]), Path(sys.argv[2] if len(sys.argv) > 2 else "/opt/autoapply")))
