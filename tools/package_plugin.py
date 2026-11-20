"""Build and verify an installable ZIP from clean, committed Git blobs."""
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "maibot_plugin_personal_assistant_webui"


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def main():
    if git("status", "--porcelain").strip():
        raise SystemExit("Commit source changes before building an installation archive")
    paths = git("ls-files", "-z").decode("utf-8").rstrip("\0").split("\0")
    files = {}
    for name in paths:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe tracked path: {name}")
        if name == "config.toml" or relative.suffix == ".pyc" or {".git", "__pycache__"} & set(relative.parts):
            raise ValueError(f"Runtime file must not be tracked: {name}")
        files[name] = git("show", f"HEAD:{name}")
    manifest = json.loads(files["_manifest.json"])
    declaration = json.loads(files["webui.json"])
    assert manifest["display"]["icon"]["type"] in {"lucide", "emoji", "local"}
    assert declaration["pages"][0]["icon"] in {"puzzle", "chart", "settings", "database", "list"}
    assert declaration["pages"][0]["entry"] in files
    inventory = "".join(f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in sorted(files.items()))
    files["CONTENTS.sha256"] = inventory.encode("utf-8")
    archive = ROOT.parent / f"{PREFIX}-{manifest['version']}.zip"
    # Fixed timestamps make an unchanged commit reproducibly packageable.
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(f"{PREFIX}/{name}", date_time=(2026, 11, 20, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            output.writestr(info, content)
    with zipfile.ZipFile(io.BytesIO(archive.read_bytes())) as check:
        assert check.testzip() is None
        assert set(check.namelist()) == {f"{PREFIX}/{name}" for name in files}
        for name, content in files.items():
            assert check.read(f"{PREFIX}/{name}") == content, name
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"Verified {len(files)} entries, version {manifest['version']}, commit {git('rev-parse', 'HEAD').decode().strip()}")
    print(f"SHA256 {digest}\n{archive}")


if __name__ == "__main__":
    main()
