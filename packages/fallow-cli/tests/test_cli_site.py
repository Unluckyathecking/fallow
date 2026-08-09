from __future__ import annotations
import base64, json, os
from pathlib import Path
import pytest
from fallow_cli.models import SiteJoinBundle
from fallow_cli.site import write_join_bundles
from fallow_cli.errors import CliError

def bundle(token="secret"):
 return SiteJoinBundle.model_validate({"version":1,"site_id":"pilot","coordinator_urls":["https://coord.example:8330"],"coordinator_spki_sha256":["sha256/"+base64.b64encode(b"p"*32).decode()],"enrollment_token":token,"mdns_service":None})
def test_atomic_owner_only_files_and_metadata(tmp_path):
 out=tmp_path/"join"; meta=write_join_bundles((bundle(),bundle("other")),out,force=False)
 assert len(meta)==2 and (out/"desk-01.fallow-join").exists()
 assert os.stat(out/"desk-01.fallow-join").st_mode & 0o777 == 0o600
 assert json.loads((out/"desk-01.fallow-join").read_text())["enrollment_token"] == "secret"
 assert "secret" not in json.dumps(meta)
def test_refusal_and_force(tmp_path):
 out=tmp_path/"join"; write_join_bundles((bundle(),),out,force=False)
 with pytest.raises(CliError): write_join_bundles((bundle("new"),),out,force=False)
 write_join_bundles((bundle("new"),),out,force=True)
 assert json.loads((out/"desk-01.fallow-join").read_text())["enrollment_token"] == "new"
@pytest.mark.parametrize("field", ["version","coordinator_urls","coordinator_spki_sha256","enrollment_token","mdns_service"])
def test_invalid_bundle_rejected(field):
 b={"version":1,"site_id":"pilot","coordinator_urls":["https://coord.example"],"coordinator_spki_sha256":["sha256/"+base64.b64encode(b"p"*32).decode()],"enrollment_token":"x","mdns_service":None}
 b.pop(field)
 with pytest.raises(Exception): SiteJoinBundle.model_validate(b)
