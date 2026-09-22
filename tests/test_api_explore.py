"""The people count behind /api/v1/search/people – the Python twin of
personenAus() in page.html, held to the same rules."""

import api_explore


def test_people_of_zaehlt_wie_die_seite():
    rows = [
        ("Carla Chef", "carla@example.com", "2026-06-10 08:00", "outlook"),
        ("Carla Chef", "carla@example.com", "2026-05-01 08:00", "outlook"),
        ("Bob Baumeister, Carla Chef", "bob@example.com", "2026-07-01 12:00", "planner"),
        ("(unbekannt)", None, "2026-05-02 10:00", "teams"),
        ("", None, "", "kontakte"),
        # A contact card names its company – no person, the book has it
        ("Beispiel GmbH", None, "", "kontakte"),
        ("Bob Baumeister", None, None, "teams"),
    ]
    leute = api_explore.people_of(rows)
    # Both were last named on the same day – the one named more often first.
    assert [p["name"] for p in leute] == ["Carla Chef", "Bob Baumeister"]
    carla, bob = leute
    # An assignee list pins no address on anyone; a row without a date
    # counts but sets no span.
    assert bob["address"] == "" and bob["items"] == 2
    assert bob["by_source"] == {"planner": 1, "teams": 1}
    assert bob["first"] == bob["last"] == "2026-07-01 12:00"
    assert carla["address"] == "carla@example.com" and carla["items"] == 3
    assert carla["first"] == "2026-05-01 08:00" and carla["last"] == "2026-07-01 12:00"


def test_people_of_sortiert_nach_letztem_kontakt_menge_und_name():
    rows = [
        ("Zoe", None, "2026-01-01 08:00", "outlook"),
        ("Anna", None, "2026-01-01 08:00", "outlook"),
        ("Mia", None, "2026-01-01 08:00", "outlook"),
        ("Mia", None, "2025-01-01 08:00", "outlook"),
        ("Ohne Datum", None, "", "outlook"),
        ("Neu", None, "2026-03-01 08:00", "outlook"),
    ]
    assert [p["name"] for p in api_explore.people_of(rows)] == ["Neu", "Mia", "Anna", "Zoe", "Ohne Datum"]


def test_people_of_mit_nichts():
    assert api_explore.people_of([]) == []
