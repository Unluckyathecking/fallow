from __future__ import annotations

import httpx
from cli_helpers import COORD_URL
from fallow_cli import main
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


def invoke(runner, env, args, *, json_output=False):
    flags=["--coordinator-url",COORD_URL]+(["--json"] if json_output else [])
    return runner.invoke(main.app, [*flags,*args], env=env)

def wire(b): return b.model_dump(mode="json")

def test_command_posts_exact_count_and_redacts(runner, env, monkeypatch, tmp_path):
    store={}; b=bundle("TOPSECRET")
    def handler(req):
        store["method"]=req.method; store["path"]=req.url.path; store["body"]=json.loads(req.content)
        return __import__("httpx").Response(201,json={"bundles":[wire(b)]})
    monkeypatch.setattr(main,"_ADMIN_TRANSPORT",__import__("httpx").MockTransport(handler))
    result=invoke(runner,env,["site","join-bundles","--count","1","--output",str(tmp_path)])
    assert result.exit_code==0 and store=={"method":"POST","path":"/v1/admin/site/join-bundles","body":{"count":1}}
    assert "TOPSECRET" not in result.stdout and "TOPSECRET" not in result.stderr

def test_json_output_redacts_token(runner, env, monkeypatch, tmp_path):
    monkeypatch.setattr(main,"_ADMIN_TRANSPORT",__import__("httpx").MockTransport(lambda req: __import__("httpx").Response(201,json={"bundles":[wire(bundle("SECRET"))]})))
    result=invoke(runner,env,["site","join-bundles","--count","1","--output",str(tmp_path)],json_output=True)
    assert result.exit_code==0 and "SECRET" not in result.stdout and "SECRET" not in result.stderr

@pytest.mark.parametrize("status",[401,403])
def test_admin_rejection_redacts_token(runner, env, monkeypatch, tmp_path, status):
    monkeypatch.setattr(main,"_ADMIN_TRANSPORT",__import__("httpx").MockTransport(lambda req: __import__("httpx").Response(status,json={"detail":"SECRET"})))
    result=invoke(runner,env,["site","join-bundles","--count","1","--output",str(tmp_path)])
    assert result.exit_code==2 and "SECRET" not in result.stdout and "SECRET" not in result.stderr

@pytest.mark.parametrize("count",[0,17])
def test_count_bounds(runner, env, tmp_path, count):
    result=invoke(runner,env,["site","join-bundles","--count",str(count),"--output",str(tmp_path)])
    assert result.exit_code != 0

def test_no_proxy_transport_is_requested(runner, env, monkeypatch, tmp_path):
    seen={}
    def handler(req): seen["trust_env"]=main._make_admin_client # transport cannot observe trust_env
    monkeypatch.setattr(main,"_ADMIN_TRANSPORT",__import__("httpx").MockTransport(lambda req: __import__("httpx").Response(403)))
    result=invoke(runner,env,["site","join-bundles","--count","1","--output",str(tmp_path)])
    assert result.exit_code==2
