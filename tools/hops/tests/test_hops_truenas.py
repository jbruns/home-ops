"""TrueNAS: the JSON-RPC client (hops.truenas) and `hops truenas call`, against FakeTrueNAS."""
import json

import pytest
from hops_fakes import FakeTrueNAS, FakeTrueNASError, write

from hops import cli, truenas
from hops.errors import ConfigError, HopsError

KEY = FakeTrueNAS.KEY
CFG = truenas.TrueNASConfig(url="wss://tn.example/api/current", api_key=KEY)


@pytest.fixture
def server() -> FakeTrueNAS:
    return FakeTrueNAS({"system.info": lambda: {"version": "25.10.4", "hostname": "tn"}})


@pytest.fixture
def sleeps() -> list:
    return []


@pytest.fixture
def client(server, sleeps):
    return truenas.Client(CFG, connect=server.connect, sleep=sleeps.append)


@pytest.fixture
def hops_tn(repo, client, capsys):
    """Run the CLI with the fake TrueNAS: hops_tn("truenas", "call", "system.info")."""
    def run(*argv):
        code = cli.main(list(argv), ctx=cli.Context(repo_root=repo, truenas=client))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


# Client

def test_call_logs_in_with_the_key_then_calls(client, server):
    assert client.call("system.info") == {"version": "25.10.4", "hostname": "tn"}
    assert server.connects == [{"url": "wss://tn.example/api/current", "verify": True, "timeout": 30.0}]
    assert [(r["method"], r["params"]) for r in server.requests] == [
        ("auth.login_with_api_key", [KEY]), ("system.info", [])]
    assert all(r["jsonrpc"] == "2.0" for r in server.requests)

    client.call("system.info")
    assert server.called() == ["auth.login_with_api_key", "system.info", "system.info"]  # one login


def test_call_skips_notifications(client, server):
    server.notifications = [{"jsonrpc": "2.0", "method": "collection_update", "params": {"msg": "added"}}]
    assert client.call("system.info")["version"] == "25.10.4"


def test_rejected_key_fails_without_showing_it(server):
    server.methods["auth.login_with_api_key"] = lambda key: False
    with pytest.raises(HopsError, match="rejected HOPS_TRUENAS_API_KEY") as exc:
        truenas.Client(CFG, connect=server.connect).call("system.info")
    assert KEY not in str(exc.value)
    assert server.closed


def test_method_error_names_the_errno_and_hides_the_key(client, server):
    def denied(*params):
        raise FakeTrueNASError("EACCES", f"Not authorized (key {KEY})")

    server.methods["pool.snapshot.create"] = denied
    with pytest.raises(truenas.TrueNASError) as exc:
        client.call("pool.snapshot.create", {"dataset": "tank/a", "name": "s"})
    assert exc.value.errname == "EACCES"
    assert str(exc.value) == "TrueNAS pool.snapshot.create: EACCES: Not authorized (key [REDACTED])"


def test_job_waits_for_the_result(client, server, sleeps):
    states = iter(["WAITING", "RUNNING", "SUCCESS"])
    server.methods["cloudsync.sync"] = lambda task_id: 41
    server.methods["core.get_jobs"] = lambda filters, options: {
        "id": filters[0][2], "state": next(states), "result": "done", "error": None}

    assert client.job("cloudsync.sync", 3) == "done"
    polls = [r["params"] for r in server.requests if r["method"] == "core.get_jobs"]
    assert polls == [[[["id", "=", 41]], {"get": True}]] * 3
    assert len(sleeps) == 2


def test_failed_job_raises_its_error(client, server):
    server.methods["cloudsync.sync"] = lambda task_id: 41
    server.methods["core.get_jobs"] = lambda filters, options: {
        "id": 41, "state": "FAILED", "result": None, "error": "bucket not found"}
    with pytest.raises(truenas.TrueNASError, match="cloudsync.sync: job 41 failed: bucket not found"):
        client.job("cloudsync.sync", 3)


# Config

def test_config_reads_url_and_key_from_dotenv(tmp_path):
    write(tmp_path, {".env": f"TRUENAS_URL=https://tn.example:8443/ui/\nHOPS_TRUENAS_API_KEY={KEY}\n"})
    cfg = truenas.load_config(tmp_path, env={})
    assert (cfg.url, cfg.api_key, cfg.verify) == ("wss://tn.example:8443/api/current", KEY, True)
    assert KEY not in repr(cfg)


def test_insecure_tls_is_an_explicit_opt_in(tmp_path):
    env = {"TRUENAS_URL": "https://tn.example", "HOPS_TRUENAS_API_KEY": KEY, "HOPS_TRUENAS_INSECURE": "1"}
    assert truenas.load_config(tmp_path, env=env).verify is False


def test_without_truenas_config_it_is_not_available(tmp_path):
    with pytest.raises(ConfigError, match="not available on this host: missing HOPS_TRUENAS_API_KEY"):
        truenas.load_config(tmp_path, env={"TRUENAS_URL": "https://tn.example"})


def test_plain_http_url_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="https"):
        truenas.load_config(tmp_path, env={"TRUENAS_URL": "http://tn.example", "HOPS_TRUENAS_API_KEY": KEY})


# hops truenas call

def test_call_prints_the_result_as_json(hops_tn):
    code, out, err = hops_tn("truenas", "call", "system.info", "--json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {"version": "25.10.4", "hostname": "tn"}


def test_call_passes_json_params(hops_tn, server):
    server.methods["pool.query"] = lambda filters, options: [{"name": "tank"}]
    code, out, _ = hops_tn("truenas", "call", "pool.query", '[["name","=","tank"]]', '{"select":["name"]}')
    assert code == 0
    assert server.requests[-1]["params"] == [[["name", "=", "tank"]], {"select": ["name"]}]


def test_bad_json_param_is_a_usage_error(hops_tn, server):
    code, _, err = hops_tn("truenas", "call", "pool.query", "[name]")
    assert code == 2 and "not JSON" in err
    assert server.requests == []


@pytest.mark.parametrize("method", [
    "pool.snapshot.create", "pool.snapshot.delete", "cloudsync.sync", "api_key.create", "api_key.delete",
    "api_key.update", "system.reboot", "auth.login_with_api_key", "core.get_jobs", "pool.query.x",
    "../pool.query",
])
def test_call_refuses_anything_but_a_read(hops_tn, server, method):
    code, out, err = hops_tn("truenas", "call", method, "{}")
    assert code == 2 and out == ""
    assert "not a read method" in err
    assert server.connects == []  # refused before connecting


def test_call_redacts_credentials(hops_tn, server):
    server.methods["cloudsync.query"] = lambda: [{
        "id": 1, "path": "/mnt/tank/a", "encryption_password": "pw", "encryption_salt": "salt",
        "credentials": {"id": 2, "provider": {"type": "B2", "account": "acct", "key": "k"}},
        "attributes": {"bucket": "b", "folder": "/x"},
        "args": "", "api_key": None,
    }]
    server.methods["cloudsync.credentials.query"] = lambda: [
        {"id": 2, "name": "b2", "provider": {"type": "B2", "account": "acct", "key": "k"}}]

    _, out, _ = hops_tn("truenas", "call", "cloudsync.query", "--json")
    assert json.loads(out) == [{
        "id": 1, "path": "/mnt/tank/a", "encryption_password": "[REDACTED]", "encryption_salt": "[REDACTED]",
        "credentials": "[REDACTED]", "attributes": {"bucket": "b", "folder": "/x"},
        "args": "", "api_key": None,
    }]
    _, out, _ = hops_tn("truenas", "call", "cloudsync.credentials.query", "--json")
    assert json.loads(out) == [
        {"id": 2, "name": "b2", "provider": {"type": "B2", "account": "[REDACTED]", "key": "[REDACTED]"}}]


@pytest.mark.parametrize("method, result, shown", [
    ("ups.config", {"monuser": "upsmon", "monpwd": "pw"}, {"monuser": "upsmon", "monpwd": "[REDACTED]"}),
    ("mail.config", {"user": "me", "pass": "pw"}, {"user": "me", "pass": "[REDACTED]"}),
    ("kerberos.keytab.query", [{"name": "k", "file": "a2V5"}], [{"name": "k", "file": "[REDACTED]"}]),
])
def test_call_redacts_secrets_with_terse_names(hops_tn, server, method, result, shown):
    server.methods[method] = lambda: result
    _, out, _ = hops_tn("truenas", "call", method)
    assert json.loads(out) == shown


def test_call_without_truenas_config_fails_clearly(repo, capsys):
    ctx = cli.Context(repo_root=repo, env={"HOME": "/h"})
    assert cli.main(["truenas", "call", "system.info"], ctx=ctx) == 2
    assert "TrueNAS is not available on this host" in capsys.readouterr().err


@pytest.mark.parametrize("params", [
    ['[]', '{"select": [["provider.key", "name"]]}'],  # alias: would print the key as `name`
    ['[]', '{"select": ["id", ["provider.key", "name"]]}'],
    ['[]', '{"select": [["name"]]}'],
    ['[]', '{"selct": ["name"]}'],  # not a query option: fail closed
    ['2', '{"select": [["provider.key", "name"]]}'],
])
def test_call_refuses_query_options_that_rename_fields(hops_tn, server, params):
    code, out, err = hops_tn("truenas", "call", "cloudsync.credentials.query", *params)
    assert code == 2 and out == ""
    assert "query option" in err
    assert server.connects == []


def test_call_allows_plain_select_and_redacts_by_source_field(hops_tn, server):
    server.methods["cloudsync.credentials.query"] = lambda filters, options: [
        {"provider": {"key": "k"}, "name": "b2"}]
    code, out, _ = hops_tn("truenas", "call", "cloudsync.credentials.query", "[]",
                           '{"select": ["name", "provider.key"], "order_by": ["name"], "limit": 1}')
    assert code == 0
    assert json.loads(out) == [{"provider": {"key": "[REDACTED]"}, "name": "b2"}]


def test_call_redacts_userinfo_in_any_url(hops_tn, server):
    server.methods["network.configuration.config"] = lambda: {
        "httpproxy": "http://user:p%40ss@proxy.lan:3128",
        "nested": [{"mirrors": ["https://bob:pw@reg.example/v2", "https://reg.example/v2"]}],
        "note": "see https://a@b.example and ftp://x:y@z/",
    }
    _, out, _ = hops_tn("truenas", "call", "network.configuration.config")
    assert json.loads(out) == {
        "httpproxy": "http://[REDACTED]@proxy.lan:3128",
        "nested": [{"mirrors": ["https://[REDACTED]@reg.example/v2", "https://reg.example/v2"]}],
        "note": "see https://[REDACTED]@b.example and ftp://[REDACTED]@z/",
    }


@pytest.mark.parametrize("method", ["app.query", "app.config", "app.get_instance", "virt.instance.query",
                                    "virt.global.config", "vm.query", "vm.device.query"])
def test_call_refuses_methods_with_free_form_config(hops_tn, server, method):
    code, out, err = hops_tn("truenas", "call", method)
    assert code == 2 and out == ""
    assert "free-form" in err
    assert server.connects == []


def test_url_with_userinfo_is_refused_without_echoing_it(tmp_path):
    with pytest.raises(ConfigError) as exc:
        truenas.load_config(tmp_path, env={"TRUENAS_URL": "https://admin:hunter2@tn.example",
                                           "HOPS_TRUENAS_API_KEY": KEY})
    assert "user" in str(exc.value) and "hunter2" not in str(exc.value) and "admin" not in str(exc.value)
