"""端到端 API 测试（通过 FastAPI TestClient）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post(payload):
    return client.post("/api/v1/precheck", json=payload)


# ---------------------------------------------------------------------------
# 安全 / 禁配
# ---------------------------------------------------------------------------
class TestForbidden:
    def test_safe_mix(self):
        r = post({
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "initial_liquid": [{"reagent_id": "nacl", "concentration": 5, "volume_ml": 100}],
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "koh", "concentration": 1, "volume_ml": 50},
                {"seq": 2, "op": "dilute", "volume_ml": 50, "at_seconds": 10},
            ],
        })
        assert r.status_code == 200
        d = r.json()
        assert d["safe"] is True and d["blocked"] is False
        assert d["findings"] == []

    def test_acid_plus_bleach_blocked_with_concentrations(self):
        r = post({
            "vessel_material": "glass",
            "temperature_c": 22,
            "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50},
            ],
        })
        d = r.json()
        assert d["blocked"] is True
        assert d["first_blocking_seq"] == 1
        f = d["findings"][0]
        assert f["rule_id"] == "F01"
        assert f["category"] == "forbidden_mix"
        assert set(f["entities"]) == {"hcl", "naocl"}
        assert f["concentrations"] == pytest.approx({"hcl": 10.0, "naocl": 6.6667}, abs=1e-3)
        step = d["steps"][0]
        assert step["liquid_volume_ml"] == 150.0

    def test_initial_liquid_triggers_seq_zero(self):
        r = post({
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "initial_liquid": [
                {"reagent_id": "naocl", "concentration": 12, "volume_ml": 50},
                {"reagent_id": "hcl", "concentration": 30, "volume_ml": 50},
            ],
            "steps": [],
        })
        d = r.json()
        assert d["first_blocking_seq"] == 0
        assert d["findings"][0]["seq"] == 0

    def test_trace_dilution_lowers_concentration(self):
        r = post({
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": 100},
                {"seq": 2, "op": "dilute", "volume_ml": 100, "at_seconds": 5},
            ],
        })
        comps = {c["reagent_id"]: c["concentration"] for c in r.json()["final_state"]["components"]}
        assert comps["nacl"] == pytest.approx(5.0)

    def test_threshold_below_min_concentration_does_not_trigger(self):
        # 3% 盐酸 100mL 混入 12% 漂白水 100mL -> 酸 1.5%（< 5% 阈值）、漂白水 6%
        r = post({
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 3, "volume_ml": 100},
                {"seq": 2, "op": "add", "reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
            ],
        })
        assert r.json()["safe"] is True

    def test_temperature_note_attached(self):
        r = post({
            "vessel_material": "hdpe",
            "temperature_c": 35,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naocl", "concentration": 10, "volume_ml": 100},
                {"seq": 2, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50},
            ],
        })
        assert "35℃" in r.json()["findings"][0]["blocking_reason"]


# ---------------------------------------------------------------------------
# 超时接触
# ---------------------------------------------------------------------------
class TestOvertime:
    BASE = {
        "vessel_material": "glass",
        "temperature_c": 20,
        "steps": [
            {"seq": 1, "op": "add", "reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
            {"seq": 2, "op": "add", "reagent_id": "nh4cl", "concentration": 20, "volume_ml": 100, "at_seconds": 60},
            {"seq": 3, "op": "hold", "at_seconds": 700},
        ],
    }

    def test_default_limit_exceeded(self):
        d = post(self.BASE).json()
        f = d["findings"][0]
        assert f["rule_id"] == "T02"
        assert f["category"] == "overtime_contact"
        assert f["seq"] == 3
        assert f["contact_seconds"] == pytest.approx(640.0)
        assert f["allowed_seconds"] == pytest.approx(600.0)

    def test_allowance_extends_limit(self):
        payload = {**self.BASE,
                   "contact_allowances": [{"a": "naocl", "b": "nh4cl", "max_contact_seconds": 3600}]}
        d = post(payload).json()
        assert d["safe"] is True

    def test_same_second_contact_no_overtime(self):
        payload = {
            "vessel_material": "glass",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
                {"seq": 2, "op": "add", "reagent_id": "nh4cl", "concentration": 20, "volume_ml": 100},
            ],
        }
        assert post(payload).json()["safe"] is True

    def test_allowance_with_material_prefix_form(self):
        payload = {
            "vessel_material": "glass",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naoh", "concentration": 30, "volume_ml": 100},
                {"seq": 2, "op": "hold", "at_seconds": 5000},
            ],
            "contact_allowances": [{"a": "naoh", "b": "material:glass", "max_contact_seconds": 9999}],
        }
        assert post(payload).json()["safe"] is True

    def test_pour_to_empty_resets_episode(self):
        payload = {
            "vessel_material": "glass",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naoh", "concentration": 30, "volume_ml": 100},
                {"seq": 2, "op": "hold", "at_seconds": 1000},
                {"seq": 3, "op": "pour", "volume_ml": 100, "at_seconds": 1000},
                {"seq": 4, "op": "add", "reagent_id": "naoh", "concentration": 30, "volume_ml": 100},
                {"seq": 5, "op": "hold", "at_seconds": 1500},
            ],
        }
        assert post(payload).json()["safe"] is True

    def test_dilution_below_threshold_ends_episode(self):
        payload = {
            "vessel_material": "glass",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naoh", "concentration": 30, "volume_ml": 100},
                {"seq": 2, "op": "dilute", "volume_ml": 1000, "at_seconds": 60},
                {"seq": 3, "op": "hold", "at_seconds": 4000},
            ],
        }
        assert post(payload).json()["safe"] is True


# ---------------------------------------------------------------------------
# 材质不相容
# ---------------------------------------------------------------------------
class TestMaterial:
    def test_hf_glass_immediate(self):
        d = post({
            "vessel_material": "glass",
            "temperature_c": 20,
            "steps": [{"seq": 1, "op": "add", "reagent_id": "hf", "concentration": 10, "volume_ml": 50}],
        }).json()
        f = d["findings"][0]
        assert f["rule_id"] == "M01"
        assert f["category"] == "material_incompatible"
        assert f["entities"] == ["hf", "material:glass"]

    def test_hf_hdpe_safe(self):
        d = post({
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "steps": [{"seq": 1, "op": "add", "reagent_id": "hf", "concentration": 10, "volume_ml": 50}],
        }).json()
        assert d["safe"] is True

    def test_strong_base_glass_timed(self):
        d = post({
            "vessel_material": "glass",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naoh", "concentration": 30, "volume_ml": 100},
                {"seq": 2, "op": "hold", "at_seconds": 2000},
            ],
        }).json()
        f = d["findings"][0]
        assert f["rule_id"] == "M04"
        assert f["category"] == "overtime_contact"
        assert f["allowed_seconds"] == 1800.0


# ---------------------------------------------------------------------------
# 清洗 / 倒出 / 稀释的状态改变
# ---------------------------------------------------------------------------
class TestStateChanges:
    def test_pour_scales_concentration_kept(self):
        d = post({
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": 100},
                {"seq": 2, "op": "pour", "volume_ml": 50, "at_seconds": 10},
            ],
        }).json()
        fs = d["final_state"]
        assert fs["liquid_volume_ml"] == 50
        assert fs["components"][0]["concentration"] == pytest.approx(10.0)

    def test_clean_resets_and_restart_is_safe(self):
        d = post({
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "naocl", "concentration": 10, "volume_ml": 100},
                {"seq": 2, "op": "clean", "at_seconds": 10},
                {"seq": 3, "op": "add", "reagent_id": "hcl", "concentration": 10, "volume_ml": 50, "at_seconds": 20},
            ],
        }).json()
        assert d["safe"] is True
        assert d["final_state"]["liquid_volume_ml"] == 50
        assert [c["reagent_id"] for c in d["final_state"]["components"]] == ["hcl"]

    @pytest.mark.parametrize("body", [
        {"op": "pour", "volume_ml": 10, "at_seconds": 20},
        {"op": "hold", "at_seconds": 20},
        {"op": "dilute", "volume_ml": 10, "at_seconds": 20},
    ])
    def test_stale_reference_after_clean_rejected(self, body):
        r = post({
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": 100},
                {"seq": 2, "op": "clean", "at_seconds": 10},
                {"seq": 3, **body},
            ],
        })
        assert r.status_code == 400
        codes = [e["code"] for e in r.json()["details"]]
        assert "STALE_STATE_REFERENCE" in codes


# ---------------------------------------------------------------------------
# 逐项拒绝
# ---------------------------------------------------------------------------
class TestRejections:
    def _codes(self, payload):
        r = post(payload)
        assert r.status_code == 400
        return r.json()["details"]

    def test_duplicate_step_seq(self):
        details = self._codes({
            "vessel_material": "glass", "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": 10},
                {"seq": 1, "op": "hold", "at_seconds": 5},
            ],
        })
        assert any(e["code"] == "DUPLICATE_STEP_SEQ" for e in details)

    @pytest.mark.parametrize("vol", [0, -3])
    def test_non_positive_volume(self, vol):
        details = self._codes({
            "vessel_material": "glass", "temperature_c": 20,
            "steps": [{"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": vol}],
        })
        assert any(e["code"] == "INVALID_VOLUME" for e in details)

    @pytest.mark.parametrize("conc", [0, -1, 101])
    def test_concentration_out_of_range(self, conc):
        details = self._codes({
            "vessel_material": "glass", "temperature_c": 20,
            "steps": [{"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": conc, "volume_ml": 10}],
        })
        assert any(e["code"] == "CONCENTRATION_OUT_OF_RANGE" for e in details)

    def test_reagent_specific_cap(self):
        details = self._codes({
            "vessel_material": "glass", "temperature_c": 20,
            "steps": [{"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 50, "volume_ml": 10}],
        })
        assert any(e["code"] == "CONCENTRATION_OUT_OF_RANGE" and "37" in e["message"] for e in details)

    def test_unknown_reagent_and_material(self):
        details = self._codes({
            "vessel_material": "kryptonite", "temperature_c": 20,
            "steps": [{"seq": 1, "op": "add", "reagent_id": "xyz", "concentration": 10, "volume_ml": 10}],
        })
        codes = {e["code"] for e in details}
        assert "UNKNOWN_MATERIAL" in codes and "UNKNOWN_REAGENT" in codes

    def test_multiple_errors_are_all_reported(self):
        details = self._codes({
            "vessel_material": "bad_mat", "temperature_c": 999,
            "initial_liquid": [{"reagent_id": "ghost", "concentration": 5, "volume_ml": 10}],
            "steps": [
                {"seq": 2, "op": "add", "reagent_id": "x", "concentration": 200, "volume_ml": 0},
                {"seq": 2, "op": "pour", "volume_ml": 10},
                {"seq": -1, "op": "hold", "at_seconds": -3},
            ],
        })
        codes = {e["code"] for e in details}
        assert {"UNKNOWN_MATERIAL", "TEMPERATURE_OUT_OF_RANGE", "UNKNOWN_REAGENT",
                "DUPLICATE_STEP_SEQ", "INVALID_SEQ", "INVALID_TIME"} <= codes

    def test_pour_exceeds_volume(self):
        details = self._codes({
            "vessel_material": "glass", "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": 10},
                {"seq": 2, "op": "pour", "volume_ml": 20},
            ],
        })
        assert any(e["code"] == "POUR_EXCEEDS_VOLUME" and e["seq"] == 2 for e in details)

    def test_non_monotonic_time(self):
        details = self._codes({
            "vessel_material": "glass", "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": 10, "at_seconds": 100},
                {"seq": 2, "op": "hold", "at_seconds": 50},
            ],
        })
        assert any(e["code"] == "NON_MONOTONIC_TIME" for e in details)

    def test_extra_fields_rejected(self):
        r = post({"vessel_material": "glass", "temperature_c": 20, "bogus": 1})
        assert r.status_code == 400
        assert r.json()["details"][0]["code"] == "MALFORMED_REQUEST"

    def test_duplicate_allowance_and_bad_entity(self):
        details = self._codes({
            "vessel_material": "glass", "temperature_c": 20,
            "steps": [{"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": 10}],
            "contact_allowances": [
                {"a": "hcl", "b": "glass", "max_contact_seconds": 10},
                {"a": "hcl", "b": "glass", "max_contact_seconds": 20},
                {"a": "nope", "b": "glass", "max_contact_seconds": 20},
                {"a": "nacl", "b": "nacl", "max_contact_seconds": 20},
            ],
        })
        codes = [e["code"] for e in details]
        assert "DUPLICATE_ALLOWANCE" in codes
        assert "UNKNOWN_ENTITY" in codes
        assert "INVALID_ALLOWANCE" in codes


# ---------------------------------------------------------------------------
# 排序与杂项
# ---------------------------------------------------------------------------
def test_steps_processed_by_seq_but_output_stable():
    d = post({
        "vessel_material": "hdpe", "temperature_c": 20,
        "steps": [
            {"seq": 3, "op": "hold", "at_seconds": 50},
            {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 10, "volume_ml": 10},
            {"seq": 2, "op": "add", "reagent_id": "koh", "concentration": 5, "volume_ml": 10},
        ],
    }).json()
    assert [t["seq"] for t in d["steps"]] == [1, 2, 3]


def test_health_and_catalog():
    assert client.get("/health").json()["status"] == "ok"
    cat = client.get("/catalog").json()
    assert {"hcl", "hf", "naocl"} <= {r["reagent_id"] for r in cat["reagents"]}
    assert {"glass", "hdpe", "ptfe"} <= {m["material_id"] for m in cat["materials"]}
    assert {r["rule_id"] for r in cat["rules"]} >= {"F01", "T02", "M01"}
