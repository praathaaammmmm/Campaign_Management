"""Campaign-aware filters on admin dashboards, lists and CSV exports.

Data set (see `_seed`): campaign A is the fixture's "Test Active Campaign" (id 1),
campaign B is "Beta Campaign". Offers 1 (Zirconia Crown) and 2 (Aligner Scan).

    Alpha One   A  offer 1  ACTIVE     registered today      staff-a   has email
    Alpha Two   A  offer 2  REDEEMED   registered today      admin     redeemed by staff-a
    Beta One    B  offer 1  REDEEMED   registered today      staff-a   redeemed by admin
    Beta Old    B  offer 2  EXPIRED    registered 20 days ago admin
"""
import csv
import io
import json
import re
from datetime import date, timedelta

import pytest

from app.database import SessionLocal
from app.models import AuditLog, Campaign, DeliveryLog, Offer, PatientOffer, campaign_offers
from app.registrations.service import register_patient_offer
from app.reporting.filters import ReportFilters, export_filename
from app.reporting.service import daily_time_series, dashboard_summary, registrations_by_category, staff_performance
from app.time_utils import utc_now
from tests.test_staff_interface import _create_staff, _login_as_staff

A = 1
NAMES = {"Alpha One", "Alpha Two", "Beta One", "Beta Old"}
LIST_PAGES = ("/patients", "/offers", "/delivery")
ALL_PAGES = ("/admin/dashboard", "/patients", "/offers", "/redemptions", "/delivery")
EXPORTS = ("/admin/reports/patients.csv", "/admin/reports/campaigns.csv")


def _campaign(name, status="ACTIVE"):
    db = SessionLocal()
    try:
        campaign = Campaign(
            name=name, start_date=date.today() - timedelta(days=30), end_date=date.today() + timedelta(days=1),
            status=status, created_by="test",
        )
        campaign.offers = db.query(Offer).order_by(Offer.id).all()
        db.add(campaign)
        db.commit()
        return campaign.id
    finally:
        db.close()


def _register(campaign_id, offer_id, name, mobile, actor="admin", email=""):
    db = SessionLocal()
    try:
        return register_patient_offer(
            db, full_name=name, mobile=mobile, email=email, age="", gender="", city="", doctor_name="",
            campaign_name="", campaign_id=campaign_id, offer_id=offer_id, beneficiary_category="CGHS" if offer_id == 1 else "ECHS",
            consent_given=True, actor=actor,
        ).id
    finally:
        db.close()


def _redeem(coupon_id, actor):
    db = SessionLocal()
    try:
        coupon = db.get(PatientOffer, coupon_id)
        coupon.status, coupon.redeemed_at, coupon.redeemed_by = "REDEEMED", utc_now(), actor
        db.add(AuditLog(user=actor, action="QR_REDEEMED", coupon_id=coupon.id, patient_id=coupon.patient_id))
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def seeded(client):
    _create_staff(username="staff-a")
    b = _campaign("Beta Campaign")
    ids = {
        "Alpha One": _register(A, 1, "Alpha One", "9833000001", actor="staff-a", email="alpha.one@example.com"),
        "Alpha Two": _register(A, 2, "Alpha Two", "9833000002"),
        "Beta One": _register(b, 1, "Beta One", "9833000003", actor="staff-a"),
        "Beta Old": _register(b, 2, "Beta Old", "9833000004"),
    }
    _redeem(ids["Alpha Two"], "staff-a")
    _redeem(ids["Beta One"], "admin")
    db = SessionLocal()
    try:
        old = db.get(PatientOffer, ids["Beta Old"])
        old.created_at = utc_now() - timedelta(days=20)
        old.expires_at = old.created_at + timedelta(days=10)
        old.status = "EXPIRED"
        for log in db.query(DeliveryLog).filter_by(coupon_id=old.id):
            log.sent_at = old.created_at
        sent = db.query(DeliveryLog).filter_by(coupon_id=ids["Beta One"]).one()
        sent.status = "SENT"
        db.commit()
    finally:
        db.close()
    return SimpleSeed(client=client, b=b, ids=ids)


class SimpleSeed:
    def __init__(self, **values):
        self.__dict__.update(values)


def _table(html):
    """The data rows of the page's (last) table, so filter dropdowns and the search box
    echo are never mistaken for results."""
    return html.rsplit("<tbody>", 1)[1].split("</tbody>", 1)[0]


def _shown(html):
    return {name for name in NAMES if name in _table(html)}


def _stat(html, label):
    return re.search(rf"<small>{label}</small><strong>([\d.]+%?)</strong>", html).group(1)


def _csv(response):
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(response.text)))


def _export_link(html, label="Download CSV"):
    return re.search(rf'<a class="secondary btn" href="([^"]+)">{label}</a>', html).group(1).replace("&amp;", "&")


def _selected(html, field):
    select_html = html.split(f'<select name="{field}"', 1)[1].split("</select>", 1)[0]
    match = re.search(r'<option value="(\d*)" selected>', select_html)
    return match.group(1) if match else ""


# --- All campaigns vs one campaign, on every page ---------------------------------------

def test_all_campaign_view_shows_everything(seeded):
    client = seeded.client
    for url in LIST_PAGES:
        page = client.get(url).text
        assert _shown(page) == NAMES, url
        assert "All campaigns" in page and "no filters applied" in page
    assert _shown(client.get("/redemptions").text) == {"Alpha Two", "Beta One"}
    dashboard = client.get("/admin/dashboard").text
    assert (_stat(dashboard, "Registrations"), _stat(dashboard, "Redeemed"), _stat(dashboard, "Expired")) == ("4", "2", "1")
    assert _stat(dashboard, "Conversion rate") == "50.0%"
    assert {row["Patient"] for row in _csv(client.get("/admin/reports/patients.csv"))} == NAMES
    assert {row["Campaign"]: row["Issued"] for row in _csv(client.get("/admin/reports/campaigns.csv"))} == {"Test Active Campaign": "2", "Beta Campaign": "2"}


def test_one_campaign_filter_on_every_page(seeded):
    client, b = seeded.client, seeded.b
    expected = {A: {"Alpha One", "Alpha Two"}, b: {"Beta One", "Beta Old"}}
    label = {A: "Test Active Campaign", b: "Beta Campaign"}
    for campaign_id, names in expected.items():
        for url in LIST_PAGES:
            page = client.get(f"{url}?campaign_id={campaign_id}").text
            assert _shown(page) == names, (url, campaign_id)
            assert f"Campaign: {label[campaign_id]}" in page  # active context is shown
            assert _selected(page, "campaign_id") == str(campaign_id)  # and the choice is kept
            assert f'href="{url}">Reset filters</a>' in page
        redeemed = {"Alpha Two"} if campaign_id == A else {"Beta One"}
        assert _shown(client.get(f"/redemptions?campaign_id={campaign_id}").text) == redeemed
        assert {row["Patient"] for row in _csv(client.get(f"/admin/reports/patients.csv?campaign_id={campaign_id}"))} == names
        campaign_rows = _csv(client.get(f"/admin/reports/campaigns.csv?campaign_id={campaign_id}"))
        assert [(row["Campaign"], row["Issued"], row["Redeemed"]) for row in campaign_rows] == [(label[campaign_id], "2", "1")]


def test_closed_campaigns_and_inactive_services_remain_selectable(seeded):
    client, b = seeded.client, seeded.b
    client.post(f"/admin/campaigns/{b}/close")
    client.post("/admin/services/2/toggle")
    for url in ALL_PAGES:
        page = client.get(f"{url}?campaign_id={b}").text
        assert f'<option value="{b}" selected>Beta Campaign (closed)</option>' in page, url
        assert "Free In-House Aligner Scan (inactive)" in page, url
    assert _shown(client.get(f"/patients?campaign_id={b}").text) == {"Beta One", "Beta Old"}


# --- Filters compose --------------------------------------------------------------------

def test_campaign_composes_with_service_status_search_and_dates(seeded):
    client, b = seeded.client, seeded.b
    today, old = date.today().isoformat(), (date.today() - timedelta(days=25)).isoformat()
    recent = (date.today() - timedelta(days=5)).isoformat()
    cases = {
        f"campaign_id={A}&offer_id=1": {"Alpha One"},
        f"campaign_id={A}&offer_id=2": {"Alpha Two"},
        f"campaign_id={b}&offer_id=1": {"Beta One"},
        f"campaign_id={A}&status=REDEEMED": {"Alpha Two"},
        f"campaign_id={b}&status=EXPIRED": {"Beta Old"},
        f"campaign_id={b}&status=ACTIVE": set(),
        f"campaign_id={b}&start={recent}&end={today}": {"Beta One"},
        f"campaign_id={b}&start={old}&end={recent}": {"Beta Old"},
        f"campaign_id={A}&start={old}&end={recent}": set(),
        f"campaign_id={b}&offer_id=2&status=EXPIRED&start={old}&end={recent}": {"Beta Old"},
        f"campaign_id={A}&offer_id=2&status=EXPIRED": set(),
        f"campaign_id={A}&q=Alpha+Two": {"Alpha Two"},
        f"campaign_id={b}&q=Alpha": set(),
        f"campaign_id={b}&q=9833000003": {"Beta One"},
        "offer_id=1&status=REDEEMED": {"Beta One"},
        "q=Alpha": {"Alpha One", "Alpha Two"},
    }
    for query, names in cases.items():
        for url in ("/patients", "/offers"):
            assert _shown(client.get(f"{url}?{query}").text) == names, (url, query)
        assert {row["Patient"] for row in _csv(client.get(f"/admin/reports/patients.csv?{query}"))} == names, query

    page = client.get(f"/patients?campaign_id={b}&offer_id=2&status=EXPIRED&start={old}&end={recent}").text
    assert "Campaign: Beta Campaign · Service: Free In-House Aligner Scan · Status: Expired" in page
    assert (_selected(page, "offer_id"), _selected(page, "campaign_id")) == ("2", str(b))
    assert f'name="start" value="{old}"' in page and f'name="end" value="{recent}"' in page

    campaign_rows = _csv(client.get(f"/admin/reports/campaigns.csv?campaign_id={b}&offer_id=2&start={old}&end={recent}"))
    assert [(row["Campaign"], row["Issued"], row["Expired"]) for row in campaign_rows] == [("Beta Campaign", "1", "1")]


def test_redemptions_filter_by_campaign_service_and_redemption_date(seeded):
    client, b = seeded.client, seeded.b
    today, yesterday = date.today().isoformat(), (date.today() - timedelta(days=1)).isoformat()
    db = SessionLocal()
    try:  # Beta One was registered today but redeemed "three days ago"
        coupon = db.get(PatientOffer, seeded.ids["Beta One"])
        coupon.redeemed_at = utc_now() - timedelta(days=3)
        db.commit()
    finally:
        db.close()

    assert _shown(client.get(f"/redemptions?start={yesterday}&end={today}").text) == {"Alpha Two"}
    assert _shown(client.get(f"/redemptions?campaign_id={b}&start={yesterday}").text) == set()
    assert _shown(client.get(f"/redemptions?campaign_id={b}&end={yesterday}").text) == {"Beta One"}
    assert _shown(client.get(f"/redemptions?campaign_id={A}&offer_id=2").text) == {"Alpha Two"}
    assert _shown(client.get(f"/redemptions?campaign_id={A}&offer_id=1").text) == set()
    page = client.get(f"/redemptions?campaign_id={b}&end={yesterday}").text
    assert "Campaign: Beta Campaign" in page and "Redeemed: earliest to" in page


def test_historical_records_survive_service_removal_and_deactivation(seeded):
    client = seeded.client
    client.post(f"/admin/campaigns/{A}/services/2/remove")  # Aligner Scan leaves campaign A...
    client.post("/admin/services/2/toggle")                 # ...and is deactivated in the catalog
    db = SessionLocal()
    try:
        assert db.execute(campaign_offers.select().where(campaign_offers.c.campaign_id == A, campaign_offers.c.offer_id == 2)).first() is None
        assert db.get(Offer, 2).active is False
    finally:
        db.close()

    for url in ("/patients", "/offers", "/delivery"):
        assert _shown(client.get(f"{url}?campaign_id={A}&offer_id=2").text) == {"Alpha Two"}, url
        assert _shown(client.get(f"{url}?campaign_id={A}").text) == {"Alpha One", "Alpha Two"}, url
    assert _shown(client.get(f"/redemptions?campaign_id={A}&offer_id=2").text) == {"Alpha Two"}
    rows = _csv(client.get(f"/admin/reports/patients.csv?campaign_id={A}&offer_id=2"))
    assert [(row["Patient"], row["Service"], row["Status"]) for row in rows] == [("Alpha Two", "Free In-House Aligner Scan", "REDEEMED")]
    dashboard = client.get(f"/admin/dashboard?campaign_id={A}&offer_id=2").text
    assert (_stat(dashboard, "Registrations"), _stat(dashboard, "Redeemed")) == ("1", "1")
    assert _csv(client.get(f"/admin/reports/campaigns.csv?campaign_id={A}&offer_id=2"))[0]["Issued"] == "1"


def test_registration_without_a_campaign_only_appears_under_all_campaigns(seeded):
    client = seeded.client
    db = SessionLocal()
    try:
        db.get(PatientOffer, seeded.ids["Alpha One"]).campaign_id = None  # a pre-campaign legacy record
        db.commit()
    finally:
        db.close()
    assert _shown(client.get("/patients").text) == NAMES
    assert _shown(client.get(f"/patients?campaign_id={A}").text) == {"Alpha Two"}
    assert _shown(client.get(f"/delivery?campaign_id={A}").text) == {"Alpha Two"}
    assert {row["Patient"]: row["Campaign"] for row in _csv(client.get("/admin/reports/patients.csv"))}["Alpha One"] == ""


# --- Delivery ---------------------------------------------------------------------------

def test_delivery_is_filtered_through_the_coupons_campaign(seeded):
    client, b = seeded.client, seeded.b
    old, recent = (date.today() - timedelta(days=25)).isoformat(), (date.today() - timedelta(days=5)).isoformat()
    db = SessionLocal()
    try:
        assert db.query(DeliveryLog).count() == 5  # one WhatsApp each, plus Alpha One's email
    finally:
        db.close()

    def attempts(query):
        return re.search(r"<p>(\d+) attempts?", client.get(f"/delivery?{query}").text).group(1)

    assert (attempts(""), attempts(f"campaign_id={A}"), attempts(f"campaign_id={b}")) == ("5", "3", "2")
    assert _shown(client.get(f"/delivery?campaign_id={A}&channel=EMAIL").text) == {"Alpha One"}
    assert attempts(f"campaign_id={b}&channel=EMAIL") == "0"
    assert _shown(client.get(f"/delivery?campaign_id={b}&delivery_status=SENT").text) == {"Beta One"}
    assert _shown(client.get(f"/delivery?campaign_id={A}&delivery_status=SENT").text) == set()
    assert _shown(client.get(f"/delivery?campaign_id={b}&offer_id=2").text) == {"Beta Old"}
    assert _shown(client.get(f"/delivery?campaign_id={b}&start={old}&end={recent}").text) == {"Beta Old"}
    assert _shown(client.get(f"/delivery?campaign_id={b}&q=Beta+One").text) == {"Beta One"}
    page = client.get(f"/delivery?campaign_id={b}&channel=WHATSAPP&delivery_status=SENT").text
    assert "Campaign: Beta Campaign · Channel: Whatsapp · Delivery status: Sent" in page
    assert '<option value="SENT" selected>' in page and '<option value="WHATSAPP" selected>' in page


# --- Dashboard --------------------------------------------------------------------------

def test_dashboard_cards_chart_and_breakdowns_follow_the_selected_campaign(seeded):
    client, b = seeded.client, seeded.b
    today = utc_now().date().isoformat()

    page = client.get(f"/admin/dashboard?campaign_id={A}").text
    assert (_stat(page, "Registrations"), _stat(page, "Redeemed"), _stat(page, "Expired"), _stat(page, "Conversion rate")) == ("2", "1", "0", "50.0%")
    assert "Campaign: Test Active Campaign" in page and '<b class="filter-context">Test Active Campaign</b>' in page
    series = json.loads(re.search(r"const series = (\{.*?\});", page).group(1))
    assert series == {"registrations": [{"day": today, "count": 2}], "redemptions": [{"day": today, "count": 1}]}
    assert "(CGHS)</td><td>1</td>" in page and "(ECHS)</td><td>1</td>" in page and "(CAPF)</td><td>0</td>" in page
    assert "<td>staff-a</td><td>1</td><td>1</td>" in page  # registered Alpha One, redeemed Alpha Two

    page = client.get(f"/admin/dashboard?campaign_id={b}").text
    assert (_stat(page, "Registrations"), _stat(page, "Redeemed"), _stat(page, "Expired"), _stat(page, "Conversion rate")) == ("2", "1", "1", "50.0%")
    assert _stat(page, "WhatsApp delivery") == "50.0%" and _stat(page, "Email delivery") == "0%"
    assert "<td>staff-a</td><td>1</td><td>0</td>" in page  # registered Beta One; an admin redeemed it

    page = client.get(f"/admin/dashboard?campaign_id={b}&offer_id=2").text
    assert (_stat(page, "Registrations"), _stat(page, "Redeemed"), _stat(page, "Conversion rate")) == ("1", "0", "0.0%")
    assert "<td>staff-a</td><td>0</td><td>0</td>" in page
    assert "<td>staff-a</td><td>2</td><td>1</td>" in client.get("/admin/dashboard").text

    db = SessionLocal()
    try:  # every dashboard query agrees on the same filtered set
        assert dashboard_summary(db, A, 1) == {"issued": 1, "redeemed": 0, "expired": 0}
        assert daily_time_series(db, b, 2)["registrations"][0]["count"] == 1 and daily_time_series(db, b, 2)["redemptions"] == []
        assert {row["value"]: row["count"] for row in registrations_by_category(db, b) if row["count"]} == {"CGHS": 1, "ECHS": 1}
        assert staff_performance(db, campaign_id=A, offer_id=2) == [{"staff_id": 1, "username": "staff-a", "registrations": 0, "redemptions": 1}]
    finally:
        db.close()


def test_dashboard_links_carry_the_filters_to_exports_and_lists(seeded):
    client, b = seeded.client, seeded.b
    start = (date.today() - timedelta(days=25)).isoformat()
    page = client.get(f"/admin/dashboard?campaign_id={b}&offer_id=2&start={start}").text
    query = f"campaign_id={b}&offer_id=2&start={start}"
    assert _export_link(page, "Download patient CSV") == f"/admin/reports/patients.csv?{query}"
    assert _export_link(page, "Download campaign CSV") == f"/admin/reports/campaigns.csv?{query}"
    assert _export_link(page, "View patients") == f"/patients?{query}"
    assert _export_link(page, "View delivery activity") == f"/delivery?campaign_id={b}&offer_id=2"
    assert 'href="/admin/dashboard">Reset filters</a>' in page

    patients = _csv(client.get(_export_link(page, "Download patient CSV")))
    assert [row["Patient"] for row in patients] == ["Beta Old"] and _stat(page, "Registrations") == "1"
    assert _shown(client.get(_export_link(page, "View patients")).text) == {"Beta Old"}
    plain = client.get("/admin/dashboard").text
    assert _export_link(plain, "Download patient CSV") == "/admin/reports/patients.csv"


# --- CSV exports ------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["/patients", "/offers", "/redemptions"])
def test_csv_export_from_a_filtered_list_matches_the_rows_on_screen(seeded, url):
    client, b = seeded.client, seeded.b
    start = (date.today() - timedelta(days=2)).isoformat()
    for query in ("", f"campaign_id={A}", f"campaign_id={b}&offer_id=1", f"campaign_id={b}&start={start}", f"campaign_id={A}&q=Alpha+Two", f"campaign_id={b}&offer_id=2&q=nobody"):
        page = client.get(f"{url}?{query}").text
        exported = _csv(client.get(_export_link(page)))
        assert {row["Patient"] for row in exported} == _shown(page), (url, query)
        assert len(exported) == len(_shown(page))
        if url == "/redemptions":
            assert all(row["Status"] == "REDEEMED" for row in exported)
    filtered = _export_link(client.get(f"{url}?campaign_id={b}&offer_id=1&q=Beta").text)
    assert f"campaign_id={b}" in filtered and "offer_id=1" in filtered and "q=Beta" in filtered


def test_csv_download_headers_and_safe_filenames(seeded):
    client, b = seeded.client, seeded.b
    plain = client.get("/admin/reports/patients.csv")
    assert plain.headers["content-disposition"] == 'attachment; filename="patient-registrations.csv"'
    assert client.get("/admin/reports/campaigns.csv").headers["content-disposition"] == 'attachment; filename="campaign-performance.csv"'
    one = client.get(f"/admin/reports/patients.csv?campaign_id={b}")
    assert one.headers["content-disposition"] == f'attachment; filename="patient-registrations-campaign-{b}-beta-campaign.csv"'
    assert client.get(f"/admin/reports/campaigns.csv?campaign_id={A}").headers["content-disposition"] == 'attachment; filename="campaign-performance-campaign-1-test-active-campaign.csv"'
    assert client.get("/admin/reports/patients.csv?campaign_id=99999").headers["content-disposition"] == 'attachment; filename="patient-registrations.csv"'

    evil = _campaign('Evil"; filename=x.exe\r\nSet-Cookie: a=b ../../etc/passwd <script>')
    header = client.get(f"/admin/reports/patients.csv?campaign_id={evil}").headers["content-disposition"]
    assert header.startswith(f'attachment; filename="patient-registrations-campaign-{evil}-evil-filename-x-exe-set-cookie-a-b-etc') and header.endswith('.csv"')
    assert len(header) < 110  # the slug is capped
    assert re.fullmatch(r'attachment; filename="[a-z0-9.-]+"', header)
    db = SessionLocal()
    try:
        assert export_filename("x", db.get(Campaign, evil)).count('"') == 0
        unnamed = Campaign(id=7, name="!!! ???")
        assert export_filename("x", unnamed) == "x-campaign-7.csv"
    finally:
        db.close()


# --- Empty, malformed and unknown -------------------------------------------------------

def test_empty_filtered_results_render_a_normal_empty_state(seeded):
    client = seeded.client
    empty = _campaign("Empty Campaign")
    messages = {
        "/patients": "No patients match these filters.", "/offers": "No issued coupons match these filters.",
        "/redemptions": "No redemptions match these filters.", "/delivery": "No delivery activity matches these filters.",
    }
    for url, message in messages.items():
        response = client.get(f"{url}?campaign_id={empty}")
        assert response.status_code == 200 and message in response.text and _shown(response.text) == set()
        assert "Campaign: Empty Campaign" in response.text
    dashboard = client.get(f"/admin/dashboard?campaign_id={empty}")
    assert dashboard.status_code == 200
    assert (_stat(dashboard.text, "Registrations"), _stat(dashboard.text, "Conversion rate"), _stat(dashboard.text, "Email delivery")) == ("0", "0%", "0%")
    assert "No registrations match the selected filters." in dashboard.text
    assert json.loads(re.search(r"const series = (\{.*?\});", dashboard.text).group(1)) == {"registrations": [], "redemptions": []}
    assert _csv(client.get(f"/admin/reports/patients.csv?campaign_id={empty}")) == []
    assert [(row["Campaign"], row["Issued"], row["Conversion rate"]) for row in _csv(client.get(f"/admin/reports/campaigns.csv?campaign_id={empty}"))] == [("Empty Campaign", "0", "0.0%")]


def test_empty_database_pages_still_render(client):
    for url in ALL_PAGES + EXPORTS:
        assert client.get(url).status_code == 200, url
        assert client.get(f"{url}?campaign_id=1&offer_id=1&status=ACTIVE&start=2026-01-01&end=2026-01-02").status_code == 200, url


@pytest.mark.parametrize("value", ["999999", "99999999999999999999999999", "0"])
def test_unknown_campaign_id_matches_nothing(seeded, value):
    client = seeded.client
    for url in ("/patients", "/offers", "/redemptions", "/delivery"):
        response = client.get(f"{url}?campaign_id={value}")
        assert response.status_code == 200 and _shown(response.text) == set(), url
        assert "Campaign: not found" in response.text and _selected(response.text, "campaign_id") == ""
    dashboard = client.get(f"/admin/dashboard?campaign_id={value}")
    assert dashboard.status_code == 200 and _stat(dashboard.text, "Registrations") == "0"
    assert _csv(client.get(f"/admin/reports/patients.csv?campaign_id={value}")) == []
    assert client.get(f"/admin/reports/campaigns.csv?campaign_id={value}").status_code == 200


@pytest.mark.parametrize("query", [
    "campaign_id=abc", "campaign_id=-5", "campaign_id=1%20OR%201=1", "campaign_id=1;DROP%20TABLE%20patients", "campaign_id=%27",
    "campaign_id=1.5", "campaign_id=", "offer_id=xyz", "offer_id=99999999999999999999", "status=NOPE", "status=%27%20OR%201=1--",
    "start=not-a-date&end=2026-13-45", "date_field=evil", "channel=FAX&delivery_status=LOST", "q=%25%27%22%3Cscript%3E",
    "campaign_id=abc&offer_id=abc&status=abc&start=abc&end=abc&q=" + "x" * 500,
])
def test_malformed_filter_values_are_ignored_safely(seeded, query):
    client = seeded.client
    for url in ALL_PAGES + EXPORTS:
        response = client.get(f"{url}?{query}")
        assert response.status_code == 200, (url, query)
        assert "<script>" not in response.text.split("</head>", 1)[-1].split("<script", 1)[0]
    if "q=" not in query and "9999" not in query:
        # An invalid value is treated as "not set": the unfiltered view, not an error.
        # (A well-formed but impossible id is a real filter that matches nothing.)
        assert _shown(client.get(f"/patients?{query}").text) == NAMES
    db = SessionLocal()
    try:
        assert db.query(PatientOffer).count() == 4
    finally:
        db.close()


def test_filter_parsing_rules():
    def parse(query):
        from types import SimpleNamespace
        from urllib.parse import parse_qs
        params = {key: values[0] for key, values in parse_qs(query, keep_blank_values=True).items()}
        return ReportFilters.from_request(SimpleNamespace(query_params=params))

    assert parse("campaign_id=7&offer_id=3&status=redeemed") == ReportFilters(campaign_id=7, offer_id=3, status="REDEEMED")
    assert parse("campaign_id=abc&offer_id=&status=x&start=nope&date_field=x") == ReportFilters()
    assert parse("campaign_id=99999999999").campaign_id == -1
    assert parse("start=2026-10-01&end=2026-10-05").start_date == date(2026, 10, 1)
    assert parse("q=%20%20two%20%20words%20").q == "two words" and len(parse("q=" + "y" * 400).q) == 100
    filters = parse("campaign_id=7&q=a+b&start=2026-10-01&date_field=redeemed")
    assert filters.query("campaign_id", "offer_id", "q", "start", "end") == "campaign_id=7&q=a+b&start=2026-10-01"
    assert filters.query("campaign_id", status="REDEEMED", date_field="redeemed") == "campaign_id=7&status=REDEEMED&date_field=redeemed"


# --- Admin only -------------------------------------------------------------------------

def test_reports_and_filters_are_admin_only_and_staff_stay_scoped(seeded):
    client, b = seeded.client, seeded.b
    _login_as_staff(client, campaign_ids=[A])

    for url in ALL_PAGES + EXPORTS:
        for query in ("", f"?campaign_id={b}", f"?campaign_id={A}&offer_id=1"):
            response = client.get(url + query, follow_redirects=False)
            assert response.status_code == 403 and "Beta" not in response.text, (url, query)

    page = client.get("/staff/patients").text
    assert _shown(page) == {"Alpha One", "Alpha Two"}
    for admin_only in ("Download CSV", "/admin/reports", "report-filters", "Beta Campaign", "All services", "<th>Campaign</th>"):
        assert admin_only not in page
    # The staff list's own campaign filter cannot be pointed at another campaign.
    assert _shown(client.get(f"/staff/patients?campaign_id={b}").text) == set()
    assert _shown(client.get(f"/staff/patients?campaign_id={A}&status=REDEEMED").text) == {"Alpha Two"}

    client.post("/logout")
    for url in ALL_PAGES + EXPORTS:
        response = client.get(f"{url}?campaign_id={A}", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"
