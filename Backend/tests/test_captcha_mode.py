"""BLE + CAPTCHA attendance mode: session-bound codes, dual-factor marking,
expiry/refresh/attempt limits, and BLE-only regression."""
import hashlib
import time
import uuid

from sqlalchemy import select

from fastapi.testclient import TestClient

from Backend.main import app
from Backend.models import Attendance, ClassSession, DemoCaptcha
from Backend.database import SessionLocal
from Backend.tests.test_demo_workflow import login, new_session, solve


def captcha_class(teacher, code):
    assert teacher.post("/api/classes", json={"class_id": code, "class_name": code, "subject": "Demo",
        "teacher_id": "T001", "class_code": code}).status_code == 200
    assert login("S001").post("/api/v2/classes/join", json={"class_code": code}).status_code == 200


def start_captcha_session(teacher, class_id):
    response = teacher.post("/api/demo/sessions", json={"class_id": class_id, "attendance_mode": "BLE_CAPTCHA"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["attendance_mode"] == "BLE_CAPTCHA"
    return "/api/demo/sessions/" + data["session_id"], data


def test_ble_only_mode_default_unchanged():
    """Mode 1: the classic workflow must behave exactly as before."""
    teacher, student = login("T001"), login("S001")
    path = new_session(teacher)
    session_id = path.rsplit("/", 1)[1]
    with SessionLocal() as db:
        assert (db.get(ClassSession, session_id).attendance_mode or "BLE") == "BLE"
    solve(student, path)  # no captcha fields needed anywhere
    rows = teacher.get(path + "/attendance").json()
    assert rows[0]["status"] == "ELIGIBLE"
    assert rows[0]["route"] in ("DIRECT", "RELAY")


def test_captcha_session_generates_random_code_and_teacher_view():
    teacher, student = login("T001"), login("S001")
    code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, code)
    captcha_path, data = start_captcha_session(teacher, code)
    assert len(data["captcha"]) == 5
    assert data["captcha"].isalnum()

    view = teacher.get(captcha_path + "/captcha").json()
    assert view["required"] is True and view["active"] is True
    assert view["expires_at"] == data["captcha_expires_at"]

    # students must not see the code
    assert student.get(captcha_path + "/captcha").status_code == 200
    student_view = student.get(captcha_path + "/captcha").json()
    assert "captcha" not in student_view

    # two sessions never share a code (each class allows one active session)
    other_code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, other_code)
    other = start_captcha_session(teacher, other_code)[1]["captcha"]
    assert other != data["captcha"]


def test_ble_only_marks_attendance_in_captcha_mode():
    """OR logic: a valid BLE challenge-response is enough in BLE_CAPTCHA mode."""
    teacher, student = login("T001"), login("S001")
    code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, code)
    path, data = start_captcha_session(teacher, code)

    # No captcha_code at all — still works via BLE.
    challenge = student.post(path + "/challenge", json={"position": "near"}).json()
    proof = {"challenge_id": challenge["challenge_id"],
             "response": hashlib.sha256(challenge["nonce"].encode()).hexdigest()}
    result = student.post(path + "/verify", json=proof)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "ELIGIBLE"
    assert body["ble_verified"] is True
    assert body["captcha_verified"] is False
    assert body["verification"] == "BLE"

    rows = teacher.get(path + "/attendance").json()
    assert rows[0]["status"] == "ELIGIBLE"
    assert rows[0]["route"] == "DIRECT"
    assert rows[0]["rssi"] == -56
    with SessionLocal() as db:
        record = db.get(Attendance, body["attendance_id"])
        assert record.attendance_mode == "BLE_CAPTCHA"
        assert record.captcha_verified is False


def test_captcha_only_marks_attendance_in_captcha_mode():
    """OR logic: a valid classroom code is enough in BLE_CAPTCHA mode,
    even when the BLE proof is wrong (or not provided correctly)."""
    teacher, student = login("T001"), login("S001")
    code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, code)
    path, data = start_captcha_session(teacher, code)
    captcha = data["captcha"]

    # Submit a wrong BLE proof but the correct captcha_code — CAPTCHA should carry.
    challenge = student.post(path + "/challenge", json={"position": "near", "captcha_code": captcha}).json()
    proof = {"challenge_id": challenge["challenge_id"],
             "response": "0" * 64,  # wrong hash
             "captcha_code": captcha}
    result = student.post(path + "/verify", json=proof)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "ELIGIBLE"
    assert body["ble_verified"] is False
    assert body["captcha_verified"] is True
    assert body["verification"] == "CAPTCHA"

    rows = teacher.get(path + "/attendance").json()
    assert rows[0]["status"] == "ELIGIBLE"
    assert rows[0]["captcha_verified"] is True


def test_both_methods_valid_marks_attendance():
    """OR logic: BLE + CAPTCHA both valid still marks attendance."""
    teacher, student = login("T001"), login("S001")
    code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, code)
    path, data = start_captcha_session(teacher, code)
    captcha = data["captcha"]

    challenge = student.post(path + "/challenge", json={"position": "near", "captcha_code": captcha})
    assert challenge.status_code == 200, challenge.text
    proof = {"challenge_id": challenge.json()["challenge_id"],
             "response": hashlib.sha256(challenge.json()["nonce"].encode()).hexdigest(),
             "captcha_code": captcha}
    result = student.post(path + "/verify", json=proof)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "ELIGIBLE"
    assert body["ble_verified"] is True and body["captcha_verified"] is True
    assert body["verification"] == "BLE + CAPTCHA"

    rows = teacher.get(path + "/attendance").json()
    assert rows[0]["status"] == "ELIGIBLE"
    assert rows[0]["route"] == "DIR+CAP" or rows[0]["route"] == "BOTH"

    # single submission: second attempt is rejected
    assert student.post(path + "/challenge", json={"position": "near", "captcha_code": captcha}).status_code == 409

    # record keeps how attendance was verified
    with SessionLocal() as db:
        record = db.get(Attendance, body["attendance_id"])
        session = db.get(ClassSession, record.session_id)
        assert session.attendance_mode == "BLE_CAPTCHA"
        assert record.route_type in ("DIR+CAP", "BOTH")


def test_captcha_and_ble_both_fail_rejects_attendance():
    """When both factors fail, attendance must remain NOT_VERIFIED."""
    teacher, student = login("T001"), login("S001")
    code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, code)
    path, data = start_captcha_session(teacher, code)
    captcha = data["captcha"]

    # Wrong captcha AND wrong BLE proof.
    challenge = student.post(path + "/challenge", json={"position": "near", "captcha_code": "ZZZZZ"}).json()
    proof = {"challenge_id": challenge["challenge_id"],
             "response": "0" * 64,  # wrong hash
             "captcha_code": "ZZZZZ"}
    result = student.post(path + "/verify", json=proof)
    assert result.status_code == 400, result.text
    assert "not verified" in result.json()["detail"].lower()
    assert "BLE" in result.json()["detail"]
    assert "CAPTCHA" in result.json()["detail"]

    rows = teacher.get(path + "/attendance").json()
    assert rows[0]["status"] == "NOT_VERIFIED"
    with SessionLocal() as db:
        # ensure no attendance record was created
        assert db.scalar(select(Attendance.attendance_id).where(Attendance.session_id == path.rsplit("/", 1)[1], Attendance.student_id == "S001")) is None


def test_expired_and_wrong_captchas_rejected():
    teacher, student = login("T001"), login("S001")
    code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, code)
    path, data = start_captcha_session(teacher, code)
    captcha = data["captcha"]

    # wrong code -> Invalid CAPTCHA
    challenge = student.post(path + "/challenge", json={"position": "near"}).json()
    proof_wrong = {"challenge_id": challenge["challenge_id"], "response": "0" * 64, "captcha_code": "ZZZZZ"}
    response = student.post(path + "/verify", json=proof_wrong)
    assert response.status_code == 400 and "Invalid CAPTCHA" in response.json()["detail"]

    # expired code -> CAPTCHA Expired
    with SessionLocal() as db:
        row = db.scalars(select_demo_captchas(path.rsplit("/", 1)[1])).first()
        row.expires_at = int(time.time() * 1000) - 1
        db.commit()
    challenge2 = student.post(path + "/challenge", json={"position": "near"}).json()
    proof_exp = {"challenge_id": challenge2["challenge_id"], "response": "0" * 64, "captcha_code": captcha}
    response2 = student.post(path + "/verify", json=proof_exp)
    assert response2.status_code == 400 and "CAPTCHA Expired" in response2.json()["detail"]


def select_demo_captchas(session_id):
    return select(DemoCaptcha).where(DemoCaptcha.session_id == session_id)


def test_refresh_invalidates_previous_code():
    teacher, student = login("T001"), login("S001")
    code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, code)
    path, data = start_captcha_session(teacher, code)
    old_captcha = data["captcha"]

    refreshed = teacher.post(path + "/captcha/refresh", json={})
    assert refreshed.status_code == 200, refreshed.text
    new_captcha = refreshed.json()["captcha"]
    assert new_captcha != old_captcha

    # the old code no longer verifies...
    challenge1 = student.post(path + "/challenge", json={"position": "near"}).json()
    response = student.post(path + "/verify", json={"challenge_id": challenge1["challenge_id"], "response": "0" * 64, "captcha_code": old_captcha})
    assert response.status_code == 400 and "Invalid CAPTCHA" in response.json()["detail"]

    # ...but the new one does (marking attendance via CAPTCHA)
    challenge2 = student.post(path + "/challenge", json={"position": "near"}).json()
    ok = student.post(path + "/verify", json={"challenge_id": challenge2["challenge_id"], "response": "0" * 64, "captcha_code": new_captcha})
    assert ok.status_code == 200, ok.text
    assert ok.json()["captcha_verified"] is True

    # students cannot refresh codes
    assert student.post(path + "/captcha/refresh", json={}).status_code == 403

    # only one active generation remains
    with SessionLocal() as db:
        rows = db.scalars(select_demo_captchas(path.rsplit("/", 1)[1])).all()
        assert sum(1 for r in rows if not r.invalid) == 1


def test_attempts_limited_then_code_reset():
    teacher, student = login("T001"), login("S001")
    code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, code)
    path, data = start_captcha_session(teacher, code)

    for attempt in range(4):  # attempts 1-4 fail with Invalid CAPTCHA
        ch = student.post(path + "/challenge", json={"position": "near"}).json()
        response = student.post(path + "/verify", json={"challenge_id": ch["challenge_id"], "response": "0" * 64, "captcha_code": "WRONG"})
        assert response.status_code == 400 and "Invalid CAPTCHA" in response.json()["detail"], (attempt, response.text)
    # 5th failure locks the code for this student...
    ch5 = student.post(path + "/challenge", json={"position": "near"}).json()
    response5 = student.post(path + "/verify", json={"challenge_id": ch5["challenge_id"], "response": "0" * 64, "captcha_code": "WRONG"})
    assert response5.status_code == 400 and "Too many" in response5.json()["detail"]
    # ...and the lockout is sticky even with the correct code
    ch6 = student.post(path + "/challenge", json={"position": "near"}).json()
    response6 = student.post(path + "/verify", json={"challenge_id": ch6["challenge_id"], "response": "0" * 64, "captcha_code": data["captcha"]})
    assert response6.status_code == 400 and "Too many" in response6.json()["detail"]


def test_finalize_invalidates_captcha_and_blocks_late_submission():
    teacher, student = login("T001"), login("S001")
    code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, code)
    path, data = start_captcha_session(teacher, code)
    captcha = data["captcha"]

    challenge = student.post(path + "/challenge", json={"position": "near", "captcha_code": captcha}).json()
    proof = {"challenge_id": challenge["challenge_id"],
             "response": hashlib.sha256(challenge["nonce"].encode()).hexdigest(),
             "captcha_code": captcha}
    assert student.post(path + "/verify", json=proof).status_code == 200

    assert teacher.post(path + "/finalize", json={}).status_code == 200
    view = teacher.get(path + "/captcha").json()
    assert view["active"] is False

    # session is finalized: late challenges cannot be issued
    challenge2 = student.post(path + "/challenge", json={"position": "near", "captcha_code": captcha})
    assert challenge2.status_code == 409


def test_captcha_not_accepted_for_other_session():
    teacher, student = login("T001"), login("S001")
    code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, code)
    path_a, data_a = start_captcha_session(teacher, code)
    other_code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, other_code)
    path_b, data_b = start_captcha_session(teacher, other_code)

    ch = student.post(path_b + "/challenge", json={"position": "near"}).json()
    response = student.post(path_b + "/verify", json={"challenge_id": ch["challenge_id"], "response": "0" * 64, "captcha_code": data_a["captcha"]})
    assert response.status_code == 400 and "Invalid CAPTCHA" in response.json()["detail"]


def test_finalize_captcha_marked_present_with_mode():
    teacher, student = login("T001"), login("S001")
    code = "CAP-" + uuid.uuid4().hex[:8]
    captcha_class(teacher, code)
    path, data = start_captcha_session(teacher, code)
    captcha = data["captcha"]
    challenge = student.post(path + "/challenge", json={"position": "near", "captcha_code": captcha}).json()
    proof = {"challenge_id": challenge["challenge_id"],
             "response": "0" * 64,  # BLE fails, but CAPTCHA is valid!
             "captcha_code": captcha}
    result = student.post(path + "/verify", json=proof)
    assert result.status_code == 200, result.text
    assert result.json()["ble_verified"] is False
    assert result.json()["captcha_verified"] is True
    assert result.json()["verification"] == "CAPTCHA"

    assert teacher.post(path + "/finalize", json={}).status_code == 200
    rows = teacher.get(path + "/attendance").json()
    assert rows[0]["status"] == "PRESENT"
    assert rows[0]["route"] == "CAPTCHA"
    assert rows[0]["captcha_verified"] is True
    assert rows[0]["ble_verified"] is False
    assert rows[0]["verification_method"] == "CAPTCHA"
