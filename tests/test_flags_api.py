"""The flag management API: create, read, update, delete, targeting rules, and the audit trail.

Every test starts with an empty database (tests/conftest.py) and talks to the real app with
real tokens, so what is tested is the behaviour a caller sees.
"""
import pytest

BOOLEAN = {"name": "new-checkout", "flag_type": "boolean", "default_value": False, "environment": "dev"}
PERCENTAGE = {"name": "new-search", "flag_type": "percentage", "default_value": False,
              "rollout_percentage": 25, "environment": "dev"}
TARGETED = {"name": "beta-dashboard", "flag_type": "targeted", "default_value": False, "environment": "dev"}


def create(api, admin, body):
    response = api.post("/flags", json=body, headers=admin)
    assert response.status_code == 201, response.text
    return response.json()


# ---- create -----------------------------------------------------------------------------

def test_create_returns_the_stored_flag(api, admin):
    flag = create(api, admin, {**BOOLEAN, "description": "Checkout v2"})
    assert flag["id"] > 0
    assert {k: flag[k] for k in BOOLEAN} == BOOLEAN
    assert flag["description"] == "Checkout v2"
    assert flag["rollout_percentage"] is None
    assert flag["targeting_rules"] == []


def test_same_name_in_the_same_environment_is_a_conflict(api, admin):
    create(api, admin, BOOLEAN)
    assert api.post("/flags", json=BOOLEAN, headers=admin).status_code == 409


def test_same_name_in_another_environment_is_allowed(api, admin):
    create(api, admin, BOOLEAN)
    assert api.post("/flags", json={**BOOLEAN, "environment": "prod"}, headers=admin).status_code == 201


@pytest.mark.parametrize("body, why", [
    ({**BOOLEAN, "flag_type": "gradual"}, "unknown flag type"),
    ({**BOOLEAN, "environment": "qa"}, "unknown environment"),
    ({**BOOLEAN, "rollout_percentage": 50}, "a boolean flag must not have a percentage"),
    ({k: v for k, v in PERCENTAGE.items() if k != "rollout_percentage"}, "a percentage flag needs one"),
    ({**PERCENTAGE, "rollout_percentage": 101}, "percentage above 100"),
    ({**PERCENTAGE, "rollout_percentage": -1}, "percentage below 0"),
    ({k: v for k, v in BOOLEAN.items() if k != "name"}, "name missing"),
])
def test_invalid_flags_are_rejected(api, admin, body, why):
    assert api.post("/flags", json=body, headers=admin).status_code == 422, why
    assert api.get("/flags", headers=admin).json() == []          # nothing was stored


# ---- read -------------------------------------------------------------------------------

def test_get_one_and_404(api, admin, viewer):
    flag = create(api, admin, BOOLEAN)
    assert api.get(f"/flags/{flag['id']}", headers=viewer).json()["name"] == "new-checkout"
    assert api.get("/flags/999999", headers=viewer).status_code == 404


def test_list_can_be_filtered_by_environment_and_type(api, admin, viewer):
    create(api, admin, BOOLEAN)
    create(api, admin, PERCENTAGE)
    create(api, admin, {**BOOLEAN, "environment": "prod"})

    def names(query=""):
        return sorted((f["name"], f["environment"]) for f in api.get(f"/flags{query}", headers=viewer).json())

    assert len(names()) == 3
    assert names("?environment=prod") == [("new-checkout", "prod")]
    assert names("?flag_type=percentage") == [("new-search", "dev")]
    assert names("?environment=dev&flag_type=boolean") == [("new-checkout", "dev")]
    assert api.get("/flags?environment=qa", headers=viewer).status_code == 422


# ---- update -----------------------------------------------------------------------------

def test_patch_changes_only_the_given_fields(api, admin):
    flag = create(api, admin, {**BOOLEAN, "description": "old"})
    updated = api.patch(f"/flags/{flag['id']}", json={"default_value": True}, headers=admin)
    assert updated.status_code == 200
    assert updated.json()["default_value"] is True
    assert updated.json()["description"] == "old"
    assert updated.json()["name"] == "new-checkout"


def test_patch_is_validated_against_the_whole_flag(api, admin):
    boolean = create(api, admin, BOOLEAN)
    percentage = create(api, admin, PERCENTAGE)
    # a percentage on a boolean flag makes no sense, even though the field alone is valid
    assert api.patch(f"/flags/{boolean['id']}", json={"rollout_percentage": 10}, headers=admin).status_code == 422
    # turning a percentage flag into a boolean one must drop the percentage in the same request
    assert api.patch(f"/flags/{percentage['id']}", json={"flag_type": "boolean"}, headers=admin).status_code == 422
    ok = api.patch(f"/flags/{percentage['id']}",
                   json={"flag_type": "boolean", "rollout_percentage": None}, headers=admin)
    assert ok.status_code == 200 and ok.json()["rollout_percentage"] is None


def test_patch_cannot_create_a_duplicate(api, admin):
    create(api, admin, BOOLEAN)
    other = create(api, admin, {**BOOLEAN, "name": "other"})
    assert api.patch(f"/flags/{other['id']}", json={"name": "new-checkout"}, headers=admin).status_code == 409


def test_patch_unknown_flag_is_404(api, admin):
    assert api.patch("/flags/999999", json={"description": "x"}, headers=admin).status_code == 404


# ---- delete -----------------------------------------------------------------------------

def test_delete_removes_the_flag(api, admin):
    flag = create(api, admin, BOOLEAN)
    assert api.delete(f"/flags/{flag['id']}", headers=admin).status_code == 204
    assert api.get(f"/flags/{flag['id']}", headers=admin).status_code == 404
    assert api.delete(f"/flags/{flag['id']}", headers=admin).status_code == 404
    # the name is free again
    assert api.post("/flags", json=BOOLEAN, headers=admin).status_code == 201


# ---- targeting rules --------------------------------------------------------------------

def test_rules_can_be_added_to_and_removed_from_a_targeted_flag(api, admin):
    flag = create(api, admin, TARGETED)
    rule = api.post(f"/flags/{flag['id']}/rules",
                    json={"attribute": "plan", "operator": "equals", "value": "pro"}, headers=admin)
    assert rule.status_code == 201
    assert rule.json()["flag_id"] == flag["id"]
    assert [r["attribute"] for r in api.get(f"/flags/{flag['id']}", headers=admin).json()["targeting_rules"]] == ["plan"]

    assert api.delete(f"/flags/{flag['id']}/rules/{rule.json()['id']}", headers=admin).status_code == 204
    assert api.get(f"/flags/{flag['id']}", headers=admin).json()["targeting_rules"] == []
    assert api.delete(f"/flags/{flag['id']}/rules/{rule.json()['id']}", headers=admin).status_code == 404


def test_rules_only_make_sense_on_targeted_flags(api, admin):
    flag = create(api, admin, BOOLEAN)
    response = api.post(f"/flags/{flag['id']}/rules",
                        json={"attribute": "plan", "operator": "equals", "value": "pro"}, headers=admin)
    assert response.status_code == 400


def test_rule_operator_is_validated(api, admin):
    flag = create(api, admin, TARGETED)
    response = api.post(f"/flags/{flag['id']}/rules",
                        json={"attribute": "plan", "operator": "matches", "value": "p.*"}, headers=admin)
    assert response.status_code == 422


def test_a_rule_of_one_flag_cannot_be_deleted_through_another(api, admin):
    first = create(api, admin, TARGETED)
    second = create(api, admin, {**TARGETED, "name": "other-targeted"})
    rule_id = api.post(f"/flags/{first['id']}/rules",
                       json={"attribute": "plan", "operator": "equals", "value": "pro"}, headers=admin).json()["id"]
    assert api.delete(f"/flags/{second['id']}/rules/{rule_id}", headers=admin).status_code == 404
    assert len(api.get(f"/flags/{first['id']}", headers=admin).json()["targeting_rules"]) == 1


# ---- audit trail ------------------------------------------------------------------------

def test_every_change_is_recorded_with_who_made_it(api, admin, viewer):
    flag = create(api, admin, BOOLEAN)
    api.patch(f"/flags/{flag['id']}", json={"default_value": True}, headers=admin)

    history = api.get(f"/flags/{flag['id']}/history", headers=viewer)
    assert history.status_code == 200
    entries = history.json()
    assert len(entries) == 2
    assert {e["actor"] for e in entries} == {"admin-user"}        # the token's subject
    assert sorted(e["action"] for e in entries) == sorted({e["action"] for e in entries})  # two distinct actions


def test_history_of_an_unknown_flag_is_404(api, viewer):
    assert api.get("/flags/999999/history", headers=viewer).status_code == 404
