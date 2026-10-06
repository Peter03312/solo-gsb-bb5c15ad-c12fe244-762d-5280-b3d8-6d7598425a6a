"""双容器转移预检（/api/v2/transfer-precheck）端到端测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PATH = "/api/v2/transfer-precheck"


def post(payload):
    return client.post(PATH, json=payload)


def two_vessels(a_init=None, b_init=None, *, a_mat="hdpe", b_mat="hdpe", steps=None, temp=20, allowances=None):
    return {
        "vessels": [
            {"vessel_id": "A", "material": a_mat, "initial_liquid": a_init or []},
            {"vessel_id": "B", "material": b_mat, "initial_liquid": b_init or []},
        ],
        "temperature_c": temp,
        "steps": steps or [],
        "contact_allowances": allowances or [],
    }


def by_id(states):
    return {v["vessel_id"]: v for v in states}


# ---------------------------------------------------------------------------
# 转移簿记：源按比例扣减、目标计入、充分混合
# ---------------------------------------------------------------------------
class TestTransferBookkeeping:
    def test_safe_transfer_proportional(self):
        d = post(two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            b_init=[{"reagent_id": "koh", "concentration": 2, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 50, "at_seconds": 0}],
        )).json()
        assert d["safe"] is True and d["blocked"] is False
        fin = by_id(d["final_states"])
        # 源：倒出一半，浓度不变
        assert fin["A"]["liquid_volume_ml"] == 50.0
        a_comp = {c["reagent_id"]: c for c in fin["A"]["components"]}
        assert a_comp["nacl"]["concentration"] == pytest.approx(10.0)
        assert a_comp["nacl"]["pure_volume_ml"] == pytest.approx(5.0)
        # 目标：纯物量计入，充分混合
        assert fin["B"]["liquid_volume_ml"] == 150.0
        b_comp = {c["reagent_id"]: c for c in fin["B"]["components"]}
        assert b_comp["nacl"]["concentration"] == pytest.approx(5.0 / 150 * 100, abs=1e-3)
        assert b_comp["nacl"]["pure_volume_ml"] == pytest.approx(5.0)
        assert b_comp["koh"]["concentration"] == pytest.approx(2.0 / 150 * 100, abs=1e-3)

    def test_transfer_into_empty_vessel(self):
        d = post(two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 40, "at_seconds": 0}],
        )).json()
        fin = by_id(d["final_states"])
        assert fin["A"]["liquid_volume_ml"] == 60.0
        assert fin["B"]["liquid_volume_ml"] == 40.0
        assert fin["B"]["components"][0]["concentration"] == pytest.approx(10.0)

    def test_full_transfer_empties_source(self):
        d = post(two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 100, "at_seconds": 0},
                   {"seq": 2, "op": "transfer", "from_vessel": "B", "to_vessel": "A",
                    "volume_ml": 100, "at_seconds": 0}],
        )).json()
        fin = by_id(d["final_states"])
        assert fin["A"]["liquid_volume_ml"] == 100.0
        assert fin["B"]["liquid_volume_ml"] == 0.0
        assert fin["B"]["components"] == []

    def test_same_reagent_merges_pure_amount(self):
        d = post(two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            b_init=[{"reagent_id": "nacl", "concentration": 20, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 100, "at_seconds": 0}],
        )).json()
        b = by_id(d["final_states"])["B"]
        comp = {c["reagent_id"]: c for c in b["components"]}
        assert set(comp) == {"nacl"}
        # (10 + 20) / 200 = 15%
        assert comp["nacl"]["concentration"] == pytest.approx(15.0)
        assert comp["nacl"]["pure_volume_ml"] == pytest.approx(30.0)

    def test_step_trace_carries_both_vessels_each_step(self):
        d = post(two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 30, "at_seconds": 10}],
        )).json()
        trace = d["steps"][0]
        assert trace["at_seconds"] == 10
        vols = {v["vessel_id"]: v["liquid_volume_ml"] for v in trace["vessels"]}
        assert vols == {"A": 70.0, "B": 30.0}
        # 初始状态与末态分别给出
        init_vols = {v["vessel_id"]: v["liquid_volume_ml"] for v in d["vessels"]}
        assert init_vols == {"A": 100.0, "B": 0.0}
        assert [v["vessel_id"] for v in d["final_states"]] == ["A", "B"]


# ---------------------------------------------------------------------------
# 即时禁配 / 材质风险（含容器编号）
# ---------------------------------------------------------------------------
class TestBlocking:
    def test_forbidden_mix_formed_in_destination(self):
        d = post(two_vessels(
            a_init=[{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}],
            b_init=[{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 50, "at_seconds": 0}],
        )).json()
        assert d["blocked"] is True and d["first_blocking_seq"] == 1
        f = d["findings"][0]
        assert f["rule_id"] == "F01"
        assert f["category"] == "forbidden_mix"
        assert f["vessel_id"] == "B"
        assert set(f["entities"]) == {"hcl", "naocl"}
        assert f["concentrations"] == pytest.approx({"hcl": 10.0, "naocl": 6.6667}, abs=1e-3)
        assert "B" in f["blocking_reason"]

    def test_reactive_components_in_different_vessels_do_not_trigger(self):
        # 酸在 A、漂白水在 B，不发生转移 -> 不在同一容器内共存，安全
        d = post(two_vessels(
            a_init=[{"reagent_id": "hcl", "concentration": 30, "volume_ml": 100}],
            b_init=[{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "hold", "at_seconds": 100}],
        )).json()
        assert d["safe"] is True

    def test_initial_liquid_risk_seq_zero_with_vessel(self):
        d = post(two_vessels(
            a_mat="glass",
            a_init=[{"reagent_id": "hf", "concentration": 10, "volume_ml": 50}],
            b_init=[{"reagent_id": "naocl", "concentration": 12, "volume_ml": 50},
                    {"reagent_id": "hcl", "concentration": 30, "volume_ml": 50}],
        )).json()
        assert d["first_blocking_seq"] == 0
        cats = {(f["vessel_id"], f["rule_id"]) for f in d["findings"] if f["seq"] == 0}
        assert ("A", "M01") in cats
        assert ("B", "F01") in cats

    def test_material_incompatible_on_destination_after_transfer(self):
        # HF 在 PTFE(A) 安全；转入玻璃容器 B -> B 内立即材质不相容
        d = post(two_vessels(
            a_mat="ptfe", a_init=[{"reagent_id": "hf", "concentration": 10, "volume_ml": 50}],
            b_mat="glass",
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 50, "at_seconds": 0}],
        )).json()
        rules = {(f["vessel_id"], f["rule_id"], f["category"]) for f in d["findings"]}
        assert ("B", "M01", "material_incompatible") in rules
        assert not any(v == "A" for v, _, _ in rules)

    def test_new_pair_formed_in_destination_triggers_at_transfer(self):
        # B 中次氯酸盐已达阈值（4%），但氨不存在、不构成接触对；转入氨水后双方过阈值，
        # 该新接触对在转入时刻（步骤 1）首次成立并立即阻断
        d = post(two_vessels(
            a_init=[{"reagent_id": "nh3", "concentration": 20, "volume_ml": 25}],
            b_init=[{"reagent_id": "naocl", "concentration": 5, "volume_ml": 75}],  # 混合后 3.75%
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 25, "at_seconds": 0}],
        )).json()
        assert d["blocked"] is True
        f = d["findings"][0]
        assert f["rule_id"] == "F03" and f["vessel_id"] == "B" and f["seq"] == 1


# ---------------------------------------------------------------------------
# 超时接触：先结算此前超时，再执行操作；片段跨转移的保持与新建
# ---------------------------------------------------------------------------
class TestOvertime:
    def test_overtime_settled_before_operation(self):
        # t=2000 先结算 A 内 naoh/玻璃超时，即便该步随即把液体全部倒出
        d = post(two_vessels(
            a_mat="glass", a_init=[{"reagent_id": "naoh", "concentration": 30, "volume_ml": 100}],
            b_mat="hdpe",
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 100, "at_seconds": 2000}],
        )).json()
        f = d["findings"][0]
        assert f["rule_id"] == "M04" and f["vessel_id"] == "A" and f["seq"] == 1
        assert f["contact_seconds"] == pytest.approx(2000.0)
        assert f["allowed_seconds"] == pytest.approx(1800.0)

    def test_existing_episode_continues_new_episode_starts_at_transfer(self):
        # A: naoh/玻璃 自 t=0 计时；t=1000 转一半到玻璃 B（B 内新片段自 1000 起计）
        payload = two_vessels(
            a_mat="glass", a_init=[{"reagent_id": "naoh", "concentration": 30, "volume_ml": 100}],
            b_mat="glass",
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 50, "at_seconds": 1000}])
        # t=1900：A 片段已 1900s 超时；B 仅 900s 未超时
        d1 = post({**payload, "steps": payload["steps"] + [
            {"seq": 2, "op": "hold", "at_seconds": 1900}]}).json()
        assert [(f["vessel_id"], f["contact_seconds"]) for f in d1["findings"]] == [("A", 1900.0)]
        # t=2900：A 2900s；B 片段 1900s 也超时
        d2 = post({**payload, "steps": payload["steps"] + [
            {"seq": 2, "op": "hold", "at_seconds": 2900}]}).json()
        got = {(f["vessel_id"], f["contact_seconds"]) for f in d2["findings"]}
        assert got == {("A", 2900.0), ("B", 1900.0)}

    def test_emptying_source_ends_its_episodes(self):
        # t=1000 把 A 完全倒空（转入 hdpe B，无材质规则）；A 计时结束，后续 hold 安全
        d = post(two_vessels(
            a_mat="glass", a_init=[{"reagent_id": "naoh", "concentration": 30, "volume_ml": 100}],
            b_mat="hdpe",
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 100, "at_seconds": 1000},
                   {"seq": 2, "op": "hold", "at_seconds": 5000}],
        )).json()
        assert d["safe"] is True

    def test_allowance_extends_limit_on_both_vessels(self):
        d = post(two_vessels(
            a_mat="glass", a_init=[{"reagent_id": "naoh", "concentration": 30, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "hold", "at_seconds": 2000}],
            allowances=[{"a": "naoh", "b": "material:glass", "max_contact_seconds": 9999}],
        )).json()
        assert d["safe"] is True


# ---------------------------------------------------------------------------
# 逐项拒绝
# ---------------------------------------------------------------------------
class TestRejections:
    def _codes(self, payload):
        r = post(payload)
        assert r.status_code == 400, r.text
        return [e["code"] for e in r.json()["details"]]

    BASE = two_vessels(
        a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}])

    def test_duplicate_vessel_id(self):
        p = {"vessels": [
            {"vessel_id": "X", "material": "hdpe"},
            {"vessel_id": "X", "material": "hdpe"}], "temperature_c": 20}
        assert "DUPLICATE_VESSEL_ID" in self._codes(p)

    def test_vessel_count_must_be_two(self):
        p = {"vessels": [{"vessel_id": "X", "material": "hdpe"}], "temperature_c": 20}
        assert "INVALID_VESSEL_COUNT" in self._codes(p)
        p3 = {"vessels": [
            {"vessel_id": "A", "material": "hdpe"},
            {"vessel_id": "B", "material": "hdpe"},
            {"vessel_id": "C", "material": "hdpe"}], "temperature_c": 20}
        assert "INVALID_VESSEL_COUNT" in self._codes(p3)

    def test_unknown_vessel_reference(self):
        p = two_vessels(steps=[{"seq": 1, "op": "transfer", "from_vessel": "A",
                                "to_vessel": "C", "volume_ml": 5, "at_seconds": 0}])
        assert "UNKNOWN_VESSEL" in self._codes(p)

    def test_same_source_and_destination(self):
        p = two_vessels(steps=[{"seq": 1, "op": "transfer", "from_vessel": "A",
                                "to_vessel": "A", "volume_ml": 5, "at_seconds": 0}])
        assert "SAME_VESSEL_TRANSFER" in self._codes(p)

    def test_empty_source(self):
        p = two_vessels(steps=[{"seq": 1, "op": "transfer", "from_vessel": "B",
                                "to_vessel": "A", "volume_ml": 5, "at_seconds": 0}])
        assert "EMPTY_SOURCE_VESSEL" in self._codes(p)

    def test_transfer_exceeds_volume(self):
        p = two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 150, "at_seconds": 0}])
        codes = self._codes(p)
        assert "TRANSFER_EXCEEDS_VOLUME" in codes

    @pytest.mark.parametrize("vol", [0, -5])
    def test_non_positive_transfer_volume(self, vol):
        p = two_vessels(steps=[{"seq": 1, "op": "transfer", "from_vessel": "A",
                                "to_vessel": "B", "volume_ml": vol, "at_seconds": 0}])
        assert "INVALID_VOLUME" in self._codes(p)

    def test_source_becomes_empty_halfway(self):
        # 第一步倒空 A 后，第二步再从 A 取液 -> 源为空
        p = two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 100, "at_seconds": 0},
                   {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 1, "at_seconds": 5}])
        assert "EMPTY_SOURCE_VESSEL" in self._codes(p)

    def test_non_monotonic_time(self):
        p = two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "hold", "at_seconds": 100},
                   {"seq": 2, "op": "hold", "at_seconds": 50}])
        assert "NON_MONOTONIC_TIME" in self._codes(p)

    def test_negative_time(self):
        p = two_vessels(steps=[{"seq": 1, "op": "hold", "at_seconds": -1}])
        assert "INVALID_TIME" in self._codes(p)

    def test_hold_requires_time_advance(self):
        p = two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "hold", "at_seconds": 0}])
        assert "INVALID_TIME" in self._codes(p)

    def test_hold_when_both_vessels_empty(self):
        p = two_vessels(steps=[{"seq": 1, "op": "hold", "at_seconds": 10}])
        assert "EMPTY_VESSEL_OPERATION" in self._codes(p)

    def test_duplicate_step_seq(self):
        p = two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 5, "at_seconds": 0},
                   {"seq": 1, "op": "hold", "at_seconds": 5}])
        assert "DUPLICATE_STEP_SEQ" in self._codes(p)

    def test_unknown_material_and_reagent(self):
        p = {"vessels": [
            {"vessel_id": "A", "material": "kryptonite",
             "initial_liquid": [{"reagent_id": "ghost", "concentration": 5, "volume_ml": 10}]},
            {"vessel_id": "B", "material": "hdpe"}], "temperature_c": 20}
        codes = self._codes(p)
        assert "UNKNOWN_MATERIAL" in codes and "UNKNOWN_REAGENT" in codes

    def test_extra_fields_rejected(self):
        r = post({"vessels": [
            {"vessel_id": "A", "material": "hdpe"},
            {"vessel_id": "B", "material": "hdpe"}], "temperature_c": 20, "bogus": 1})
        assert r.status_code == 400
        assert r.json()["details"][0]["code"] == "MALFORMED_REQUEST"

    def test_hold_with_transfer_fields_rejected(self):
        p = two_vessels(steps=[{"seq": 1, "op": "hold", "at_seconds": 5, "from_vessel": "A"}])
        assert "INVALID_STEP_FIELDS" in self._codes(p)

    def test_temperature_out_of_range(self):
        p = two_vessels(temp=999)
        assert "TEMPERATURE_OUT_OF_RANGE" in self._codes(p)

    def test_no_partial_simulation_on_validation_error(self):
        r = post(two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 5, "at_seconds": 0},
                   {"seq": 2, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 999, "at_seconds": 5}]))
        assert r.status_code == 400
        body = r.json()
        assert "TRANSFER_EXCEEDS_VOLUME" in [e["code"] for e in body["details"]]
        assert "findings" not in body and "final_states" not in body


# ---------------------------------------------------------------------------
# 同刻按序号执行 / 初始与每步双容器状态
# ---------------------------------------------------------------------------
class TestOrdering:
    def test_same_timestamp_steps_execute_by_seq(self):
        # 提交顺序 seq2 在前，但 seq1 先执行：先 A->B 20，再 B->A 10
        d = post(two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 2, "op": "transfer", "from_vessel": "B", "to_vessel": "A",
                    "volume_ml": 10, "at_seconds": 0},
                   {"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 20, "at_seconds": 0}])).json()
        fin = by_id(d["final_states"])
        assert fin["A"]["liquid_volume_ml"] == 90.0
        assert fin["B"]["liquid_volume_ml"] == 10.0
        assert [t["seq"] for t in d["steps"]] == [1, 2]

    def test_states_emitted_per_step_for_both_vessels(self):
        d = post(two_vessels(
            a_init=[{"reagent_id": "nacl", "concentration": 10, "volume_ml": 100}],
            steps=[{"seq": 1, "op": "transfer", "from_vessel": "A", "to_vessel": "B",
                    "volume_ml": 25, "at_seconds": 0},
                   {"seq": 2, "op": "hold", "at_seconds": 10}])).json()
        assert len(d["steps"]) == 2
        for t in d["steps"]:
            assert {v["vessel_id"] for v in t["vessels"]} == {"A", "B"}
        vols = {v["vessel_id"]: v["liquid_volume_ml"] for v in d["steps"][0]["vessels"]}
        assert vols == {"A": 75.0, "B": 25.0}
