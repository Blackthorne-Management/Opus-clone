import yaml
import pytest
from fastapi.testclient import TestClient

from kir_clipper import onboarding, studio
from kir_clipper.config import load_config
from kir_clipper.web.app import create_app

from .conftest import ROOT

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def friend_cfg(cfg, tmp_path):
    cfg.root = tmp_path                       # personal files go to a temp folder, not the repo
    (tmp_path / onboarding.TEMPLATE).write_text((ROOT / onboarding.TEMPLATE).read_text(encoding="utf-8"), encoding="utf-8")
    return cfg


def test_setup_writes_personal_files_only(friend_cfg, tmp_path):
    cfg = friend_cfg
    assert not onboarding.current(cfg)["done"]
    onboarding.save(cfg, {"name": "GamerGabe", "timezone": "America/Chicago", "inbox": str(tmp_path / "rec"),
                          "cta_text": "Follow @gabe", "topics": "Fortnite tips", "wants": "big plays", "criteria": ""},
                    logo_bytes=PNG)
    local = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
    assert local["brand"]["name"] == "GamerGabe" and local["setup"]["done"] is True
    assert local["highlights"]["criteria_file"] == "my-criteria.md"
    criteria = (tmp_path / "my-criteria.md").read_text(encoding="utf-8")
    assert "GamerGabe streams about Fortnite tips" in criteria and "{name}" not in criteria
    assert "Keepingit_Rael" not in criteria
    assert (tmp_path / "brand" / "logo.png").read_bytes() == PNG
    now = onboarding.current(cfg)
    assert now["done"] and now["name"] == "GamerGabe" and now["timezone"] == "America/Chicago"


def test_setup_survives_a_reload(friend_cfg, tmp_path):
    onboarding.save(friend_cfg, {"name": "Gabe", "timezone": "Europe/London", "inbox": "~/Streams/inbox"})
    (tmp_path / "config.yaml").write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    reloaded = load_config(tmp_path / "config.yaml")
    assert reloaded.get("brand.name") == "Gabe" and reloaded.get("setup.done")


@pytest.mark.parametrize("form,msg", [({"name": "", "timezone": "UTC", "inbox": "x"}, "name"),
                                      ({"name": "a", "timezone": "Mars/Base", "inbox": "x"}, "time zone"),
                                      ({"name": "a", "timezone": "UTC", "inbox": ""}, "folder"),
                                      ({"name": "a", "timezone": "UTC", "inbox": "x", "upload_post_user": "a b"}, "profile")])
def test_setup_validation(friend_cfg, form, msg):
    with pytest.raises(ValueError, match=msg):
        onboarding.save(friend_cfg, form)


def test_studio_profiles_start_from_templates_and_save_separately(friend_cfg, tmp_path):
    cfg = friend_cfg
    (tmp_path / "brand").mkdir(exist_ok=True)
    (tmp_path / "brand" / "voice.md").write_text("TEMPLATE", encoding="utf-8")
    assert studio.read_profile(cfg)["voice"] == "TEMPLATE"
    studio.save_profile(cfg, voice="MINE")
    assert (tmp_path / "brand" / "my-voice.md").read_text(encoding="utf-8") == "MINE"
    assert (tmp_path / "brand" / "voice.md").read_text(encoding="utf-8") == "TEMPLATE"   # template untouched
    assert studio.read_profile(cfg)["voice"] == "MINE"


def test_setup_screen_endpoints(friend_cfg, tmp_path):
    client = TestClient(create_app(friend_cfg))
    s = client.get("/api/summary").json()
    assert s["setup_done"] is False and s["brand"]
    r = client.post("/api/setup", data={"name": "Gabe", "timezone": "UTC", "inbox": str(tmp_path / "in")},
                    files={"logo": ("logo.png", PNG, "image/png")})
    assert r.status_code == 200 and r.json()["restart"] is True
    assert client.get("/api/summary").json()["brand"] == "Gabe"
    bad = client.post("/api/setup", data={"name": "Gabe", "timezone": "UTC", "inbox": "x"},
                      files={"logo": ("logo.jpg", b"\xff\xd8\xff", "image/jpeg")})
    assert bad.status_code == 400 and "PNG" in bad.json()["detail"]
