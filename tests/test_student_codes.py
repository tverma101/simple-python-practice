"""Per-student access codes for the LAN practice server."""
import pytest

from ducky.classroom import ClassroomDB


@pytest.fixture
def db(tmp_path):
    d = ClassroomDB(db_path=tmp_path / "classroom.db")
    d.create_class("c1", "Cohort 1", access_code="111111111")
    return d


def test_student_gets_own_code(db):
    s = db.create_student("c1", "Ada")
    assert s["access_code"] and len(s["access_code"]) == 9
    found = db.get_student_by_code(s["access_code"])
    assert found is not None and found["name"] == "Ada"


def test_codes_are_distinct_per_student(db):
    a = db.create_student("c1", "Ada")["access_code"]
    b = db.create_student("c1", "Bob")["access_code"]
    assert a != b


def test_codes_are_unique_across_classes(tmp_path):
    """A student types the code with no class selected, so it must be global."""
    d = ClassroomDB(db_path=tmp_path / "c.db")
    d.create_class("c1", "One", access_code="111111111")
    d.create_class("c2", "Two", access_code="222222222")
    d.create_student("c1", "Ada", access_code="999999999")
    with pytest.raises(ValueError, match="already in use"):
        d.create_student("c2", "Bob", access_code="999999999")


def test_unknown_code_resolves_to_nothing(db):
    assert db.get_student_by_code("000000000") is None
    assert db.get_student_by_code("") is None
    assert db.get_student_by_code("   ") is None


def test_revoked_student_cannot_reuse_their_code(db):
    s = db.create_student("c1", "Ada")
    assert db.revoke_student(s["token"], "c1") is True
    assert db.get_student_by_code(s["access_code"]) is None


def test_blank_values_rejected(db):
    with pytest.raises(ValueError):
        db.create_student("c1", "  ")
    with pytest.raises(ValueError):
        db.create_student("c1", "Ada", access_code="  ")


def test_unknown_class_rejected(db):
    with pytest.raises(ValueError, match="unknown class"):
        db.create_student("nope", "Ada")


def test_giving_a_code_to_an_existing_student_keeps_their_token(db):
    """Handing out a code must not fork a student's submission history."""
    first = db.connect_student("c1", "Ada")
    again = db.create_student("c1", "Ada", access_code="555555555")
    assert again["token"] == first["token"]
    assert db.get_student_by_code("555555555")["token"] == first["token"]


def test_many_students_without_codes_coexist(db):
    """NULL codes must not collide with each other under the unique index."""
    a = db.connect_student("c1", "NoCode1")
    b = db.connect_student("c1", "NoCode2")
    assert a["token"] != b["token"]
    assert db.get_student_by_code("") is None


def test_roster_lists_codes(db):
    db.create_student("c1", "Ada", access_code="111111111")
    db.create_student("c1", "Bob", access_code="222222222")
    roster = db.list_access_codes("c1")
    assert [r["name"] for r in roster] == ["Ada", "Bob"]
    assert {r["access_code"] for r in roster} == {"111111111", "222222222"}


def test_migration_is_idempotent_on_reopen(tmp_path):
    path = tmp_path / "c.db"
    first = ClassroomDB(db_path=path)
    first.create_class("c1", "One")
    first.create_student("c1", "Ada")
    second = ClassroomDB(db_path=path)      # reopens, re-runs migrations
    second.create_student("c1", "Bob")
    assert len(second.list_access_codes("c1")) == 2
