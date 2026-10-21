"""The topology editor's HTTP seam: predicted links come from the medium, saves round-trip."""

import json
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from esp_netsim.config.schema import load_scenario
from esp_netsim.editor import serve

SCENARIO = """# header comment kept on save
name: ed
nodes:
  - { id: 1, mac: "aa:bb:cc:00:00:01", pos: [0, 0] }
  - { id: 2, mac: "aa:bb:cc:00:00:02", pos: [20, 0] }
  - { id: 3, mac: "aa:bb:cc:00:00:03", pos: [5000, 0] }
run:
  seed: 4
traffic:
  - { node: 1, at_us: 0 }
"""


@pytest.fixture
def editor(tmp_path):
    path = tmp_path / "ed.yaml"
    path.write_text(SCENARIO)
    server = serve(path, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield path, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _post(url: str, doc: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, json.dumps(doc).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_links_follow_distance(editor):
    path, url = editor
    status, body = _post(url + "/links", yaml.safe_load(SCENARIO))
    assert status == 200
    pairs = {(l["a"], l["b"]): l for l in body["links"]}
    assert pairs[(1, 2)]["pdr"] > 0.99 and pairs[(1, 2)]["rssi_dbm"] < -40
    assert not any(3 in p for p in pairs)  # 5 km away hears nobody


def test_save_moves_node_and_keeps_header_and_other_sections(editor):
    path, url = editor
    doc = yaml.safe_load(SCENARIO)
    doc["nodes"][2]["pos"] = [30, 0]
    status, body = _post(url + "/save", doc)
    assert status == 200 and any(l["b"] == 3 for l in body["links"])
    text = path.read_text()
    assert text.startswith("# header comment kept on save\n")
    sc = load_scenario(str(path))
    assert (sc.nodes[2].x, sc.seed, len(sc.traffic)) == (30.0, 4, 1)


def test_invalid_scenario_is_rejected_and_file_untouched(editor):
    path, url = editor
    doc = yaml.safe_load(SCENARIO)
    doc["nodes"][1]["mac"] = doc["nodes"][0]["mac"]
    status, body = _post(url + "/save", doc)
    assert status == 400 and "duplicate" in body["error"]
    assert path.read_text() == SCENARIO
