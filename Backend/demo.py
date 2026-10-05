import hashlib
import os
import secrets
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update, text
from sqlalchemy.exc import IntegrityError

try:
    from .database import SessionLocal
    from .main import require_authenticated_user, require_teacher
    from .models import Attendance, AuditLog, ClassSession, CourseClass, Enrollment, Student
    from .models import DemoCaptcha, DemoChallenge, DemoRelay
except (ImportError, ValueError):
    from database import SessionLocal
    from main import require_authenticated_user, require_teacher
    from models import Attendance, AuditLog, ClassSession, CourseClass, Enrollment, Student
    from models import DemoCaptcha, DemoChallenge, DemoRelay

router = APIRouter(prefix="/api/demo")

CAPTCHA_DEFAULT_TTL_MS = 5 * 60 * 1000  # refreshed codes live 5 minutes by default
CAPTCHA_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/1, O/0 lookalikes
CAPTCHA_MAX_ATTEMPTS = 5  # per student per captcha generation

_captcha_plaintext_cache: dict[str, str] = {}
_captcha_attempts: dict[tuple[str, str], int] = {}


def evaluate_captcha(db, session, captcha_code: str | None, student_id: str) -> tuple[bool, str | None]:
    if not captcha_code:
        return False, "no code entered"
    row = active_captcha(db, session.session_id)
    if not row or row.invalid:
        return False, "No active classroom code"
    if row.expires_at <= now_ms():
        return False, "CAPTCHA Expired"
    key = (row.captcha_id, student_id)
    attempts = _captcha_attempts.get(key, 0)
    if attempts >= CAPTCHA_MAX_ATTEMPTS:
        return False, "Too many failed attempts; classroom code locked"
    code_hash = hashlib.sha256(captcha_code.strip().upper().encode()).hexdigest()
    if secrets.compare_digest(row.code_hash, code_hash):
        return True, None
    attempts += 1
    _captcha_attempts[key] = attempts
    if attempts >= CAPTCHA_MAX_ATTEMPTS:
        return False, "Too many failed attempts; classroom code locked"
    return False, "Invalid CAPTCHA"


def captcha_ttl_ms() -> int:
    """Lifetime of a generated classroom code; teacher can override per request."""
    return int(os.getenv("CAPTCHA_TTL_MS", CAPTCHA_DEFAULT_TTL_MS))


def get_db():
    db = SessionLocal()
    try:
        if db.bind.dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def now_ms():
    return int(time.time() * 1000)


def enrolled(db, student_id, class_id):
    student = db.get(Student, student_id)
    return bool(student and (student.class_id == class_id or db.scalar(select(Enrollment.enrollment_id).where(
        Enrollment.student_id == student_id, Enrollment.class_id == class_id))))


def session_for(db, session_id, user, active=False):
    session = db.get(ClassSession, session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if user["role"] == "student":
        if not enrolled(db, user["sub"], session.class_id):
            raise HTTPException(403, "Not enrolled in this class")
    elif user["role"] != "admin" and session.teacher_id != user["sub"]:
        raise HTTPException(403, "Session belongs to another teacher")
    if active and (session.status != "ACTIVE" or session.expiration_time <= now_ms()):
        raise HTTPException(409, "Session is closed or expired")
    return session


def log(db, user, event, detail):
    db.add(AuditLog(log_id=uuid.uuid4().hex, timestamp=now_ms(), event_type=event,
                    operator_id=user["sub"], details=detail))


def session_data(s):
    return {"session_id": s.session_id, "class_id": s.class_id, "subject": s.subject,
            "teacher_id": s.teacher_id, "status": "EXPIRED" if s.status == "ACTIVE" and s.expiration_time <= now_ms() else s.status,
            "expiration_time": s.expiration_time, "mode": "SIMULATION",
            "attendance_mode": s.attendance_mode or "BLE"}


class StartIn(BaseModel):
    class_id: str = Field(min_length=1, max_length=32)
    duration_seconds: int = Field(default=600, ge=10, le=3600)
    attendance_mode: str = Field(default="BLE", pattern="^(BLE|BLE_CAPTCHA)$")


class ChallengeIn(BaseModel):
    position: str = Field(pattern="^(near|back|outside)$")
    relay_student_id: str | None = Field(default=None, max_length=32)
    captcha_code: str | None = Field(default=None, max_length=8)


class ProofIn(BaseModel):
    challenge_id: str = Field(min_length=32, max_length=32)
    response: str = Field(pattern="^[0-9a-f]{64}$")
    captcha_code: str | None = Field(default=None, max_length=8)


class RelayIn(BaseModel):
    enabled: bool
    position: str = Field(default="near", pattern="^(near|back|outside)$")


class RefreshIn(BaseModel):
    ttl_seconds: int = Field(default=300, ge=30, le=3600)


@router.get("/profile")
def profile(user=Depends(require_authenticated_user), db=Depends(get_db)):
    result = {"user_id": user["sub"], "role": user["role"], "mode": "SIMULATION"}
    if user["role"] == "student":
        student = db.get(Student, user["sub"])
        if not student:
            raise HTTPException(401, "Account no longer exists")
        result.update(name=student.name, device_id=student.registered_device_id)
    return result


@router.get("/classes")
def classes(user=Depends(require_authenticated_user), db=Depends(get_db)):
    rows = db.scalars(select(CourseClass)).all()
    return [{"class_id": c.class_id, "class_name": c.class_name, "subject": c.subject,
             "class_code": c.class_code or c.class_id} for c in rows
            if user["role"] == "admin" or
            (user["role"] == "teacher" and c.teacher_id == user["sub"]) or
            (user["role"] == "student" and enrolled(db, user["sub"], c.class_id))]


@router.get("/sessions")
def sessions(user=Depends(require_authenticated_user), db=Depends(get_db)):
    rows = db.scalars(select(ClassSession).where(ClassSession.session_id.like("demo_%")).order_by(ClassSession.start_time.desc())).all()
    return [session_data(s) for s in rows if user["role"] == "admin" or
            (user["role"] == "teacher" and s.teacher_id == user["sub"]) or
            (user["role"] == "student" and enrolled(db, user["sub"], s.class_id))]


@router.post("/sessions")
def start(item: StartIn, user=Depends(require_teacher), db=Depends(get_db)):
    course = db.get(CourseClass, item.class_id.strip().upper())
    if not course:
        raise HTTPException(404, "Class not found")
    if user["role"] != "admin" and course.teacher_id != user["sub"]:
        raise HTTPException(403, "Class belongs to another teacher")
    if db.scalar(select(ClassSession.session_id).where(ClassSession.class_id == course.class_id,
            ClassSession.status == "ACTIVE", ClassSession.expiration_time > now_ms())):
        raise HTTPException(409, "Class already has an active session")
    stamp = now_ms()
    session = ClassSession(session_id="demo_" + uuid.uuid4().hex, class_id=course.class_id,
        teacher_id=course.teacher_id, subject=course.subject, start_time=stamp,
        expiration_time=stamp + item.duration_seconds * 1000, random_nonce=secrets.token_hex(16), status="ACTIVE",
        attendance_mode=item.attendance_mode)
    db.add(session)
    log(db, user, "DEMO_SESSION_START", session.session_id)
    db.commit()
    data = session_data(session)
    if item.attendance_mode == "BLE_CAPTCHA":
        data.update(generate_captcha_row(db, session, user))
        db.commit()
    return data


# ---- BLE + CAPTCHA mode -----------------------------------------------------

def generate_captcha_row(db, session, user):
    """Create a fresh classroom code for the session, invalidating prior ones.
    The plaintext code is returned once and never stored; only its SHA-256
    digest is kept, and it expires after captcha_ttl_ms()."""
    stamp = now_ms()
    # Refresh: every previous code for this session becomes unusable.
    db.execute(update(DemoCaptcha).where(DemoCaptcha.session_id == session.session_id,
        DemoCaptcha.invalid.is_(False)).values(invalid=True))
    alphabet = CAPTCHA_ALPHABET
    code = "".join(secrets.choice(alphabet) for _ in range(5))
    row = DemoCaptcha(captcha_id=uuid.uuid4().hex, session_id=session.session_id,
        code_hash=hashlib.sha256(code.encode()).hexdigest(),
        created_at=stamp, expires_at=stamp + captcha_ttl_ms(), invalid=False)
    db.add(row)
    # The plaintext lives only in this process's memory (like the teacher's
    # screen); the DB keeps just its SHA-256 digest. After a server restart the
    # cache is empty and check_captcha asks the teacher to refresh the code.
    _captcha_plaintext_cache[row.captcha_id] = code
    log(db, user, "DEMO_CAPTCHA_GENERATED", session.session_id)
    return {"captcha": code, "captcha_expires_at": row.expires_at, "captcha_ttl_ms": captcha_ttl_ms()}


def active_captcha(db, session_id):
    return db.scalars(select(DemoCaptcha).where(DemoCaptcha.session_id == session_id,
        DemoCaptcha.invalid.is_(False)).order_by(DemoCaptcha.created_at.desc())).first()


def teacher_captcha_view(db, session):
    """Teacher panel data for the current generation; None when mode is BLE."""
    if (session.attendance_mode or "BLE") != "BLE_CAPTCHA":
        return None
    row = active_captcha(db, session.session_id)
    if row is None:
        return {"required": True, "active": False, "expires_at": None, "ttl_ms": captcha_ttl_ms()}
    return {
        "required": True,
        "active": not row.invalid and row.expires_at > now_ms(),
        "expires_at": row.expires_at,
        "ttl_ms": captcha_ttl_ms()
    }


def _captcha_plaintext(db, row):
    """Return the cached plaintext for an active generation.
    The DB stores only a SHA-256 digest; if this process never generated (or no
    longer holds) the plaintext, only a teacher refresh can restore it."""
    code = _captcha_plaintext_cache.get(row.captcha_id)
    if code is None or hashlib.sha256(code.encode()).hexdigest() != row.code_hash:
        raise HTTPException(409, "Classroom code lost after server restart; ask the teacher to refresh it")
    return code


@router.get("/sessions/{session_id}/attendance")
def attendance(session_id: str, user=Depends(require_authenticated_user), db=Depends(get_db)):
    session = session_for(db, session_id, user)
    students = db.scalars(select(Student).order_by(Student.student_id)).all()
    records = {a.student_id: a for a in db.scalars(select(Attendance).where(Attendance.session_id == session_id)).all()}
    result = []
    for s in students:
        if not enrolled(db, s.student_id, session.class_id):
            continue
        if user["role"] == "student" and s.student_id != user["sub"]:
            continue
        rec = records.get(s.student_id)
        if rec:
            status_val = rec.verification_status
            route_val = rec.route_type
            rssi_val = rec.rssi_evidence
            ble_val = bool(rec.ble_verified)
            cap_val = bool(rec.captcha_verified)
            method_val = rec.verification_method or ("BLE + CAPTCHA" if (ble_val and cap_val) else ("CAPTCHA" if cap_val else "BLE"))
        else:
            status_val = "NOT_VERIFIED"
            route_val = None
            rssi_val = None
            ble_val = False
            cap_val = False
            method_val = None
        result.append({
            "student_id": s.student_id,
            "name": s.name,
            "status": status_val,
            "route": route_val,
            "rssi": rssi_val,
            "ble_verified": ble_val,
            "captcha_verified": cap_val,
            "verification_method": method_val,
            "attendance_mode": session.attendance_mode or "BLE"
        })
    return result


@router.get("/sessions/{session_id}/captcha")
def get_captcha(session_id: str, user=Depends(require_authenticated_user), db=Depends(get_db)):
    """Teacher panel data for the current classroom code."""
    session = session_for(db, session_id, user)
    view = teacher_captcha_view(db, session)
    if view is None:
        raise HTTPException(409, "This session is in BLE-only attendance mode")
    return view


@router.post("/sessions/{session_id}/captcha/refresh")
def refresh_captcha(session_id: str, item: RefreshIn | None = None,
                    user=Depends(require_teacher), db=Depends(get_db)):
    """Teacher invalidates the current code and gets a brand-new one."""
    session = session_for(db, session_id, user)
    if (session.attendance_mode or "BLE") != "BLE_CAPTCHA":
        raise HTTPException(409, "This session is in BLE-only attendance mode")
    if session.status != "ACTIVE" or session.expiration_time <= now_ms():
        raise HTTPException(409, "Session is closed or expired")
    db.execute(update(DemoCaptcha).where(DemoCaptcha.session_id == session_id,
        DemoCaptcha.invalid.is_(False)).values(invalid=True))
    session_marker = session  # keep the row session-bound
    ttl = (item.ttl_seconds if item else 300) * 1000
    data = generate_captcha_row(db, session, user)
    data["captcha_ttl_ms"] = ttl
    db.flush()  # make the new generation visible to the re-read below
    row = active_captcha(db, session_id)
    row.expires_at = now_ms() + ttl
    data["captcha_expires_at"] = row.expires_at
    db.commit()
    return data


@router.post("/sessions/{session_id}/relay")
def relay(session_id: str, item: RelayIn, user=Depends(require_authenticated_user), db=Depends(get_db)):
    session_for(db, session_id, user, active=True)
    if user["role"] != "student":
        raise HTTPException(403, "Student role required")
    key = session_id + ":" + user["sub"]
    row = db.get(DemoRelay, key)
    if not row:
        row = DemoRelay(relay_id=key, session_id=session_id, student_id=user["sub"])
        db.add(row)
    row.enabled = item.enabled
    row.position = item.position
    db.commit()
    return {"enabled": row.enabled, "mode": "SIMULATION"}


@router.get("/sessions/{session_id}/relays")
def relays(session_id: str, user=Depends(require_authenticated_user), db=Depends(get_db)):
    session_for(db, session_id, user, active=True)
    return [r.student_id for r in db.scalars(select(DemoRelay).where(DemoRelay.session_id == session_id,
        DemoRelay.enabled.is_(True), DemoRelay.position != "outside")).all() if r.student_id != user["sub"]]


@router.post("/sessions/{session_id}/challenge")
def challenge(session_id: str, item: ChallengeIn, user=Depends(require_authenticated_user), db=Depends(get_db)):
    session = session_for(db, session_id, user, active=True)
    if user["role"] != "student":
        raise HTTPException(403, "Student role required")
    # OR logic: the classroom code is optional at challenge time. A student
    # with a working BLE link never needs it; entering it here is allowed but
    # only evaluated at verify time, together with the BLE proof.
    if db.scalar(select(Attendance.attendance_id).where(Attendance.session_id == session_id, Attendance.student_id == user["sub"])):
        raise HTTPException(409, "Attendance already recorded")
    relay_ok = False
    if item.relay_student_id:
        r = db.get(DemoRelay, session_id + ":" + item.relay_student_id)
        if not r or not r.enabled or r.position == "outside" or r.student_id == user["sub"] or not enrolled(db, r.student_id, session.class_id):
            if session.attendance_mode != "BLE_CAPTCHA":
                raise HTTPException(409, "Selected relay is unavailable")
        else:
            relay_ok = True
    elif item.position == "outside":
        if session.attendance_mode != "BLE_CAPTCHA":
            raise HTTPException(409, "Simulated direct signal is below threshold; enable a classmate relay")
    db.execute(update(DemoChallenge).where(DemoChallenge.session_id == session_id,
        DemoChallenge.student_id == user["sub"]).values(used=True))
    nonce = secrets.token_hex(32)
    rssi = -120 if ((item.position == "outside" and not relay_ok) or (item.relay_student_id and not relay_ok)) else (-74 if relay_ok or item.position == "back" else -56)
    row = DemoChallenge(challenge_id=uuid.uuid4().hex, session_id=session_id, student_id=user["sub"],
        expected_hash=hashlib.sha256(nonce.encode()).hexdigest(), expires_at=min(now_ms() + 30000, session.expiration_time),
        used=False, relay_student_id=item.relay_student_id, rssi=rssi)
    db.add(row)
    db.commit()
    data = {"challenge_id": row.challenge_id, "nonce": nonce, "expires_at": row.expires_at,
            "mode": "SIMULATION", "proof": "SHA256(nonce); demonstrates challenge lifecycle, not hardware attestation"}
    if session.attendance_mode == "BLE_CAPTCHA":
        data["attendance_mode"] = "BLE_CAPTCHA"
    return data


@router.post("/sessions/{session_id}/verify")
def verify(session_id: str, item: ProofIn, user=Depends(require_authenticated_user), db=Depends(get_db)):
    """OR logic for BLE_CAPTCHA sessions: attendance is marked when the BLE
    challenge-response succeeds OR the classroom code verifies OR both.
    BLE-only sessions keep the classic single-factor flow."""
    session = session_for(db, session_id, user, active=True)
    if user["role"] != "student":
        raise HTTPException(403, "Student role required")

    if db.scalar(select(Attendance.attendance_id).where(Attendance.session_id == session_id, Attendance.student_id == user["sub"])):
        raise HTTPException(409, "Attendance already recorded")

    row = db.get(DemoChallenge, item.challenge_id)
    if not row or row.session_id != session_id or row.student_id != user["sub"]:
        raise HTTPException(400, "Challenge does not match this student and session")

    ble_ok = False
    ble_error = None

    if session.attendance_mode != "BLE_CAPTCHA":
        if row.used:
            raise HTTPException(409, "Challenge already consumed")
        if row.expires_at <= now_ms():
            raise HTTPException(409, "Challenge expired")
        if row.relay_student_id:
            relay_row = db.get(DemoRelay, session_id + ":" + row.relay_student_id)
            if not relay_row or not relay_row.enabled or relay_row.position == "outside":
                raise HTTPException(409, "Selected relay is unavailable")
        if not item.response or not secrets.compare_digest(row.expected_hash, item.response):
            row.used = True
            db.commit()
            raise HTTPException(400, "Attendance not verified: BLE verification failed (invalid challenge response)")
        ble_ok = True
    else:
        if row.used:
            ble_error = "challenge used"
        elif row.expires_at <= now_ms():
            ble_error = "challenge expired"
        elif row.rssi < -85:
            ble_error = "Simulated direct signal is below threshold"
        elif not item.response or not secrets.compare_digest(row.expected_hash, item.response):
            ble_error = "invalid challenge response"
        else:
            ble_ok = True

        if ble_ok and row.relay_student_id:
            relay_row = db.get(DemoRelay, session_id + ":" + row.relay_student_id)
            if not relay_row or not relay_row.enabled or relay_row.position == "outside":
                ble_ok = False
                ble_error = "Relay is no longer available"

    captcha_ok, captcha_error = False, None
    if session.attendance_mode == "BLE_CAPTCHA":
        if item.captcha_code:
            captcha_ok, captcha_error = evaluate_captcha(db, session, item.captcha_code, user["sub"])
        else:
            captcha_error = "no code entered"

    is_verified = (ble_ok or captcha_ok) if session.attendance_mode == "BLE_CAPTCHA" else ble_ok

    if not is_verified:
        # Both factors failed: consume challenge so it cannot be replayed
        if not row.used and row.expires_at > now_ms():
            row.used = True
            db.commit()
        captcha_msg = captcha_error if isinstance(captcha_error, str) else (captcha_error[1] if captcha_error else "unknown reason")
        detail = f"Attendance not verified: BLE verification failed ({ble_error or 'invalid'}) and CAPTCHA verification failed ({captcha_msg})"
        raise HTTPException(400, detail)

    if ble_ok:
        consumed = db.execute(update(DemoChallenge).where(DemoChallenge.challenge_id == row.challenge_id,
            DemoChallenge.used.is_(False), DemoChallenge.expires_at > now_ms()).values(used=True)).rowcount
        if consumed != 1 and session.attendance_mode != "BLE_CAPTCHA":
            db.rollback()
            raise HTTPException(409, "Challenge already consumed")
    else:
        row.used = True  # captcha-only success burns the challenge

    if session.attendance_mode == "BLE_CAPTCHA":
        if ble_ok and captcha_ok:
            verification = "BLE + CAPTCHA"
            route = "DIR+CAP" if not row.relay_student_id else "RELAY+CAP"
        elif ble_ok:
            verification = "BLE"
            route = "RELAY" if row.relay_student_id else "DIRECT"
        else:
            verification = "CAPTCHA"
            route = "CAPTCHA"
    else:
        verification = "BLE"
        route = "RELAY" if row.relay_student_id else "DIRECT"

    record = Attendance(
        attendance_id="demo_" + hashlib.sha256((session_id + ":" + user["sub"]).encode()).hexdigest()[:48],
        session_id=session_id,
        student_id=user["sub"],
        timestamp=now_ms(),
        verification_status="ELIGIBLE",
        route_type=route,
        rssi_evidence=row.rssi if ble_ok else None,
        ble_verified=ble_ok,
        captcha_verified=captcha_ok,
        verification_method=verification,
        hop_count=2 if row.relay_student_id else 0,
        via_student=row.relay_student_id,
        synced=True,
        teacher_signature="CAPTCHA_VERIFIED" if (captcha_ok and not ble_ok) else ("BLE_CAPTCHA_VERIFIED" if (ble_ok and captcha_ok) else "BLE_VERIFIED"),
        attendance_mode=session.attendance_mode or "BLE"
    )
    db.add(record)
    log(db, user, "DEMO_ELIGIBLE", session_id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Attendance already recorded")

    return {
        "status": "ELIGIBLE",
        "mode": "SIMULATION",
        "attendance_id": record.attendance_id,
        "attendance_mode": session.attendance_mode or "BLE",
        "ble_verified": ble_ok,
        "captcha_verified": captcha_ok,
        "verification": verification,
        "route": record.route_type,
        "rssi": record.rssi_evidence,
        "timestamp": record.timestamp
    }


@router.post("/sessions/{session_id}/finalize")
def finalize(session_id: str, user=Depends(require_teacher), db=Depends(get_db)):
    session = session_for(db, session_id, user)
    if session.status == "FINALIZED":
        return {"status": "FINALIZED", "updated": 0}
    changed = db.execute(update(Attendance).where(Attendance.session_id == session_id,
        Attendance.verification_status == "ELIGIBLE").values(verification_status="PRESENT", synced=True)).rowcount
    session.status = "FINALIZED"
    db.execute(update(DemoChallenge).where(DemoChallenge.session_id == session_id).values(used=True))
    db.execute(update(DemoRelay).where(DemoRelay.session_id == session_id).values(enabled=False))
    # Ending the session invalidates the classroom code: nothing can be
    # verified against it afterwards.
    db.execute(update(DemoCaptcha).where(DemoCaptcha.session_id == session_id).values(invalid=True))
    _captcha_plaintext_cache.clear()
    log(db, user, "DEMO_FINALIZED", session_id)
    db.commit()
    return {"status": "FINALIZED", "updated": changed}
