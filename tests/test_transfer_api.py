"""双容器转移预检端到端测试（通过 FastAPI TestClient）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post(payload):
    return client.post("/api/v1/transfer-precheck", json=payload)


def hdpe(vid, initial=None):
    return {"vessel_id": vid, "vessel_material": "hdpe", "initial_liquid": initial or []}


# ---------------------------------------------------------------------------
# 安全转移 / 体积簿记
# ---------------------------------------------------------------------------
class TestTransferBookkeeping:
    def test_proportional_split_keeps_concentration(self):
        d = post({
            "vessels": [
                hdpe("A", [{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}]),
                hdpe("B"),
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 40, "at_seconds": 10},
            ],
        }).json()
        states = {v["vessel_id"]: v for v in d["final_states"]}
        assert d["safe"] is True
        assert states["A"]["liquid_volume_ml"] == 60.0
        assert states["B"]["liquid_volume_ml"] == 40.0
        assert states["A"]["components"][0]["concentration"] == pytest.approx(10.0)
        assert states["B"]["components"][0]["concentration"] == pytest.approx(10.0)
        assert states["A"]["components"][0]["pure_volume_ml"] == pytest.approx(6.0)
        assert states["B"]["components"][0]["pure_volume_ml"] == pytest.approx(4.0)

    def test_into_existing_liquid_redistributes_pure_volume(self):
        # 30% HCl 100mL 转入 10% 漂白水 100mL（仅核体积簿记，规则另测）
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "nacl", "concentration": 20, "volume_ml": 100}]},
                {"vessel_id": "B", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "koh", "concentration": 2, "volume_ml": 100}]},
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 50, "at_seconds": 5},
            ],
        }).json()
        b = {v["vessel_id"]: v for v in d["final_states"]}["B"]
        assert b["liquid_volume_ml"] == 150.0
        comps = {c["reagent_id"]: c for c in b["components"]}
        assert comps["nacl"]["pure_volume_ml"] == pytest.approx(10.0)
        assert comps["nacl"]["concentration"] == pytest.approx(10 / 150 * 100, abs=1e-3)
        assert comps["koh"]["pure_volume_ml"] == pytest.approx(2.0)
        assert comps["koh"]["concentration"] == pytest.approx(2 / 150 * 100, abs=1e-3)

    def test_back_and_forth_transfers(self):
        d = post({
            "vessels": [
                hdpe("A", [{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}]),
                hdpe("B"),
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 60, "at_seconds": 1},
                {"seq": 2, "op": "transfer", "from_vessel": "B", "to_vessel": "A", "volume_ml": 20, "at_seconds": 2},
            ],
        }).json()
        states = {v["vessel_id"]: v for v in d["final_states"]}
        assert states["A"]["liquid_volume_ml"] == pytest.approx(60.0)
        assert states["B"]["liquid_volume_ml"] == pytest.approx(40.0)
        for s in states.values():
            assert s["components"][0]["concentration"] == pytest.approx(10.0)

    def test_same_second_steps_execute_in_seq_order(self):
        d = post({
            "vessels": [
                hdpe("A", [{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}]),
                hdpe("B"),
            ],
            "temperature_c": 20,
            "steps": [
                # 乱序提交但同刻：必须按 seq 执行。seq1 先把 A 全部转入 B（A 变空），
                # seq2 再从空 A 转出 -> 拒绝
                {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 1, "at_seconds": 5},
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 100, "at_seconds": 5},
            ],
        })
        assert d.status_code == 400
        assert any(e["code"] == "EMPTY_SOURCE_VESSEL" and e["seq"] == 2 for e in d.json()["details"])

    def test_trace_shape(self):
        d = post({
            "vessels": [
                hdpe("A", [{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}]),
                hdpe("B"),
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 40, "at_seconds": 10},
            ],
        }).json()
        step = d["steps"][0]
        assert step["from_vessel"] == "A" and step["to_vessel"] == "B" and step["volume_ml"] == 40
        assert {v["vessel_id"] for v in step["vessels"]} == {"A", "B"}
        assert d["first_blocking_vessel_id"] is None
        assert [v["vessel_id"] for v in d["final_states"]] == ["A", "B"]


# ---------------------------------------------------------------------------
# 即时禁配 / 材质风险
# ---------------------------------------------------------------------------
class TestImmediateRisks:
    def test_forbidden_mix_forms_in_destination(self):
        d = post({
            "vessels": [
                {"vessel_id": "src", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}]},
                {"vessel_id": "dst", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}]},
            ],
            "temperature_c": 22,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "src", "to_vessel": "dst", "volume_ml": 50, "at_seconds": 5},
            ],
        }).json()
        assert d["blocked"] is True
        assert d["first_blocking_seq"] == 1
        assert d["first_blocking_vessel_id"] == "dst"
        f = d["findings"][0]
        assert f["rule_id"] == "F01" and f["vessel_id"] == "dst"
        assert f["concentrations"] == pytest.approx({"hcl": 10.0, "naocl": 6.6667}, abs=1e-3)
        assert "dst" in f["blocking_reason"]

    def test_source_emptying_does_not_fire_existing_pair_again(self):
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [
                     {"reagent_id": "hcl", "concentration": 30, "volume_ml": 100},
                     {"reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
                 ]},
                hdpe("B"),
            ],
            "temperature_c": 22,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 100, "at_seconds": 5},
            ],
        }).json()
        # 初始即禁配（seq=0）；部分转出浓度不变，不重复报；B 承接后首次共存再报一次
        fired = [(f["seq"], f["vessel_id"]) for f in d["findings"]]
        assert fired == [(0, "A"), (1, "B")]

    def test_material_incompatible_at_destination_on_transfer_in(self):
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "hf", "concentration": 10, "volume_ml": 50}]},
                {"vessel_id": "B", "vessel_material": "glass", "initial_liquid": []},
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 50, "at_seconds": 5},
            ],
        }).json()
        f = d["findings"][0]
        assert f["rule_id"] == "M01" and f["vessel_id"] == "B"
        assert f["category"] == "material_incompatible"

    def test_initial_state_checked_in_both_vessels(self):
        d = post({
            "vessels": [
                hdpe("A"),
                {"vessel_id": "B", "vessel_material": "glass",
                 "initial_liquid": [{"reagent_id": "hf", "concentration": 10, "volume_ml": 50}]},
            ],
            "temperature_c": 20,
            "steps": [],
        }).json()
        assert d["first_blocking_seq"] == 0
        assert d["first_blocking_vessel_id"] == "B"

    def test_initial_forbidden_pair_in_both_vessels(self):
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [
                     {"reagent_id": "naocl", "concentration": 12, "volume_ml": 50},
                     {"reagent_id": "hcl", "concentration": 30, "volume_ml": 50},
                 ]},
                {"vessel_id": "B", "vessel_material": "hdpe",
                 "initial_liquid": [
                     {"reagent_id": "naocl", "concentration": 12, "volume_ml": 50},
                     {"reagent_id": "hcl", "concentration": 30, "volume_ml": 50},
                 ]},
            ],
            "temperature_c": 20,
            "steps": [],
        }).json()
        assert [(f["seq"], f["vessel_id"]) for f in d["findings"]] == [(0, "A"), (0, "B")]

    def test_below_threshold_transfer_is_safe(self):
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "hcl", "concentration": 3, "volume_ml": 100}]},
                {"vessel_id": "B", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "naocl", "concentration": 12, "volume_ml": 100}]},
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 100, "at_seconds": 5},
            ],
        }).json()
        assert d["safe"] is True


# ---------------------------------------------------------------------------
# 超时接触与转移的交互
# ---------------------------------------------------------------------------
class TestOvertimeTransfer:
    def test_ongoing_episode_continues_in_source(self):
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [
                     {"reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
                     {"reagent_id": "nh4cl", "concentration": 20, "volume_ml": 100},
                 ]},
                hdpe("B"),
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "hold", "at_seconds": 400},
                {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 100, "at_seconds": 500},
                {"seq": 3, "op": "hold", "at_seconds": 900},
            ],
        }).json()
        f = d["findings"][0]
        assert f["rule_id"] == "T02" and f["vessel_id"] == "A"
        assert f["seq"] == 3 and f["contact_seconds"] == pytest.approx(900.0)
        assert f["allowed_seconds"] == pytest.approx(600.0)

    def test_new_episode_starts_at_transfer_in(self):
        payload = {
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "naocl", "concentration": 12, "volume_ml": 100}]},
                {"vessel_id": "B", "vessel_material": "hdpe",
                 "initial_liquid": [{"reagent_id": "nh4cl", "concentration": 20, "volume_ml": 100}]},
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "hold", "at_seconds": 2000},
                {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 100, "at_seconds": 2000},
                {"seq": 3, "op": "hold", "at_seconds": 2500},
            ],
        }
        assert post(payload).json()["safe"] is True
        payload["steps"][2]["at_seconds"] = 2700
        d = post(payload).json()
        f = d["findings"][0]
        assert f["rule_id"] == "T02" and f["vessel_id"] == "B"
        assert f["contact_seconds"] == pytest.approx(700.0)

    def test_allowance_covers_both_vessels(self):
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [
                     {"reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
                     {"reagent_id": "nh4cl", "concentration": 20, "volume_ml": 100},
                 ]},
                hdpe("B"),
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 100, "at_seconds": 100},
                {"seq": 2, "op": "hold", "at_seconds": 800},
            ],
            "contact_allowances": [{"a": "naocl", "b": "nh4cl", "max_contact_seconds": 3600}],
        }).json()
        assert d["safe"] is True

    def test_material_episode_continues_across_transfer_out(self):
        # 30% NaOH 在玻璃 A 中接触 1000s，转出一半到玻璃 B；A 侧计时延续
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "glass",
                 "initial_liquid": [{"reagent_id": "naoh", "concentration": 30, "volume_ml": 100}]},
                {"vessel_id": "B", "vessel_material": "glass", "initial_liquid": []},
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "hold", "at_seconds": 1000},
                {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 50, "at_seconds": 1000},
                {"seq": 3, "op": "hold", "at_seconds": 1500},
            ],
        }).json()
        overtime = {(f["vessel_id"], round(f["contact_seconds"])) for f in d["findings"]}
        # A 侧片段自 0 起：1500s 不超时；B 侧片段自 1000 起：500s 不超时
        assert d["safe"] is True
        assert overtime == set()

    def test_material_episode_times_out_in_aged_source(self):
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "glass",
                 "initial_liquid": [{"reagent_id": "naoh", "concentration": 30, "volume_ml": 100}]},
                {"vessel_id": "B", "vessel_material": "glass", "initial_liquid": []},
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "hold", "at_seconds": 1000},
                {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 50, "at_seconds": 1000},
                {"seq": 3, "op": "hold", "at_seconds": 2000},
            ],
        }).json()
        overtime = {(f["vessel_id"], round(f["contact_seconds"])) for f in d["findings"]}
        assert ("A", 2000.0) in overtime      # A 片段自 t=0 起
        assert ("B", 1000.0) not in overtime  # B 片段自 t=1000 起，仅 1000s

    def test_full_transfer_ends_source_episode(self):
        # 转空结束玻璃 A 的接触片段；液体经 HDPE B 转回玻璃 A 后作为全新片段重新计时
        def run(last_at):
            return post({
                "vessels": [
                    {"vessel_id": "A", "vessel_material": "glass",
                     "initial_liquid": [{"reagent_id": "naoh", "concentration": 30, "volume_ml": 100}]},
                    {"vessel_id": "B", "vessel_material": "hdpe", "initial_liquid": []},
                ],
                "temperature_c": 20,
                "steps": [
                    {"seq": 1, "op": "hold", "at_seconds": 1700},
                    {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 100, "at_seconds": 1700},
                    {"seq": 3, "op": "transfer", "from_vessel": "B", "to_vessel": "A", "volume_ml": 100, "at_seconds": 1800},
                    {"seq": 4, "op": "hold", "at_seconds": last_at},
                ],
            }).json()

        # 旧片段 1700s（未超 1800）随转空结束；新片段自 t=1800 起，到 t=2000 仅 200s
        d = run(2000)
        assert [f for f in d["findings"] if f["vessel_id"] == "A"] == []
        # 新片段持续到 t=4000（2200s）才超时，证明计时确实被重置
        d = run(4000)
        a_finding = [f for f in d["findings"] if f["vessel_id"] == "A" and f["seq"] == 4]
        assert len(a_finding) == 1
        assert a_finding[0]["contact_seconds"] == pytest.approx(2200.0)

    def test_overtime_settled_before_operation_each_step(self):
        # 每步先结算此前超时：超时 finding 归属于 hold 时刻而非后续 transfer
        d = post({
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe",
                 "initial_liquid": [
                     {"reagent_id": "naocl", "concentration": 12, "volume_ml": 100},
                     {"reagent_id": "nh4cl", "concentration": 20, "volume_ml": 100},
                 ]},
                hdpe("B"),
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "hold", "at_seconds": 601},
                {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 10, "at_seconds": 601},
            ],
        }).json()
        f = d["findings"][0]
        assert f["seq"] == 1


# ---------------------------------------------------------------------------
# 逐项拒绝
# ---------------------------------------------------------------------------
class TestTransferRejections:
    BASE = {
        "vessels": [
            hdpe("A", [{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}]),
            hdpe("B"),
        ],
        "temperature_c": 20,
    }

    def _codes(self, payload):
        r = post(payload)
        assert r.status_code == 400, r.text
        return r.json()["details"]

    def test_unknown_source_and_destination(self):
        p = {**self.BASE, "steps": [
            {"seq": 1, "op": "transfer", "from_vessel": "X", "to_vessel": "A", "volume_ml": 10},
            {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "Y", "volume_ml": 10, "at_seconds": 5},
        ]}
        details = self._codes(p)
        assert {e["code"] for e in details} == {"UNKNOWN_VESSEL"}
        assert {e["field"] for e in details} == {"from_vessel", "to_vessel"}

    def test_same_vessel(self):
        p = {**self.BASE, "steps": [
            {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "A", "volume_ml": 10},
        ]}
        assert any(e["code"] == "SAME_VESSEL" for e in self._codes(p))

    def test_empty_source_vessel(self):
        p = {**self.BASE, "steps": [
            {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 100},
            {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 1, "at_seconds": 5},
        ]}
        details = self._codes(p)
        assert any(e["code"] == "EMPTY_SOURCE_VESSEL" and e["seq"] == 2 for e in details)

    @pytest.mark.parametrize("vol", [0, -5])
    def test_non_positive_volume(self, vol):
        p = {**self.BASE, "steps": [
            {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": vol},
        ]}
        assert any(e["code"] == "INVALID_VOLUME" for e in self._codes(p))

    def test_transfer_exceeds_volume(self):
        p = {**self.BASE, "steps": [
            {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 150},
        ]}
        details = self._codes(p)
        assert any(e["code"] == "TRANSFER_EXCEEDS_VOLUME" and "150" in e["message"] for e in details)

    def test_time_rollback(self):
        p = {**self.BASE, "steps": [
            {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 10, "at_seconds": 100},
            {"seq": 2, "op": "hold", "at_seconds": 50},
        ]}
        assert any(e["code"] == "NON_MONOTONIC_TIME" for e in self._codes(p))

    def test_hold_both_empty_rejected(self):
        p = {
            "vessels": [hdpe("A"), hdpe("B")],
            "temperature_c": 20,
            "steps": [{"seq": 1, "op": "hold", "at_seconds": 10}],
        }
        assert any(e["code"] == "EMPTY_VESSEL_OPERATION" for e in self._codes(p))

    def test_hold_one_vessel_has_liquid_allowed(self):
        p = {**self.BASE, "steps": [{"seq": 1, "op": "hold", "at_seconds": 10}]}
        assert post(p).status_code == 200

    def test_duplicate_vessel_id(self):
        p = {"vessels": [hdpe("A"), hdpe("A")], "temperature_c": 20, "steps": []}
        assert any(e["code"] == "DUPLICATE_VESSEL_ID" for e in self._codes(p))

    @pytest.mark.parametrize("n", [0, 1, 3])
    def test_vessel_count_must_be_two(self, n):
        p = {"vessels": [hdpe(f"A{i}") for i in range(n)], "temperature_c": 20, "steps": []}
        assert any(e["code"] == "INVALID_VESSEL" for e in self._codes(p))

    def test_duplicate_step_seq(self):
        p = {**self.BASE, "steps": [
            {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 10},
            {"seq": 1, "op": "hold", "at_seconds": 50},
        ]}
        assert any(e["code"] == "DUPLICATE_STEP_SEQ" for e in self._codes(p))

    def test_unknown_material_and_reagent_still_rejected(self):
        p = {
            "vessels": [
                {"vessel_id": "A", "vessel_material": "kryptonite",
                 "initial_liquid": [{"reagent_id": "ghost", "concentration": 5, "volume_ml": 10}]},
                hdpe("B"),
            ],
            "temperature_c": 20,
            "steps": [],
        }
        codes = {e["code"] for e in self._codes(p)}
        assert {"UNKNOWN_MATERIAL", "UNKNOWN_REAGENT"} <= codes

    def test_extra_field_rejected(self):
        p = {**self.BASE, "steps": [
            {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 10, "bogus": 1},
        ]}
        r = post(p)
        assert r.status_code == 400
        assert r.json()["details"][0]["code"] == "MALFORMED_REQUEST"

    def test_hold_with_transfer_fields_rejected(self):
        p = {**self.BASE, "steps": [
            {"seq": 1, "op": "hold", "at_seconds": 10, "from_vessel": "A"},
        ]}
        assert any(e["code"] == "INVALID_STEP_FIELDS" for e in self._codes(p))

    def test_no_partial_simulation_on_rejection(self):
        r = post({**self.BASE, "steps": [
            {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B", "volume_ml": 10},
            {"seq": 2, "op": "transfer", "from_vessel": "B", "to_vessel": "A", "volume_ml": 9999, "at_seconds": 5},
        ]})
        assert r.status_code == 400
        body = r.json()
        assert "findings" not in body and "steps" not in body

    def test_multiple_distinct_errors_all_reported(self):
        p = {
            "vessels": [
                {"vessel_id": "A", "vessel_material": "bad_mat",
                 "initial_liquid": [{"reagent_id": "ghost", "concentration": 5, "volume_ml": 10}]},
                {"vessel_id": "B", "vessel_material": "hdpe", "initial_liquid": []},
            ],
            "temperature_c": 999,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "B", "to_vessel": "B", "volume_ml": 0},
            ],
        }
        codes = {e["code"] for e in self._codes(p)}
        assert {"UNKNOWN_MATERIAL", "UNKNOWN_REAGENT", "TEMPERATURE_OUT_OF_RANGE",
                "SAME_VESSEL", "INVALID_VOLUME"} <= codes

    def test_duplicate_vessel_id_blocks_stateflow_cascades(self):
        # 容器编号重复时不再级联产生 UNKNOWN_VESSEL（顶层问题逐项报，不返回部分模拟）
        p = {
            "vessels": [
                {"vessel_id": "A", "vessel_material": "hdpe", "initial_liquid": []},
                {"vessel_id": "A", "vessel_material": "hdpe", "initial_liquid": []},
            ],
            "temperature_c": 20,
            "steps": [
                {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "A", "volume_ml": 10},
            ],
        }
        codes = [e["code"] for e in self._codes(p)]
        assert "DUPLICATE_VESSEL_ID" in codes
        assert "UNKNOWN_VESSEL" not in codes


# ---------------------------------------------------------------------------
# 兼容：旧端点行为不变
# ---------------------------------------------------------------------------
def test_legacy_endpoint_unchanged():
    d = client.post("/api/v1/precheck", json={
        "vessel_material": "glass",
        "temperature_c": 22,
        "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
        "steps": [{"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 30, "volume_ml": 50}],
    }).json()
    assert set(d.keys()) == {
        "safe", "blocked", "first_blocking_seq", "ruleset_version", "vessel_material",
        "temperature_c", "findings", "final_state", "steps",
    }
    assert "vessel_id" not in d["findings"][0]
