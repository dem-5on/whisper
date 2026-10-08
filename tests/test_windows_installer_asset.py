from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_website_download_installer_matches_canonical_installer():
    canonical = ROOT / "install.ps1"
    website_asset = ROOT / "website" / "download" / "whisper-installer.ps1"

    assert website_asset.read_bytes() == canonical.read_bytes()
